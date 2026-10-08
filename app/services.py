"""Shared helpers used by both the REST routes and the WebSocket handler."""
from datetime import datetime, timezone

from sqlmodel import Session, select

from .config import BOT_USERNAME
from .manager import manager
from .models import Conversation, ConversationMember, Message, User


def iso(dt: datetime | None) -> str | None:
    """Timestamps as ISO strings. SQLite forgets the timezone, so we put UTC back."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def user_out(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "is_bot": user.is_bot,
        "online": True if user.is_bot else manager.is_online(user.id),
        "last_seen_at": iso(user.last_seen_at),
    }


def message_out(message: Message, sender: User) -> dict:
    return {
        "id": message.id,
        "conversation_id": message.conversation_id,
        "sender_id": message.sender_id,
        "sender_username": sender.username,
        "sender_is_bot": sender.is_bot,
        "content": message.content,
        "type": message.type,
        "created_at": iso(message.created_at),
    }


def member_ids(session: Session, conversation_id: int) -> list[int]:
    return list(
        session.exec(
            select(ConversationMember.user_id).where(
                ConversationMember.conversation_id == conversation_id
            )
        ).all()
    )


def is_member(session: Session, conversation_id: int, user_id: int) -> bool:
    return session.get(ConversationMember, (conversation_id, user_id)) is not None


def members(session: Session, conversation_id: int) -> list[User]:
    return list(
        session.exec(
            select(User)
            .join(ConversationMember, ConversationMember.user_id == User.id)
            .where(ConversationMember.conversation_id == conversation_id)
        ).all()
    )


def conversation_out(session: Session, conversation: Conversation, me: User) -> dict:
    people = members(session, conversation.id)
    others = [u for u in people if u.id != me.id]
    row = session.exec(
        select(Message, User)
        .join(User, Message.sender_id == User.id)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.id.desc())
        .limit(1)
    ).first()
    return {
        "id": conversation.id,
        "type": conversation.type,
        "title": conversation.name or ", ".join(u.username for u in others) or "Just you",
        "members": [user_out(u) for u in people],
        "last_message": message_out(*row) if row else None,
    }


def save_message(session: Session, conversation_id: int, sender: User, content: str) -> dict:
    message = Message(conversation_id=conversation_id, sender_id=sender.id, content=content)
    session.add(message)
    session.commit()
    session.refresh(message)
    return message_out(message, sender)


def ensure_bot_user(session: Session) -> User:
    """Create the AI bot account on first start. Nobody can log in as it."""
    bot = session.exec(select(User).where(User.username == BOT_USERNAME)).first()
    if bot is None:
        bot = User(username=BOT_USERNAME, password_hash="!", is_bot=True)
        session.add(bot)
        session.commit()
        session.refresh(bot)
    return bot
