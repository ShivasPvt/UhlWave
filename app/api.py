"""REST routes for things that don't need to be real-time:
finding people, listing conversations, starting a DM, loading old messages.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlmodel import Session, col, select

from .auth import get_current_user
from .chat_requests import create_direct_chat, direct_chat_between, needs_request
from .db import get_session
from .folders import is_unlocked, locked_ids, require_open
from .models import Conversation, ConversationMember, Message, User
from .services import conversation_out, has_blocked, is_member, messages_out, not_expired, user_out

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
def list_conversations(request: Request, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Your chats. Locked chats are only included after you've entered your PIN."""
    conversations = session.exec(
        select(Conversation)
        .join(ConversationMember, ConversationMember.conversation_id == Conversation.id)
        .where(ConversationMember.user_id == me.id)
    ).all()
    if not is_unlocked(request, me):
        hidden = locked_ids(session, me.id)
        conversations = [c for c in conversations if c.id not in hidden]
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
    """Open a DM with someone. Returns the existing one if you've talked before.
    People with private accounts need a chat request first (see chat_requests.py)."""
    other = session.exec(select(User).where(User.username == body.username.lower())).first()
    if other is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No user with that name.")
    if other.id == me.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't start a chat with yourself.")

    existing = direct_chat_between(session, me.id, other.id)
    if existing:
        return conversation_out(session, existing, me)
    if needs_request(session, me, other):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"{other.username} has a private account. Send them a chat request.")
    if has_blocked(session, other.id, me.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can't message this person.")
    return conversation_out(session, create_direct_chat(session, me, other), me)


@router.get("/conversations/{conversation_id}/messages")
def message_history(
    conversation_id: int,
    request: Request,
    before: int | None = Query(None, description="Load messages older than this id"),
    limit: int = Query(50, ge=1, le=100),
    me: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if not is_member(session, conversation_id, me.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found.")
    require_open(session, request, me, conversation_id)
    query = (
        select(Message, User)
        .join(User, Message.sender_id == User.id)
        .where(Message.conversation_id == conversation_id, not_expired())
    )
    if before is not None:
        query = query.where(Message.id < before)
    rows = session.exec(query.order_by(Message.id.desc()).limit(limit)).all()
    return messages_out(session, list(reversed(rows)))  # oldest first
