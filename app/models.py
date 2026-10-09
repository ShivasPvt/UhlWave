"""Database tables.

A direct message (DM) is a conversation with exactly two members; a group is
a conversation with type "group", a name, and any number of members.
"""
from datetime import datetime, timezone

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel

from .crypto import EncryptedBytes, EncryptedText


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    __tablename__ = "users"  # "user" is a reserved word in Postgres

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True, max_length=30)
    password_hash: str  # "!" for accounts that only sign in with Google
    is_bot: bool = False
    # Filled in for people who sign in with Google. `google_sub` is Google's
    # permanent id for the account; emails can change, this never does.
    email: str | None = Field(default=None, index=True, unique=True, max_length=254)
    google_sub: str | None = Field(default=None, index=True, unique=True, max_length=255)
    created_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime | None = None

    # ---- privacy & security settings (Settings panel) ----
    show_presence: bool = True       # let others see "Online" / "Last seen"
    send_typing: bool = True         # let others see "… is typing"
    send_read_receipts: bool = True  # let others see ✓✓ "Seen". Off = you don't see theirs either.
    disappear_seconds: int = 0       # new messages you send vanish after this long; 0 = never
    # Bumped by "Log out of all devices". Login tokens carry the version they
    # were made with, so every older token stops working at once.
    token_version: int = 0

    # ---- profile ----
    bio: str | None = Field(default=None, max_length=160)          # "About" line on your profile
    avatar_id: int | None = Field(default=None, foreign_key="attachments.id")  # profile photo
    # Private account: strangers must send a chat request, and you approve it first.
    is_private: bool = False
    # Locked chats: a PIN (bcrypt-hashed, like passwords) that opens the locked folder.
    lock_pin_hash: str | None = None
    # Wallpaper, bubble colours and font, as JSON (see app/appearance.py).
    appearance: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    # End-to-end encryption for secret chats (see static/e2ee.js). The public key is
    # shared with chat partners. The private key is encrypted in the browser with the
    # person's secret-chat passphrase before it's stored here, so the server can't use it.
    e2ee_public_key: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    e2ee_private_wrapped: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    e2ee_updated_at: datetime | None = None


class Conversation(SQLModel, table=True):
    __tablename__ = "conversations"

    id: int | None = Field(default=None, primary_key=True)
    type: str = "direct"  # "direct" or "group"
    name: str | None = Field(default=None, max_length=60)  # only used for groups
    created_by: int | None = Field(default=None, foreign_key="users.id")  # who made the group
    created_at: datetime = Field(default_factory=utcnow)
    description: str | None = Field(default=None, max_length=500)  # groups only
    avatar_id: int | None = Field(default=None, foreign_key="attachments.id")  # group photo
    # Secret part of the group's invite link (/join/<code>). None = no link.
    invite_code: str | None = Field(default=None, index=True, unique=True, max_length=40)


class ConversationMember(SQLModel, table=True):
    __tablename__ = "conversation_members"

    conversation_id: int = Field(foreign_key="conversations.id", primary_key=True)
    user_id: int = Field(foreign_key="users.id", primary_key=True, index=True)
    # Read receipts and unread counts: the newest message this member has seen.
    last_read_message_id: int | None = None
    joined_at: datetime = Field(default_factory=utcnow)
    # Groups: "admin" or "member". Ignored in one-to-one chats.
    role: str = Field(default="member", max_length=10)
    # Groups: a short label shown next to this person's name, e.g. "Class rep".
    tag: str | None = Field(default=None, max_length=30)
    # Only for this member's own chat list: folders and filters.
    archived: bool = False
    locked: bool = False
    category_id: int | None = Field(default=None, foreign_key="categories.id")


class Attachment(SQLModel, table=True):
    """An uploaded image, voice note or file. Stored in the database so it
    survives redeploys on hosts with throwaway disks (like Render's free tier)."""
    __tablename__ = "attachments"

    id: int | None = Field(default=None, primary_key=True)
    uploader_id: int = Field(foreign_key="users.id", index=True)
    # The name and bytes are encrypted at rest (see crypto.py).
    filename: str = Field(sa_column=Column(EncryptedText, nullable=False))
    content_type: str = Field(max_length=100)
    size: int
    data: bytes = Field(sa_column=Column(EncryptedBytes, nullable=False))
    # What the AI read from this file: a voice note's transcript or a document's
    # text. Saved so each file is only processed once. None = not read yet.
    ai_text: str | None = Field(default=None, sa_column=Column(EncryptedText, nullable=True))
    created_at: datetime = Field(default_factory=utcnow)


