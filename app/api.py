"""REST routes for things that don't need to be real-time:
finding people, listing conversations, starting a DM, loading old messages.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlmodel import Session, col, select

from .auth import get_current_user
from .db import get_session
from .models import Conversation, ConversationMember, Message, User
from .services import conversation_out, is_member, message_out, user_out

router = APIRouter(prefix="/api", tags=["chat"])


@router.get("/users")
def search_users(
    q: str = Query("", max_length=30),
    me: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    query = select(User).where(User.id != me.id)
    if q:
        query = query.where(col(User.username).contains(q.lower()))
    users = session.exec(query.order_by(User.username).limit(20)).all()
    return [user_out(u) for u in users]


@router.get("/conversations")
def list_conversations(me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    conversations = session.exec(
        select(Conversation)
        .join(ConversationMember, ConversationMember.conversation_id == Conversation.id)
        .where(ConversationMember.user_id == me.id)
    ).all()
    result = [conversation_out(session, c, me) for c in conversations]
    # Most recently active first; brand-new empty chats go to the top too.
    result.sort(key=lambda c: (c["last_message"] or {}).get("id", 10**12), reverse=True)
    return result


class StartChatBody(BaseModel):
    username: str


@router.post("/conversations")
def start_direct_chat(
    body: StartChatBody,
    me: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Open a DM with someone. Returns the existing one if you've talked before."""
    other = session.exec(select(User).where(User.username == body.username.lower())).first()
    if other is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No user with that name.")
    if other.id == me.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't start a chat with yourself.")

    mine = set(session.exec(select(ConversationMember.conversation_id).where(ConversationMember.user_id == me.id)).all())
    theirs = set(session.exec(select(ConversationMember.conversation_id).where(ConversationMember.user_id == other.id)).all())
    for conversation_id in mine & theirs:
        conversation = session.get(Conversation, conversation_id)
        if conversation and conversation.type == "direct":
            return conversation_out(session, conversation, me)

    conversation = Conversation(type="direct")
    session.add(conversation)
    session.commit()
    session.refresh(conversation)
    session.add(ConversationMember(conversation_id=conversation.id, user_id=me.id))
    session.add(ConversationMember(conversation_id=conversation.id, user_id=other.id))
    session.commit()
    return conversation_out(session, conversation, me)


@router.get("/conversations/{conversation_id}/messages")
def message_history(
    conversation_id: int,
    before: int | None = Query(None, description="Load messages older than this id"),
    limit: int = Query(50, ge=1, le=100),
    me: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if not is_member(session, conversation_id, me.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found.")
    query = (
        select(Message, User)
        .join(User, Message.sender_id == User.id)
        .where(Message.conversation_id == conversation_id)
    )
    if before is not None:
        query = query.where(Message.id < before)
    rows = session.exec(query.order_by(Message.id.desc()).limit(limit)).all()
    return [message_out(m, u) for m, u in reversed(rows)]  # oldest first
