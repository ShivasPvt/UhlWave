"""Polls in chats and groups.

  POST /api/conversations/{id}/polls   {"question": "...", "options": ["A", "B"], "multiple": false}
  POST /api/polls/{id}/vote            {"option_ids": [12]}   (an empty list takes your vote back)
  POST /api/polls/{id}/close           end the poll (only the person who made it)
  GET  /api/polls/{id}                 the poll as it is now

Every vote is pushed to everyone in the chat as a "message_updated" event, so
the bars move live. Polls aren't allowed in secret chats (the server would have
to read the options to count votes).
"""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, delete, select

from .auth import get_current_user
from .db import get_session
from .manager import manager
from .models import Conversation, Message, Poll, PollOption, PollVote, User
from .services import is_member, load_message_out, member_ids, save_message, send_problem

router = APIRouter(prefix="/api", tags=["polls"])


class NewPoll(BaseModel):
    question: str = Field(min_length=1, max_length=300)
    options: list[str] = Field(min_length=2, max_length=12)
    multiple: bool = False


class VoteBody(BaseModel):
    option_ids: list[int] = Field(default_factory=list, max_length=12)


async def _push(session: Session, message_id: int) -> dict:
    out = load_message_out(session, message_id)
    await manager.send_to_users(member_ids(session, out["conversation_id"]), {"type": "message_updated", "message": out})
    return out


@router.post("/conversations/{conversation_id}/polls", status_code=status.HTTP_201_CREATED)
async def create_poll(conversation_id: int, body: NewPoll, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    problem = send_problem(session, conversation_id, me.id)
    if problem:
        not_in_chat = problem.startswith("You're not in")
        raise HTTPException(status.HTTP_404_NOT_FOUND if not_in_chat else status.HTTP_403_FORBIDDEN, problem)
    if session.get(Conversation, conversation_id).type == "secret":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Polls aren't available in secret chats.")
    question = body.question.strip()
    options = [o.strip()[:100] for o in body.options if o.strip()]
    if not question:
        raise HTTPException(422, "Ask a question.")
    if len(options) < 2:
        raise HTTPException(422, "A poll needs at least two options.")
    if len({o.lower() for o in options}) != len(options):
        raise HTTPException(422, "Each option needs to be different.")

    out = save_message(session, conversation_id, me, question, type="poll")
    poll = Poll(message_id=out["id"], multiple=body.multiple)
    session.add(poll)
    session.commit()
    session.refresh(poll)
    for i, text in enumerate(options):
        session.add(PollOption(poll_id=poll.id, position=i, text=text))
    session.commit()
    out = load_message_out(session, out["id"])
    await manager.send_to_users(member_ids(session, conversation_id), {"type": "message", "message": out, "client_id": None})
    return out


def _poll_for_member(session: Session, poll_id: int, me: User) -> tuple[Poll, Message]:
    poll = session.get(Poll, poll_id)
    message = session.get(Message, poll.message_id) if poll else None
    if poll is None or message is None or message.deleted_at is not None or not is_member(session, message.conversation_id, me.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Poll not found.")
    return poll, message


@router.get("/polls/{poll_id}")
def get_poll(poll_id: int, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """The poll's message as it is now (the app re-checks after a vote is refused)."""
    _, message = _poll_for_member(session, poll_id, me)
    return load_message_out(session, message.id)


@router.post("/polls/{poll_id}/vote")
async def vote(poll_id: int, body: VoteBody, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    poll, message = _poll_for_member(session, poll_id, me)
    if poll.closed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This poll has ended.")
    problem = send_problem(session, message.conversation_id, me.id)
    if problem:
        raise HTTPException(status.HTTP_403_FORBIDDEN, problem)
    wanted = list(dict.fromkeys(body.option_ids))
    if len(wanted) > 1 and not poll.multiple:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pick one option.")
    valid = set(session.exec(select(PollOption.id).where(PollOption.poll_id == poll.id)).all())
    if any(o not in valid for o in wanted):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That option isn't in this poll.")
    # Replace your old votes with the new ones. Two of your votes landing at the same
    # moment (double tap, two tabs) can clash on the unique key; the later one wins.
    for attempt in range(2):
        try:
            session.execute(delete(PollVote).where(col(PollVote.poll_id) == poll.id, col(PollVote.user_id) == me.id))
            for option_id in wanted:
                session.add(PollVote(poll_id=poll.id, option_id=option_id, user_id=me.id))
            session.commit()
            break
        except IntegrityError:
            session.rollback()
            if attempt:
                raise HTTPException(status.HTTP_409_CONFLICT, "Your vote clashed with another one. Try again.")
    return await _push(session, message.id)


@router.post("/polls/{poll_id}/close")
async def close_poll(poll_id: int, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    poll, message = _poll_for_member(session, poll_id, me)
    if message.sender_id != me.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the person who made the poll can end it.")
    poll.closed = True
    session.add(poll)
    session.commit()
    return await _push(session, message.id)


def delete_polls_for(session: Session, message_ids_query) -> None:
    """Remove polls (and their options and votes) for messages about to be deleted."""
    polls = select(Poll.id).where(col(Poll.message_id).in_(message_ids_query))
    session.execute(delete(PollVote).where(col(PollVote.poll_id).in_(polls)))
    session.execute(delete(PollOption).where(col(PollOption.poll_id).in_(polls)))
    session.execute(delete(Poll).where(col(Poll.message_id).in_(message_ids_query)))