class Message(SQLModel, table=True):
    __tablename__ = "messages"

    id: int | None = Field(default=None, primary_key=True)
    conversation_id: int = Field(foreign_key="conversations.id", index=True)
    sender_id: int = Field(foreign_key="users.id")
    # Encrypted at rest (see crypto.py), so never filter on it in SQL. In secret
    # chats it's also end-to-end encrypted by the browsers before it gets here.
    content: str = Field(sa_column=Column(EncryptedText, nullable=False))
    # The message this one replies to, shown as a quote above it.
    reply_to_id: int | None = Field(default=None, foreign_key="messages.id")
    # "text", "image", "voice", "file", "location", "gif", "sticker", "call", "poll",
    # or "system" (a grey line like "riya added kabir")
    type: str = "text"
    attachment_id: int | None = Field(default=None, foreign_key="attachments.id")
    # For shared locations; the content holds the place name.
    latitude: float | None = None
    longitude: float | None = None
    created_at: datetime = Field(default_factory=utcnow, index=True)
    edited_at: datetime | None = None
    deleted_at: datetime | None = None  # "This message was deleted": content and attachment are wiped
    # Disappearing messages: hidden once this time passes, then deleted. UTC.
    expires_at: datetime | None = Field(default=None, index=True)
    # GIFs and stickers from GIPHY: the image address. Built-in stickers use the content instead.
    media_url: str | None = Field(default=None, max_length=500)
    # Pinned to the top of the chat (by pinned_by, at pinned_at).
    pinned_at: datetime | None = None
    pinned_by: int | None = Field(default=None, foreign_key="users.id")


class Reaction(SQLModel, table=True):
    """One emoji per person per message, like WhatsApp."""
    __tablename__ = "reactions"

    message_id: int = Field(foreign_key="messages.id", primary_key=True)
    user_id: int = Field(foreign_key="users.id", primary_key=True)
    emoji: str = Field(max_length=16)
    created_at: datetime = Field(default_factory=utcnow)


class Block(SQLModel, table=True):
    """`blocker_id` has blocked `blocked_id`. Neither can message the other."""
    __tablename__ = "blocks"

    blocker_id: int = Field(foreign_key="users.id", primary_key=True)
    blocked_id: int = Field(foreign_key="users.id", primary_key=True, index=True)
    created_at: datetime = Field(default_factory=utcnow)


class Category(SQLModel, table=True):
    """A folder someone made for their own chat list, like "Work" or "Hostel"."""
    __tablename__ = "categories"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    name: str = Field(max_length=24)
    created_at: datetime = Field(default_factory=utcnow)


class ChatRequest(SQLModel, table=True):
    """Someone asking to chat with a person who has a private account."""
    __tablename__ = "chat_requests"

    id: int | None = Field(default=None, primary_key=True)
    from_user_id: int = Field(foreign_key="users.id", index=True)
    to_user_id: int = Field(foreign_key="users.id", index=True)
    note: str | None = Field(default=None, max_length=200)  # optional "hi, it's riya from DBMS"
    status: str = Field(default="pending", max_length=10)   # "pending", "accepted" or "declined"
    created_at: datetime = Field(default_factory=utcnow)
    answered_at: datetime | None = None


class Report(SQLModel, table=True):
    """Reports about chats. Reporting has been switched off; the table is kept so
    databases that already have it stay consistent (accounts can still be deleted)."""
    __tablename__ = "reports"

    id: int | None = Field(default=None, primary_key=True)
    reporter_id: int = Field(foreign_key="users.id", index=True)
    conversation_id: int = Field(foreign_key="conversations.id")
    message_id: int | None = None
    reason: str = Field(max_length=40)
    details: str | None = Field(default=None, max_length=1000)
    emailed: bool = False
    created_at: datetime = Field(default_factory=utcnow, index=True)


class Poll(SQLModel, table=True):
    """A poll posted in a chat. The message (type "poll") carries the question."""
    __tablename__ = "polls"

    id: int | None = Field(default=None, primary_key=True)
    message_id: int = Field(foreign_key="messages.id", index=True, unique=True)
    multiple: bool = False   # can people pick more than one option?
    closed: bool = False     # the creator ended it; no more votes
    created_at: datetime = Field(default_factory=utcnow)


class PollOption(SQLModel, table=True):
    __tablename__ = "poll_options"

    id: int | None = Field(default=None, primary_key=True)
    poll_id: int = Field(foreign_key="polls.id", index=True)
    position: int = 0
    text: str = Field(sa_column=Column(EncryptedText, nullable=False))


class PollVote(SQLModel, table=True):
    __tablename__ = "poll_votes"

    poll_id: int = Field(foreign_key="polls.id", primary_key=True)
    option_id: int = Field(foreign_key="poll_options.id", primary_key=True)
    user_id: int = Field(foreign_key="users.id", primary_key=True, index=True)
    created_at: datetime = Field(default_factory=utcnow)
