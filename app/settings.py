"""The Settings panel: privacy, disappearing messages, blocked people and account.

Every route here acts on the logged-in user only.
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlmodel import Session, col, delete, select, update

from .auth import COOKIE_NAME, USERNAME_PATTERN, get_current_user, set_login_cookie
from .config import BOT_USERNAME
from .db import get_session
from .manager import manager
from .models import (
    Attachment, Block, Category, ChatRequest, Conversation, ConversationMember, Message, PollVote, Reaction, Report, User,
)
from .polls import delete_polls_for
from .services import avatar_url, user_out

router = APIRouter(prefix="/api/settings", tags=["settings"])

# The choices offered for disappearing messages, in seconds. 0 means off.
DISAPPEAR_CHOICES = (0, 5 * 60, 60 * 60, 24 * 60 * 60, 7 * 24 * 60 * 60)


def settings_out(session: Session, user: User) -> dict:
    blocked = session.exec(
        select(User)
        .join(Block, Block.blocked_id == User.id)
        .where(Block.blocker_id == user.id)
        .order_by(User.username)
    ).all()
    return {
        "username": user.username,
        "email": user.email,
        "sign_in_method": "google" if user.google_sub else "password",
        "has_password": user.password_hash != "!",
        "bio": user.bio,
        "avatar_url": avatar_url(user.avatar_id),
        "is_private": user.is_private,
        "has_lock_pin": bool(user.lock_pin_hash),
        "show_presence": user.show_presence,
        "send_typing": user.send_typing,
        "send_read_receipts": user.send_read_receipts,
        "disappear_seconds": user.disappear_seconds,
        "disappear_choices": list(DISAPPEAR_CHOICES),
        "blocked": [user_out(u) for u in blocked],
    }


@router.get("")
def get_settings(me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    return settings_out(session, me)


class SettingsPatch(BaseModel):
    is_private: bool | None = None
    show_presence: bool | None = None
    send_typing: bool | None = None
    send_read_receipts: bool | None = None
    disappear_seconds: int | None = None


@router.patch("")
async def update_settings(
    body: SettingsPatch, me: User = Depends(get_current_user), session: Session = Depends(get_session)
):
    if body.disappear_seconds is not None:
        if body.disappear_seconds not in DISAPPEAR_CHOICES:
            raise HTTPException(422, "Pick one of the offered timers.")
        me.disappear_seconds = body.disappear_seconds
    if body.send_typing is not None:
        me.send_typing = body.send_typing
    if body.send_read_receipts is not None:
        me.send_read_receipts = body.send_read_receipts
    if body.is_private is not None:
        me.is_private = body.is_private

    presence_changed = body.show_presence is not None and body.show_presence != me.show_presence
    if body.show_presence is not None:
        me.show_presence = body.show_presence
    session.add(me)
    session.commit()
    session.refresh(me)

    # Tell everyone straight away, so hiding your status takes effect without a reload.
    if presence_changed and manager.is_online(me.id):
        await manager.broadcast({"type": "presence", "user_id": me.id, "online": me.show_presence}, exclude=me.id)
    return settings_out(session, me)


class UsernameBody(BaseModel):
    username: str = Field(min_length=3, max_length=30, pattern=USERNAME_PATTERN)


@router.put("/username")
def change_username(body: UsernameBody, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    username = body.username.lower()
    if username == me.username:
        return settings_out(session, me)
    if username == BOT_USERNAME or session.exec(select(User).where(User.username == username)).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "That username is taken. Try another.")
    me.username = username
    session.add(me)
    session.commit()
    session.refresh(me)
    return settings_out(session, me)


# ---- blocking ----------------------------------------------------------------

class BlockBody(BaseModel):
    username: str


@router.post("/blocked")
def block_user(body: BlockBody, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    other = session.exec(select(User).where(User.username == body.username.lower())).first()
    if other is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No user with that name.")
    if other.id == me.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't block yourself.")
    if session.get(Block, (me.id, other.id)) is None:
        session.add(Block(blocker_id=me.id, blocked_id=other.id))
        session.commit()
    return settings_out(session, me)


@router.delete("/blocked/{user_id}")
def unblock_user(user_id: int, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    block = session.get(Block, (me.id, user_id))
    if block is not None:
        session.delete(block)
        session.commit()
    return settings_out(session, me)


# ---- account & security ------------------------------------------------------

@router.post("/logout-everywhere")
async def logout_everywhere(
    response: Response, me: User = Depends(get_current_user), session: Session = Depends(get_session)
):
    """Make every existing login token useless, then log this browser back in."""
    me.token_version += 1
    session.add(me)
    session.commit()
    session.refresh(me)
    # Drop live connections. Code 4000 = "reconnect": this browser comes back with its new
    # cookie, while other devices come back with a dead token and get sent to the login page.
    await manager.close_user(me.id, code=4000, reason="Logged out everywhere")
    set_login_cookie(response, me)
    return {"ok": True}


class DeleteAccountBody(BaseModel):
    confirm_username: str


@router.post("/delete-account")
async def delete_account(
    body: DeleteAccountBody,
    response: Response,
    me: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Delete the account and every message it sent. Can't be undone."""
    if body.confirm_username.strip().lower() != me.username:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Type your username exactly to confirm.")
    user_id = me.id
    my_messages = select(Message.id).where(col(Message.sender_id) == user_id)
    my_uploads = select(Attachment.id).where(col(Attachment.uploader_id) == user_id)
    session.execute(delete(Reaction).where(col(Reaction.user_id) == user_id))
    session.execute(delete(Reaction).where(col(Reaction.message_id).in_(my_messages)))
    # Things that point at this person or their uploads, which are about to go.
    session.execute(update(Message).where(col(Message.pinned_by) == user_id).values(pinned_by=None))
    session.execute(update(Message).where(col(Message.reply_to_id).in_(my_messages)).values(reply_to_id=None))
    session.execute(delete(PollVote).where(col(PollVote.user_id) == user_id))
    delete_polls_for(session, my_messages)
    session.execute(update(Message).where(col(Message.attachment_id).in_(my_uploads), col(Message.sender_id) != user_id).values(attachment_id=None))
    session.execute(update(Conversation).where(col(Conversation.avatar_id).in_(my_uploads)).values(avatar_id=None))
    session.execute(delete(ChatRequest).where((col(ChatRequest.from_user_id) == user_id) | (col(ChatRequest.to_user_id) == user_id)))
    session.execute(delete(Report).where(col(Report.reporter_id) == user_id))
    session.execute(update(ConversationMember).where(col(ConversationMember.user_id) == user_id).values(category_id=None))
    session.execute(delete(Category).where(col(Category.user_id) == user_id))
    me.avatar_id = None
    session.add(me)
    session.flush()
    session.execute(delete(Message).where(col(Message.sender_id) == user_id))
    session.execute(delete(Attachment).where(col(Attachment.uploader_id) == user_id))
    for group in session.exec(select(Conversation).where(Conversation.created_by == user_id)).all():
        group.created_by = None  # the group carries on without an admin
        session.add(group)
    session.execute(delete(ConversationMember).where(col(ConversationMember.user_id) == user_id))
    session.execute(delete(Block).where((col(Block.blocker_id) == user_id) | (col(Block.blocked_id) == user_id)))
    session.delete(me)
    session.commit()

    await manager.close_user(user_id, reason="Account deleted")
    await manager.broadcast({"type": "presence", "user_id": user_id, "online": False})
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}
