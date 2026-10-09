"""Everything you can do to a message after it's sent, plus attachments and search.

  POST   /api/conversations/{id}/attachments   upload an image, voice note or file (raw body)
  GET    /api/attachments/{id}                 download / show one
  PATCH  /api/messages/{id}                    edit your own text message
  DELETE /api/messages/{id}                    delete your own message for everyone
  POST   /api/messages/{id}/reactions          react with an emoji (same emoji again removes it)
  POST   /api/messages/{id}/pin                pin to the top of the chat (up to 3 per chat)
  DELETE /api/messages/{id}/pin                unpin
  POST   /api/conversations/{id}/sticker       send a GIF or sticker
  GET    /api/search/messages?q=               search your chats
  GET    /api/conversations/{id}/search?q=     search inside one chat
  GET    /api/conversations/{id}/media?kind=   photos, documents, links or voice notes in one chat

Changes are pushed to everyone in the chat as a "message_updated" WebSocket event.
"""
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlmodel import Session, col, delete, func, select

from .auth import get_current_user
from .db import engine, get_session
from .folders import is_unlocked, locked_ids, require_open
from .manager import manager
from .models import Attachment, Conversation, ConversationMember, Message, Reaction, User, utcnow
from .tools import reverse_geocode, search_places
from .ws import is_ai_chat, start_ai_reply
from .services import (
    is_member, load_message_out, member_ids, messages_out, not_expired, pinned_messages, save_message,
    send_problem, system_message,
)

router = APIRouter(prefix="/api", tags=["messages"])

# How many recent messages a search looks through. Text is encrypted in the
# database, so searching means decrypting; this keeps it quick.
SEARCH_SCAN = 5000

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB: files live in the database, so keep them small
INLINE_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}  # no SVG: it can carry scripts
AUDIO_TYPES = {"audio/webm", "audio/ogg", "audio/mpeg", "audio/mp4", "audio/wav", "audio/x-wav", "audio/aac"}
REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "🙏"]


def _base_type(content_type: str | None) -> str:
    return (content_type or "application/octet-stream").split(";")[0].strip().lower()


def _clean_filename(name: str | None) -> str:
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return name[:200] or "file"


async def _push_update(conversation_id: int, message: dict) -> None:
    with Session(engine) as session:
        people = member_ids(session, conversation_id)
    await manager.send_to_users(people, {"type": "message_updated", "message": message})


def _not_secret(session: Session, conversation_id: int) -> None:
    """Secret chats only carry end-to-end encrypted text: the server can't encrypt
    files, places or GIFs for the two browsers, so it refuses them."""
    conversation = session.get(Conversation, conversation_id)
    if conversation is not None and conversation.type == "secret":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Secret chats are for end-to-end encrypted text only.")


