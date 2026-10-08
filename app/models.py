"""Database tables.

A direct message (DM) is just a conversation with exactly two members, so
group chats later only need a new "type" value and a way to add members.
"""
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    __tablename__ = "users"  # "user" is a reserved word in Postgres

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True, max_length=30)
    password_hash: str
    is_bot: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime | None = None


class Conversation(SQLModel, table=True):
    __tablename__ = "conversations"

    id: int | None = Field(default=None, primary_key=True)
    type: str = "direct"  # "direct" now; add "group" as an extension
    name: str | None = None  # only used for groups
    created_at: datetime = Field(default_factory=utcnow)


class ConversationMember(SQLModel, table=True):
    __tablename__ = "conversation_members"

    conversation_id: int = Field(foreign_key="conversations.id", primary_key=True)
    user_id: int = Field(foreign_key="users.id", primary_key=True, index=True)
    # Ready for read receipts: store the newest message this member has seen.
    last_read_message_id: int | None = None
    joined_at: datetime = Field(default_factory=utcnow)


class Message(SQLModel, table=True):
    __tablename__ = "messages"

    id: int | None = Field(default=None, primary_key=True)
    conversation_id: int = Field(foreign_key="conversations.id", index=True)
    sender_id: int = Field(foreign_key="users.id")
    content: str = Field(max_length=4000)
    type: str = "text"  # later: "image", "voice", "file"
    created_at: datetime = Field(default_factory=utcnow, index=True)
