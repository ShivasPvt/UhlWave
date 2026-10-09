"""Backing up your chats.

  GET /api/backup?media=false              everything you can see, as one JSON file
  GET /api/backup?media=true               the same, with photos, voice notes and files inside
  GET /api/conversations/{id}/export       one chat as a readable .txt file

Locked chats are only included if you've entered your PIN recently. The JSON
file can be opened again from Settings → Backup → "Open a backup", which shows
the chats read-only in the browser (nothing is uploaded).
"""
import base64
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlmodel import Session, col, select

from .auth import get_current_user
from .db import get_session
from .folders import is_unlocked, locked_ids, require_open
from .models import Attachment, Conversation, ConversationMember, Message, User
from .services import iso, is_member, members, not_expired

router = APIRouter(prefix="/api", tags=["backup"])

MAX_MEDIA_BYTES = 25 * 1024 * 1024  # stop adding files to a backup after this much


def _title(conversation: Conversation, people: list[User], me: User) -> str:
    if conversation.type == "group":
        return conversation.name or "Group"
    return ", ".join(u.username for u in people if u.id != me.id) or "Just you"


def _rows(session: Session, conversation_id: int):
    return session.exec(
        select(Message, User).join(User, Message.sender_id == User.id)
        .where(Message.conversation_id == conversation_id, not_expired())
        .order_by(col(Message.id))
    ).all()


@router.get("/backup")
def backup(
    request: Request,
    media: bool = Query(False),
    me: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    conversation_ids = list(session.exec(
        select(ConversationMember.conversation_id).where(ConversationMember.user_id == me.id)
    ).all())
    skipped_locked = 0
    if not is_unlocked(request, me):
        hidden = locked_ids(session, me.id)
        skipped_locked = len(hidden)
        conversation_ids = [c for c in conversation_ids if c not in hidden]

    media_used, media_skipped = 0, 0
    chats = []
    for conversation_id in sorted(conversation_ids):
        conversation = session.get(Conversation, conversation_id)
        people = members(session, conversation_id)
        messages = []
        for m, u in _rows(session, conversation_id):
            text = "" if m.deleted_at else m.content
            if conversation.type == "secret" and m.type != "system" and text:
                text = "[end-to-end encrypted: only readable in the app]"
            item = {"id": m.id, "at": iso(m.created_at), "from": u.username, "type": m.type, "text": text}
            if m.deleted_at:
                item["deleted"] = True
            if m.edited_at:
                item["edited_at"] = iso(m.edited_at)
            if m.latitude is not None and m.longitude is not None:
                item["location"] = {"latitude": m.latitude, "longitude": m.longitude}
            if m.media_url:
                item["media_url"] = m.media_url
            if m.attachment_id and not m.deleted_at:
                a = session.get(Attachment, m.attachment_id)
                if a:
                    item["file"] = {"name": a.filename, "type": a.content_type, "size": a.size}
                    if media and media_used + a.size <= MAX_MEDIA_BYTES:
                        item["file"]["data_base64"] = base64.b64encode(a.data).decode()
                        media_used += a.size
                    elif media:
                        media_skipped += 1
            messages.append(item)
        chats.append({
            "id": conversation.id, "type": conversation.type, "title": _title(conversation, people, me),
            "description": conversation.description, "members": [u.username for u in people], "messages": messages,
        })

    now = datetime.now(timezone.utc)
    data = {
        "app": "UhlWave", "format": 1, "exported_at": now.isoformat(), "account": me.username,
        "includes_media": media, "media_skipped_over_limit": media_skipped,
        "locked_chats_left_out": skipped_locked, "chats": chats,
    }
    filename = f"uhlwave-backup-{me.username}-{now:%Y-%m-%d}.json"
    return Response(
        content=json.dumps(data, ensure_ascii=False, indent=1),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@router.get("/conversations/{conversation_id}/export")
def export_chat(conversation_id: int, request: Request, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not is_member(session, conversation_id, me.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found.")
    require_open(session, request, me, conversation_id)
    conversation = session.get(Conversation, conversation_id)
    people = members(session, conversation_id)
    title = _title(conversation, people, me)
    lines = [f"UhlWave chat: {title}", f"Exported {datetime.now(timezone.utc):%d %b %Y %H:%M} UTC", ""]
    for m, u in _rows(session, conversation_id):
        when = m.created_at.strftime("%d/%m/%Y, %H:%M")
        if m.deleted_at:
            text = "This message was deleted"
        elif m.type in ("image", "voice", "file"):
            text = f"<{m.type}: {m.content}>"
        elif m.type == "location" and m.latitude is not None:
            text = f"<location: {m.content}> https://www.openstreetmap.org/?mlat={m.latitude}&mlon={m.longitude}"
        elif m.type in ("gif", "sticker"):
            text = f"<{m.type}: {m.content}>"
        elif conversation.type == "secret" and m.type != "system":
            text = "[end-to-end encrypted]"
        else:
            text = m.content
        lines.append(f"{when} - {text}" if m.type == "system" else f"{when} - {u.username}: {text}")
    safe = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in title)[:60] or "chat"
    return Response(
        content="\n".join(lines) + "\n",
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="UhlWave - {safe}.txt"', "Cache-Control": "no-store"},
    )
