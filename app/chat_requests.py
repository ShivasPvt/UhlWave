"""Private accounts and chat requests.

If someone turns on "Private account" in Settings, people they don't already
chat with can't message them directly. Instead they send a chat request:

  POST   /api/requests                 ask to chat with someone (or open the chat if allowed)
  GET    /api/requests                 your incoming and outgoing requests that are still waiting
  POST   /api/requests/{id}/accept     say yes: the chat opens for both of you
  POST   /api/requests/{id}/decline    say no (they aren't told; their request just goes away)
  DELETE /api/requests/{id}            take back a request you sent

Both people get a "requests_changed" WebSocket event so their lists update live.
"""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import Session, col, select

from .auth import get_current_user
from .db import get_session
from .manager import manager
from .models import ChatRequest, Conversation, ConversationMember, User, utcnow
from .services import conversation_out, has_blocked, iso, user_out

router = APIRouter(prefix="/api/requests", tags=["requests"])

ASK_AGAIN_AFTER = timedelta(days=1)  # after a "no", wait this long before asking again


def direct_chat_between(session: Session, a: int, b: int) -> Conversation | None:
    mine = select(ConversationMember.conversation_id).where(ConversationMember.user_id == a)
    rows = session.exec(
        select(Conversation)
        .join(ConversationMember, ConversationMember.conversation_id == Conversation.id)
        .where(Conversation.type == "direct", ConversationMember.user_id == b, col(Conversation.id).in_(mine))
    ).all()
    return rows[0] if rows else None


def create_direct_chat(session: Session, a: User, b: User) -> Conversation:
    conversation = Conversation(type="direct")
    session.add(conversation)
    session.commit()
    session.refresh(conversation)
    session.add(ConversationMember(conversation_id=conversation.id, user_id=a.id))
    session.add(ConversationMember(conversation_id=conversation.id, user_id=b.id))
    session.commit()
    return conversation


def needs_request(session: Session, me: User, other: User) -> bool:
    """True if `other` has a private account and hasn't chatted with `me` before."""
    return other.is_private and not other.is_bot and direct_chat_between(session, me.id, other.id) is None


def private_add_problem(session: Session, adder: User, user: User) -> str | None:
    """Private accounts can only be added to groups by people they already chat with."""
    if user.id != adder.id and needs_request(session, adder, user):
        return f"{user.username} has a private account. Only people they already chat with can add them to groups."
    return None


def request_out(r: ChatRequest, other: User) -> dict:
    return {
        "id": r.id, "status": r.status, "note": r.note, "created_at": iso(r.created_at),
        "from_user_id": r.from_user_id, "to_user_id": r.to_user_id, "user": user_out(other),
    }


async def _tell(*user_ids: int) -> None:
    await manager.send_to_users(user_ids, {"type": "requests_changed"})


class AskBody(BaseModel):
    username: str
    note: str | None = Field(default=None, max_length=200)


@router.post("")
async def ask(body: AskBody, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    other = session.exec(select(User).where(User.username == body.username.strip().lower())).first()
    if other is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No user with that name.")
    if other.id == me.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't start a chat with yourself.")
    if has_blocked(session, other.id, me.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can't message this person.")

    existing = direct_chat_between(session, me.id, other.id)
    if existing or not other.is_private or other.is_bot:
        conversation = existing or create_direct_chat(session, me, other)
        return {"status": "chat", "conversation": conversation_out(session, conversation, me)}

    # They might already have asked *us*: then this is a yes.
    theirs = session.exec(select(ChatRequest).where(
        ChatRequest.from_user_id == other.id, ChatRequest.to_user_id == me.id, ChatRequest.status == "pending",
    )).first()
    if theirs:
        return await _accept(session, theirs, me)

    pending = session.exec(select(ChatRequest).where(
        ChatRequest.from_user_id == me.id, ChatRequest.to_user_id == other.id, ChatRequest.status == "pending",
    )).first()
    if pending:
        return {"status": "pending", "request": request_out(pending, other)}

    declined = session.exec(
        select(ChatRequest).where(
            ChatRequest.from_user_id == me.id, ChatRequest.to_user_id == other.id, ChatRequest.status == "declined",
        ).order_by(col(ChatRequest.answered_at).desc())
    ).first()
    if declined and declined.answered_at and declined.answered_at.replace(tzinfo=None) > (utcnow() - ASK_AGAIN_AFTER).replace(tzinfo=None):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"{other.username} didn't accept your last request. You can ask again tomorrow.")

    note = (body.note or "").strip() or None
    request = ChatRequest(from_user_id=me.id, to_user_id=other.id, note=note)
    session.add(request)
    session.commit()
    session.refresh(request)
    await _tell(other.id)
    return {"status": "pending", "request": request_out(request, other)}


@router.get("")
def list_requests(me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    incoming = session.exec(
        select(ChatRequest, User).join(User, User.id == ChatRequest.from_user_id)
        .where(ChatRequest.to_user_id == me.id, ChatRequest.status == "pending")
        .order_by(col(ChatRequest.id).desc())
    ).all()
    outgoing = session.exec(
        select(ChatRequest, User).join(User, User.id == ChatRequest.to_user_id)
        .where(ChatRequest.from_user_id == me.id, ChatRequest.status == "pending")
        .order_by(col(ChatRequest.id).desc())
    ).all()
    return {
        "incoming": [request_out(r, u) for r, u in incoming],
        "outgoing": [request_out(r, u) for r, u in outgoing],
    }


def _my_incoming(session: Session, request_id: int, me: User) -> ChatRequest:
    request = session.get(ChatRequest, request_id)
    if request is None or request.to_user_id != me.id or request.status != "pending":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That request isn't waiting any more.")
    return request


async def _accept(session: Session, request: ChatRequest, me: User) -> dict:
    sender = session.get(User, request.from_user_id)
    request.status, request.answered_at = "accepted", utcnow()
    session.add(request)
    session.commit()
    conversation = direct_chat_between(session, me.id, sender.id) or create_direct_chat(session, me, sender)
    await _tell(me.id, sender.id)
    await manager.send_to_users([me.id, sender.id], {"type": "conversations_changed"})
    return {"status": "chat", "conversation": conversation_out(session, conversation, me)}


@router.post("/{request_id}/accept")
async def accept(request_id: int, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    return await _accept(session, _my_incoming(session, request_id, me), me)


@router.post("/{request_id}/decline")
async def decline(request_id: int, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    request = _my_incoming(session, request_id, me)
    request.status, request.answered_at = "declined", utcnow()
    session.add(request)
    session.commit()
    await _tell(me.id, request.from_user_id)
    return {"ok": True}


@router.delete("/{request_id}")
async def cancel(request_id: int, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    request = session.get(ChatRequest, request_id)
    if request is None or request.from_user_id != me.id or request.status != "pending":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That request isn't waiting any more.")
    to_user = request.to_user_id
    session.delete(request)
    session.commit()
    await _tell(me.id, to_user)
    return {"ok": True}