def _visible_message(session: Session, message_id: int, me: User) -> Message:
    """A message in one of your chats that hasn't disappeared. 404 otherwise."""
    message = session.get(Message, message_id)
    if message is None or not is_member(session, message.conversation_id, me.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found.")
    if message.expires_at is not None and message.expires_at.replace(tzinfo=None) <= utcnow().replace(tzinfo=None):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Message not found.")
    return message


# ---- attachments -------------------------------------------------------------

@router.post("/conversations/{conversation_id}/attachments", status_code=status.HTTP_201_CREATED)
async def upload_attachment(
    conversation_id: int,
    request: Request,
    filename: str = Query("file", max_length=300),
    kind: str = Query("auto", pattern="^(auto|voice)$"),
    me: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Send the file as the raw request body, with its Content-Type header set."""
    problem = send_problem(session, conversation_id, me.id)
    if problem:
        not_in_chat = problem.startswith("You're not in")
        raise HTTPException(status.HTTP_404_NOT_FOUND if not_in_chat else status.HTTP_403_FORBIDDEN, problem)
    _not_secret(session, conversation_id)

    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Files can be up to 5 MB.")
        chunks.append(chunk)
    if size == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That file is empty.")

    content_type = _base_type(request.headers.get("content-type"))
    if kind == "voice":
        if content_type not in AUDIO_TYPES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Voice notes must be audio.")
        message_type = "voice"
    elif content_type in INLINE_IMAGE_TYPES:
        message_type = "image"
    else:
        message_type = "file"

    name = _clean_filename(filename)
    attachment = Attachment(uploader_id=me.id, filename=name, content_type=content_type, size=size, data=b"".join(chunks))
    session.add(attachment)
    session.commit()
    session.refresh(attachment)

    out = save_message(session, conversation_id, me, name, type=message_type, attachment_id=attachment.id)
    people = member_ids(session, conversation_id)
    talking_to_ai = is_ai_chat(session, conversation_id)
    await manager.send_to_users(people, {"type": "message", "message": out, "client_id": request.headers.get("x-client-id")})
    # Send a photo, voice note or file straight to @ai and it answers, just like a text message.
    if talking_to_ai:
        start_ai_reply(conversation_id, me.disappear_seconds)
    return out


@router.get("/attachments/{attachment_id}")
def download_attachment(attachment_id: int, request: Request, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    message = session.exec(select(Message).where(Message.attachment_id == attachment_id)).first()
    attachment = session.get(Attachment, attachment_id)
    if message is None or attachment is None or message.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found.")
    _visible_message(session, message.id, me)  # checks you're in that chat and it hasn't expired
    require_open(session, request, me, message.conversation_id)

    inline = attachment.content_type in INLINE_IMAGE_TYPES or attachment.content_type in AUDIO_TYPES
    media_type = attachment.content_type if inline else "application/octet-stream"
    disposition = "inline" if inline else "attachment"
    return Response(
        content=attachment.data,
        media_type=media_type,
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(attachment.filename)}",
            "X-Content-Type-Options": "nosniff",
            # Even if someone uploads something nasty, the browser won't run it from here.
            "Content-Security-Policy": "default-src 'none'; media-src 'self'; img-src 'self'; sandbox",
            "Cache-Control": "private, max-age=3600",
        },
    )


# ---- sharing your location -------------------------------------------------------

class LocationBody(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    # Set when the place was picked on the map or found by name, e.g. "Library, IIT Mandi".
    label: str | None = Field(default=None, max_length=300)


@router.post("/conversations/{conversation_id}/location", status_code=status.HTTP_201_CREATED)
async def share_location(
    conversation_id: int, body: LocationBody,
    me: User = Depends(get_current_user), session: Session = Depends(get_session),
):
    """Post "I'm here" as a message. The browser asks the person's permission first;
    nothing is shared unless they tap the button."""
    problem = send_problem(session, conversation_id, me.id)
    if problem:
        not_in_chat = problem.startswith("You're not in")
        raise HTTPException(status.HTTP_404_NOT_FOUND if not_in_chat else status.HTTP_403_FORBIDDEN, problem)
    _not_secret(session, conversation_id)
    lat, lon = round(body.latitude, 5), round(body.longitude, 5)  # about 1 m; more is false precision
    label = (body.label or "").strip() or await reverse_geocode(lat, lon) or "Shared location"
    out = save_message(session, conversation_id, me, label[:300], type="location", latitude=lat, longitude=lon)
    people = member_ids(session, conversation_id)
    talking_to_ai = is_ai_chat(session, conversation_id)
    await manager.send_to_users(people, {"type": "message", "message": out, "client_id": None})
    if talking_to_ai:
        start_ai_reply(conversation_id, me.disappear_seconds)
    return out


@router.get("/places")
async def find_places(
    q: str = Query(..., min_length=2, max_length=120),
    lat: float | None = Query(None, ge=-90, le=90),
    lon: float | None = Query(None, ge=-180, le=180),
    me: User = Depends(get_current_user),
):
    """Search for a place to share, when the browser can't find your location (or you want somewhere else)."""
    try:
        return await search_places(q.strip(), (lat, lon) if lat is not None and lon is not None else None)
    except Exception:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Place search isn't responding. Try again in a moment.")


@router.get("/places/name")
async def name_place(
    lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180),
    me: User = Depends(get_current_user),
):
    """A readable name for a point on the map."""
    return {"label": await reverse_geocode(lat, lon)}


# ---- edit, delete, react -----------------------------------------------------

class EditBody(BaseModel):
    content: str = Field(min_length=1, max_length=16000)  # secret chats send longer, encrypted text


@router.patch("/messages/{message_id}")
async def edit_message(message_id: int, body: EditBody, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    message = _visible_message(session, message_id, me)
    if message.sender_id != me.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can only edit your own messages.")
    if message.type != "text" or message.deleted_at is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only text messages can be edited.")
    content = body.content.strip()
    if not content:
        raise HTTPException(422, "A message can't be empty.")
    secret = session.get(Conversation, message.conversation_id).type == "secret"
    if secret and not content.startswith("e2e1:"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Secret chats only accept end-to-end encrypted messages.")
    if not secret and len(content) > 4000:
        raise HTTPException(422, "Message is too long.")
    if content != message.content:
        message.content = content
        message.edited_at = utcnow()
        session.add(message)
        session.commit()
    out = load_message_out(session, message.id)
    await _push_update(message.conversation_id, out)
    return out


@router.delete("/messages/{message_id}")
async def delete_message(message_id: int, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Delete for everyone. The bubble stays as "This message was deleted"."""
    message = _visible_message(session, message_id, me)
    if message.sender_id != me.id or message.type == "system":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can only delete your own messages.")
    if message.deleted_at is None:
        attachment_id = message.attachment_id
        message.deleted_at = utcnow()
        message.content = ""
        message.attachment_id = None
        message.latitude = message.longitude = None
        session.add(message)
        session.execute(delete(Reaction).where(col(Reaction.message_id) == message.id))
        if attachment_id:
            session.execute(delete(Attachment).where(col(Attachment.id) == attachment_id))
        session.commit()
    out = load_message_out(session, message.id)
    await _push_update(message.conversation_id, out)
    return out


class ReactBody(BaseModel):
    emoji: str


@router.post("/messages/{message_id}/reactions")
async def react(message_id: int, body: ReactBody, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    message = _visible_message(session, message_id, me)
    if body.emoji not in REACTIONS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pick one of the offered reactions.")
    if message.deleted_at is not None or message.type == "system":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't react to that.")
    problem = send_problem(session, message.conversation_id, me.id)
    if problem:
        raise HTTPException(status.HTTP_403_FORBIDDEN, problem)

    existing = session.get(Reaction, (message.id, me.id))
    if existing and existing.emoji == body.emoji:
        session.delete(existing)  # same emoji again = take it back
    elif existing:
        existing.emoji = body.emoji
        existing.created_at = utcnow()
        session.add(existing)
    else:
        session.add(Reaction(message_id=message.id, user_id=me.id, emoji=body.emoji))
    session.commit()
    out = load_message_out(session, message.id)
    await _push_update(message.conversation_id, out)
    return out


# ---- search ------------------------------------------------------------------

@router.get("/search/messages")
def search_messages(
    request: Request,
    q: str = Query(..., min_length=2, max_length=100),
    me: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Newest-first matches across every chat you're in (text messages only).
    Locked chats are only searched after you've entered your PIN."""
    mine = select(ConversationMember.conversation_id).where(ConversationMember.user_id == me.id)
    if not is_unlocked(request, me):
        mine = mine.where(col(ConversationMember.locked).is_(False))
    # Text is encrypted in the database, so look through recent messages here instead.
    # Secret chats are skipped: only the two browsers can read those.
    secret = select(Conversation.id).where(Conversation.type == "secret")
    candidates = session.exec(
        select(Message, User)
        .join(User, Message.sender_id == User.id)
        .where(
            col(Message.conversation_id).in_(mine),
            col(Message.conversation_id).not_in(secret),
            Message.type == "text",
            col(Message.deleted_at).is_(None),
            not_expired(),
        )
        .order_by(Message.id.desc())
        .limit(SEARCH_SCAN)
    ).all()
    rows = _matching(candidates, q)[:30]

    titles: dict[int, str] = {}
    for conversation_id in {m.conversation_id for m, _ in rows}:
        conversation = session.get(Conversation, conversation_id)
        if conversation.type == "group":
            titles[conversation_id] = conversation.name
        else:
            others = session.exec(
                select(User.username)
                .join(ConversationMember, ConversationMember.user_id == User.id)
                .where(ConversationMember.conversation_id == conversation_id, User.id != me.id)
            ).all()
            titles[conversation_id] = ", ".join(others) or "Just you"
    return [{**m, "conversation_title": titles[m["conversation_id"]]} for m in messages_out(session, list(rows))]


@router.get("/reactions")
def reaction_choices():
    return REACTIONS


# ---- pinning ---------------------------------------------------------------------

MAX_PINS = 3


async def _push_pins(session: Session, conversation_id: int) -> None:
    await manager.send_to_users(member_ids(session, conversation_id), {
        "type": "pins_changed", "conversation_id": conversation_id, "pinned": pinned_messages(session, conversation_id),
    })


@router.post("/messages/{message_id}/pin")
async def pin_message(message_id: int, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Pin a message for everyone in the chat. A fourth pin replaces the oldest."""
    message = _visible_message(session, message_id, me)
    if message.deleted_at is not None or message.type == "system":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't pin that.")
    problem = send_problem(session, message.conversation_id, me.id)
    if problem:
        raise HTTPException(status.HTTP_403_FORBIDDEN, problem)
    if message.pinned_at is None:
        pinned = session.exec(
            select(Message).where(Message.conversation_id == message.conversation_id, col(Message.pinned_at).is_not(None))
            .order_by(col(Message.pinned_at))
        ).all()
        for old in pinned[: max(0, len(pinned) - MAX_PINS + 1)]:
            old.pinned_at, old.pinned_by = None, None
            session.add(old)
        message.pinned_at, message.pinned_by = utcnow(), me.id
        session.add(message)
        session.commit()
        line = system_message(session, message.conversation_id, me, f"{me.username} pinned a message")
        await manager.send_to_users(member_ids(session, message.conversation_id), {"type": "message", "message": line, "client_id": None})
    await _push_update(message.conversation_id, load_message_out(session, message.id))
    await _push_pins(session, message.conversation_id)
    return pinned_messages(session, message.conversation_id)


@router.delete("/messages/{message_id}/pin")
async def unpin_message(message_id: int, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    message = _visible_message(session, message_id, me)
    if message.pinned_at is not None:
        message.pinned_at, message.pinned_by = None, None
        session.add(message)
        session.commit()
    await _push_update(message.conversation_id, load_message_out(session, message.id))
    await _push_pins(session, message.conversation_id)
    return pinned_messages(session, message.conversation_id)


# ---- search and media inside one chat ---------------------------------------------

def _open_chat(session: Session, request: Request, me: User, conversation_id: int) -> None:
    if not is_member(session, conversation_id, me.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found.")
    require_open(session, request, me, conversation_id)


@router.get("/conversations/{conversation_id}/search")
def search_in_chat(
    conversation_id: int, request: Request,
    q: str = Query(..., min_length=1, max_length=100),
    me: User = Depends(get_current_user), session: Session = Depends(get_session),
):
    """Matches in one chat, newest first: message text, file names, places and GIF titles."""
    _open_chat(session, request, me, conversation_id)
    conversation = session.get(Conversation, conversation_id)
    if conversation.type == "secret":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Secret chats are searched on your device, not on the server.")
    candidates = session.exec(
        select(Message, User).join(User, Message.sender_id == User.id)
        .where(
            Message.conversation_id == conversation_id, Message.type != "system",
            col(Message.deleted_at).is_(None), not_expired(),
        )
        .order_by(Message.id.desc()).limit(SEARCH_SCAN)
    ).all()
    return messages_out(session, _matching(candidates, q)[:100])


def _matching(rows, q: str) -> list:
    """Messages whose text contains q, ignoring case. Done in Python because the
    database only holds the encrypted text."""
    needle = q.strip().lower()
    return [(m, u) for m, u in rows if needle in (m.content or "").lower()]


LINK = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
MEDIA_KINDS = {
    "photos": ("image", "gif", "sticker"),
    "docs": ("file",),
    "voice": ("voice",),
    "links": ("text",),
    "places": ("location",),
}


@router.get("/conversations/{conversation_id}/media")
def chat_media(
    conversation_id: int, request: Request,
    kind: str = Query("photos", pattern="^(photos|docs|voice|links|places)$"),
    me: User = Depends(get_current_user), session: Session = Depends(get_session),
):
    """Everything of one kind that was shared in a chat, newest first."""
    _open_chat(session, request, me, conversation_id)
    query = (
        select(Message, User).join(User, Message.sender_id == User.id)
        .where(
            Message.conversation_id == conversation_id, col(Message.type).in_(MEDIA_KINDS[kind]),
            col(Message.deleted_at).is_(None), not_expired(),
        )
    )
    rows = session.exec(query.order_by(Message.id.desc()).limit(SEARCH_SCAN if kind == "links" else 200)).all()
    if kind == "links":
        rows = [(m, u) for m, u in rows if "http" in m.content.lower()][:200]
    out = messages_out(session, list(rows))
    if kind == "links":
        for m in out:
            m["links"] = LINK.findall(m["content"])
        out = [m for m in out if m["links"]]
    return out


# ---- GIFs and stickers --------------------------------------------------------------

# Built-in stickers: big emoji that work even without a GIF key.
STICKERS = [
    "😂", "🥹", "😍", "🥳", "😎", "🤯", "😭", "😤", "🫡", "🤝", "🙌", "👏",
    "🔥", "💯", "✨", "🎉", "❤️", "💔", "👀", "🤔", "😴", "☕", "📚", "🏔️",
]


class StickerBody(BaseModel):
    kind: str = Field(pattern="^(gif|sticker)$")
    url: str | None = Field(default=None, max_length=500)    # a GIPHY image address
    emoji: str | None = Field(default=None, max_length=16)   # a built-in sticker
    title: str | None = Field(default=None, max_length=120)


def _giphy_url(url: str) -> bool:
    """Only accept images hosted by GIPHY, so nobody can make everyone's browser load an arbitrary site."""
    from urllib.parse import urlparse

    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (host == "giphy.com" or host.endswith(".giphy.com"))


@router.post("/conversations/{conversation_id}/sticker", status_code=status.HTTP_201_CREATED)
async def send_sticker(
    conversation_id: int, body: StickerBody, request: Request,
    me: User = Depends(get_current_user), session: Session = Depends(get_session),
):
    problem = send_problem(session, conversation_id, me.id)
    if problem:
        not_in_chat = problem.startswith("You're not in")
        raise HTTPException(status.HTTP_404_NOT_FOUND if not_in_chat else status.HTTP_403_FORBIDDEN, problem)
    _not_secret(session, conversation_id)
    if body.url:
        if not _giphy_url(body.url):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "That GIF address isn't from GIPHY.")
        title = (body.title or "").strip() or ("GIF" if body.kind == "gif" else "Sticker")
        out = save_message(session, conversation_id, me, title[:120], type=body.kind, media_url=body.url)
    elif body.kind == "sticker" and body.emoji in STICKERS:
        out = save_message(session, conversation_id, me, body.emoji, type="sticker")
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pick a GIF or one of the stickers.")
    people = member_ids(session, conversation_id)
    talking_to_ai = is_ai_chat(session, conversation_id)
    await manager.send_to_users(people, {"type": "message", "message": out, "client_id": request.headers.get("x-client-id")})
    if talking_to_ai:
        start_ai_reply(conversation_id, me.disappear_seconds)
    return out


@router.get("/stickers")
def sticker_choices():
    return STICKERS
