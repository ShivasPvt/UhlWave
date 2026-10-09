"""Profiles: a photo and an "About" line for each person, and photos for groups.

  GET    /api/users/{username}/profile   someone's profile card
  PUT    /api/settings/profile           change your About line: {"bio": "..."}
  POST   /api/settings/avatar            upload your profile photo (the image is the raw request body)
  DELETE /api/settings/avatar            remove it
  GET    /api/avatars/{id}               show a profile or group photo

Photos are stored in the database like other attachments, so they survive
redeploys on hosts with throwaway disks.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlmodel import Session, col, select

from .auth import get_current_user
from .chat_requests import direct_chat_between
from .db import get_session
from .manager import manager
from .models import Attachment, ChatRequest, Conversation, ConversationMember, User
from .services import has_blocked, user_out

router = APIRouter(prefix="/api", tags=["profiles"])

IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
MAX_AVATAR_BYTES = 3 * 1024 * 1024


async def read_image_upload(request: Request, max_bytes: int) -> tuple[bytes, str]:
    """The uploaded image and its type. Refuses anything that isn't a normal photo."""
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type not in IMAGE_TYPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Use a PNG, JPEG, WebP or GIF image.")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"Images can be up to {max_bytes // (1024 * 1024)} MB.")
        chunks.append(chunk)
    if size == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That image is empty.")
    return b"".join(chunks), content_type


def save_image(session: Session, uploader: User, data: bytes, content_type: str, filename: str) -> Attachment:
    attachment = Attachment(uploader_id=uploader.id, filename=filename, content_type=content_type, size=len(data), data=data)
    session.add(attachment)
    session.commit()
    session.refresh(attachment)
    return attachment


def delete_attachment(session: Session, attachment_id: int | None) -> None:
    if attachment_id:
        old = session.get(Attachment, attachment_id)
        if old is not None:
            session.delete(old)


def serve_image(attachment: Attachment, cache: str = "private, max-age=86400") -> Response:
    return Response(
        content=attachment.data,
        media_type=attachment.content_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; img-src 'self'; sandbox",
            "Cache-Control": cache,
        },
    )


# ---- profiles ----------------------------------------------------------------

@router.get("/users/{username}/profile")
def profile(username: str, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    user = session.exec(select(User).where(User.username == username.lower())).first()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No user with that name.")
    my_groups = select(ConversationMember.conversation_id).where(ConversationMember.user_id == me.id)
    common = session.exec(
        select(Conversation.name)
        .join(ConversationMember, ConversationMember.conversation_id == Conversation.id)
        .where(Conversation.type == "group", ConversationMember.user_id == user.id, col(Conversation.id).in_(my_groups))
        .order_by(Conversation.name)
    ).all() if user.id != me.id else []
    chat = direct_chat_between(session, me.id, user.id) if user.id != me.id else None
    pending = session.exec(select(ChatRequest).where(
        ChatRequest.from_user_id == me.id, ChatRequest.to_user_id == user.id, ChatRequest.status == "pending",
    )).first() if user.id != me.id else None
    return {
        **user_out(user),
        "bio": user.bio,
        "groups_in_common": list(common),
        "conversation_id": chat.id if chat else None,
        "request_pending": pending is not None,
        "blocked_by_me": has_blocked(session, me.id, user.id),
        "is_me": user.id == me.id,
    }


class ProfileBody(BaseModel):
    bio: str | None = Field(default=None, max_length=160)


@router.put("/settings/profile")
def update_profile(body: ProfileBody, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    me.bio = (body.bio or "").strip() or None
    session.add(me)
    session.commit()
    return {"bio": me.bio}


async def _tell_contacts(session: Session, user: User) -> None:
    """Everyone who shares a chat with this person reloads their list, so the new photo shows."""
    mine = select(ConversationMember.conversation_id).where(ConversationMember.user_id == user.id)
    people = set(session.exec(
        select(ConversationMember.user_id).where(col(ConversationMember.conversation_id).in_(mine))
    ).all())
    await manager.send_to_users(people, {"type": "conversations_changed"})


@router.post("/settings/avatar")
async def upload_avatar(request: Request, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    data, content_type = await read_image_upload(request, MAX_AVATAR_BYTES)
    old = me.avatar_id
    attachment = save_image(session, me, data, content_type, "profile-photo")
    me.avatar_id = attachment.id
    session.add(me)
    delete_attachment(session, old)
    session.commit()
    await _tell_contacts(session, me)
    return user_out(me)


@router.delete("/settings/avatar")
async def remove_avatar(me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    old = me.avatar_id
    me.avatar_id = None
    session.add(me)
    delete_attachment(session, old)
    session.commit()
    await _tell_contacts(session, me)
    return user_out(me)


@router.get("/avatars/{attachment_id}")
def show_avatar(attachment_id: int, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    # Only serve attachments that really are profile or group photos, never chat files.
    is_avatar = session.exec(select(User.id).where(User.avatar_id == attachment_id)).first() is not None
    group = session.exec(select(Conversation).where(Conversation.avatar_id == attachment_id)).first()
    if not is_avatar and group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Photo not found.")
    attachment = session.get(Attachment, attachment_id)
    if attachment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Photo not found.")
    return serve_image(attachment)
