"""The real-time part. Every logged-in browser keeps one WebSocket open to /ws.

Messages are small JSON objects with a "type" field.

Browser -> server
  {"type": "message", "conversation_id": 7, "content": "hi", "client_id": "tmp-123", "reply_to_id": 41}
  {"type": "typing",  "conversation_id": 7, "is_typing": true}
  {"type": "read",    "conversation_id": 7, "message_id": 120}
  {"type": "call_invite" | "call_accept" | "call_decline" | "call_signal" | "call_end", ...}   see calls.py

A message starting with /imagine makes @ai draw a picture (see imagegen.py).

Server -> browser
  {"type": "message",  "message": {...}, "client_id": "tmp-123"}
  {"type": "message_updated", "message": {...}}          edited, deleted or reacted to
  {"type": "typing",   "conversation_id": 7, "user_id": 2, "username": "riya", "is_typing": true}
  {"type": "read",     "conversation_id": 7, "user_id": 2, "message_id": 120}
  {"type": "presence", "user_id": 2, "online": false}
  {"type": "conversations_changed"}                      a group you're in changed; reload the list
  {"type": "pins_changed", "conversation_id": 7, "pinned": [...]}
  {"type": "requests_changed"}                           a chat request to or from you changed
  {"type": "call_*", ...}                                calls (see calls.py)
  {"type": "error",    "detail": "..."}
"""
import asyncio
import json
import re

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlmodel import Session, select

from .auth import COOKIE_NAME, user_from_token
from .ai_media import prepare_for_ai
from .bot import generate_reply
from . import calls
from .config import AI_CONTEXT_MESSAGES
from .db import engine
from .imagegen import ImageGenError, check_quota, generate_image, image_quota_key, made_images
from .manager import manager
from .models import Attachment, Conversation, ConversationMember, Message, User, utcnow
from .services import (
    blocked_either_way, ensure_bot_user, is_member, member_ids, members,
    messages_out, not_expired, save_message, send_problem,
)

router = APIRouter()

MAX_MESSAGE_LENGTH = 4000
# Secret chats carry end-to-end encrypted text (see static/e2ee.js): "e2e1:" plus
# base64, so it's longer than the message it hides.
E2E_PREFIX = "e2e1:"
MAX_E2E_LENGTH = 16000
MENTION_AI = re.compile(r"(^|\W)@ai\b", re.IGNORECASE)
IMAGINE = re.compile(r"^/imagine\b\s*(.*)$", re.IGNORECASE | re.DOTALL)

# Keep references to running AI replies so Python doesn't garbage-collect them mid-flight.
_background_tasks: set[asyncio.Task] = set()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # 1. Who is this? The login cookie comes along with the WebSocket handshake.
    with Session(engine) as session:
        user = user_from_token(websocket.cookies.get(COOKIE_NAME), session)
    await websocket.accept()
    if user is None:
        await websocket.close(code=4401, reason="Not logged in")
        return

    user_id, username = user.id, user.username

    # 2. Register the socket and tell everyone if this person just came online
    #    (unless they've hidden their online status in Settings).
    if manager.connect(user_id, websocket) and user.show_presence:
        await manager.broadcast({"type": "presence", "user_id": user_id, "online": True}, exclude=user_id)

    # 3. Handle incoming events until the browser disconnects.
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                event = json.loads(raw)
                if not isinstance(event, dict):
                    raise ValueError
            except ValueError:
                await websocket.send_json({"type": "error", "detail": "Send JSON objects only."})
                continue

            kind = event.get("type")
            if kind == "message":
                await handle_message(user_id, event, websocket)
            elif kind == "typing":
                await handle_typing(user_id, username, event)
            elif kind == "read":
                await handle_read(user_id, event)
            elif isinstance(kind, str) and kind.startswith("call_"):
                await calls.handle_event(user_id, username, event, websocket)
            else:
                await websocket.send_json({"type": "error", "detail": f"Unknown event type: {kind!r}"})
    except WebSocketDisconnect:
        pass
    finally:
        # 4. Clean up. If that was their last open tab, they're now offline.
        gone = manager.disconnect(user_id, websocket)
        await calls.on_disconnect(user_id, websocket, fully_offline=gone)
        if gone:
            announce = False
            with Session(engine) as session:
                db_user = session.get(User, user_id)
                if db_user:  # None if they just deleted their account
                    db_user.last_seen_at = utcnow()
                    session.add(db_user)
                    session.commit()
                    announce = db_user.show_presence
            if announce:
                await manager.broadcast({"type": "presence", "user_id": user_id, "online": False})


async def handle_message(user_id: int, event: dict, websocket: WebSocket) -> None:
    conversation_id = event.get("conversation_id")
    content = event.get("content")
    client_id = event.get("client_id")
    reply_to_id = event.get("reply_to_id")

    if not isinstance(conversation_id, int) or not isinstance(content, str):
        await websocket.send_json({"type": "error", "detail": "Message needs conversation_id and content."})
        return
    if reply_to_id is not None and not isinstance(reply_to_id, int):
        reply_to_id = None
    content = content.strip()
    if not content:
        return

    with Session(engine) as session:
        # Never trust the browser: check this person really is in the conversation (and isn't blocked).
        problem = send_problem(session, conversation_id, user_id)
        if problem:
            await websocket.send_json({"type": "error", "detail": problem, "client_id": client_id})
            return
        conversation = session.get(Conversation, conversation_id)
        secret = conversation.type == "secret"
        if secret and not content.startswith(E2E_PREFIX):
            await websocket.send_json({"type": "error", "detail": "This secret chat only accepts end-to-end encrypted messages. Unlock secret chats and try again.", "client_id": client_id})
            return
        if len(content) > (MAX_E2E_LENGTH if secret else MAX_MESSAGE_LENGTH):
            await websocket.send_json({"type": "error", "detail": "Message is too long.", "client_id": client_id})
            return
        if reply_to_id is not None:
            target = session.get(Message, reply_to_id)
            if target is None or target.conversation_id != conversation_id:
                reply_to_id = None
        recipients = member_ids(session, conversation_id)
        sender = session.get(User, user_id)
        disappear_seconds = sender.disappear_seconds
        # Save first, then deliver. If it's on screen, it's in the database.
        out = save_message(session, conversation_id, sender, content, reply_to_id=reply_to_id)
        imagine = None if secret else IMAGINE.match(content)
        should_ai_reply = not secret and not imagine and wants_ai_reply(session, conversation_id, content)

    await manager.send_to_users(recipients, {"type": "message", "message": out, "client_id": client_id})

    if imagine:
        _run_in_background(imagine_reply(conversation_id, imagine.group(1), user_id, disappear_seconds))
    elif should_ai_reply:
        # The AI's answer disappears on the same timer as the message that asked it.
        start_ai_reply(conversation_id, disappear_seconds)


async def handle_typing(user_id: int, username: str, event: dict) -> None:
    conversation_id = event.get("conversation_id")
    if not isinstance(conversation_id, int):
        return
    with Session(engine) as session:
        if not is_member(session, conversation_id, user_id):
            return
        sender = session.get(User, user_id)
        if sender is None or not sender.send_typing:  # "Send typing indicators" is off
            return
        others = [
            uid for uid in member_ids(session, conversation_id)
            if uid != user_id and not blocked_either_way(session, user_id, uid)
        ]
    await manager.send_to_users(others, {
        "type": "typing",
        "conversation_id": conversation_id,
        "user_id": user_id,
        "username": username,
        "is_typing": bool(event.get("is_typing")),
    })


async def handle_read(user_id: int, event: dict) -> None:
    """The browser has shown this person everything up to message_id."""
    conversation_id, message_id = event.get("conversation_id"), event.get("message_id")
    if not isinstance(conversation_id, int) or not isinstance(message_id, int):
        return
    with Session(engine) as session:
        membership = session.get(ConversationMember, (conversation_id, user_id))
        message = session.get(Message, message_id)
        if membership is None or message is None or message.conversation_id != conversation_id:
            return
        if (membership.last_read_message_id or 0) >= message_id:
            return  # nothing new
        membership.last_read_message_id = message_id
        session.add(membership)
        session.commit()
        reader = session.get(User, user_id)
        if not reader.send_read_receipts:
            return  # saved for your own unread count, but nobody is told
        # Only people who share their own receipts get to see others'.
        audience = [
            u.id for u in members(session, conversation_id)
            if u.id != user_id and not u.is_bot and u.send_read_receipts
        ]
    await manager.send_to_users(audience, {
        "type": "read", "conversation_id": conversation_id, "user_id": user_id, "message_id": message_id,
    })


def _run_in_background(coroutine) -> None:
    task = asyncio.create_task(coroutine)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def start_ai_reply(conversation_id: int, disappear_seconds: int = 0) -> None:
    """Let the AI answer in the background, so the chat never waits on it."""
    _run_in_background(ai_reply(conversation_id, disappear_seconds))


async def post_bot_image(conversation_id: int, prompt: str, data: bytes, content_type: str, disappear_seconds: int = 0) -> None:
    """Post a picture @ai made, as a normal photo message from @ai."""
    extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(content_type, "png")
    with Session(engine) as session:
        bot = ensure_bot_user(session)
        attachment = Attachment(uploader_id=bot.id, filename=f"ai-image.{extension}", content_type=content_type, size=len(data), data=data)
        session.add(attachment)
        session.commit()
        session.refresh(attachment)
        out = save_message(session, conversation_id, bot, prompt[:4000] or "Picture", disappear_seconds, type="image", attachment_id=attachment.id)
        recipients = member_ids(session, conversation_id)
    await manager.send_to_users(recipients, {"type": "message", "message": out, "client_id": None})


async def _bot_says(conversation_id: int, text: str, disappear_seconds: int = 0) -> None:
    with Session(engine) as session:
        bot = ensure_bot_user(session)
        out = save_message(session, conversation_id, bot, text, disappear_seconds)
        recipients = member_ids(session, conversation_id)
    await manager.send_to_users(recipients, {"type": "message", "message": out, "client_id": None})


async def imagine_reply(conversation_id: int, prompt: str, user_id: int, disappear_seconds: int = 0) -> None:
    """/imagine <description>: @ai draws it."""
    with Session(engine) as session:
        bot = ensure_bot_user(session)
        bot_id, bot_name = bot.id, bot.username
        recipients = member_ids(session, conversation_id)
    typing = {"type": "typing", "conversation_id": conversation_id, "user_id": bot_id, "username": bot_name, "drawing": True}
    await manager.send_to_users(recipients, {**typing, "is_typing": True})
    try:
        check_quota(f"user:{user_id}")
        data, content_type = await generate_image(prompt)
    except ImageGenError as error:
        await manager.send_to_users(recipients, {**typing, "is_typing": False})
        await _bot_says(conversation_id, str(error), disappear_seconds)
        return
    await manager.send_to_users(recipients, {**typing, "is_typing": False})
    await post_bot_image(conversation_id, prompt.strip(), data, content_type, disappear_seconds)


def is_ai_chat(session: Session, conversation_id: int) -> bool:
    """A one-to-one chat with the AI itself."""
    conversation = session.get(Conversation, conversation_id)
    return conversation is not None and conversation.type == "direct" and any(
        u.is_bot for u in members(session, conversation_id)
    )


def wants_ai_reply(session: Session, conversation_id: int, content: str) -> bool:
    """The AI answers when someone writes @ai, or always in a DM with the AI itself."""
    return bool(MENTION_AI.search(content)) or is_ai_chat(session, conversation_id)


async def ai_reply(conversation_id: int, disappear_seconds: int = 0) -> None:
    with Session(engine) as session:
        bot = ensure_bot_user(session)
        bot_id, bot_name = bot.id, bot.username
        rows = session.exec(
            select(Message, User)
            .join(User, Message.sender_id == User.id)
            .where(Message.conversation_id == conversation_id, not_expired())
            .order_by(Message.id.desc())
            .limit(AI_CONTEXT_MESSAGES)
        ).all()
        recent = messages_out(session, list(reversed(rows)))
        recipients = member_ids(session, conversation_id)

    typing = {"type": "typing", "conversation_id": conversation_id, "user_id": bot_id, "username": bot_name}
    await manager.send_to_users(recipients, {**typing, "is_typing": True})
    # Photos, voice notes and files are turned into something the AI can read (app/ai_media.py).
    history = await prepare_for_ai(recent)
    # Pictures the AI draws with its generate_image tool are collected here, then posted.
    pictures: list = []
    made_images.set(pictures)
    image_quota_key.set(f"chat:{conversation_id}")
    reply = await generate_reply(history)
    await manager.send_to_users(recipients, {**typing, "is_typing": False})

    with Session(engine) as session:
        bot = session.get(User, bot_id)
        out = save_message(session, conversation_id, bot, reply[:MAX_MESSAGE_LENGTH], disappear_seconds)
    await manager.send_to_users(recipients, {"type": "message", "message": out, "client_id": None})
    for picture in pictures:
        await post_bot_image(conversation_id, picture["prompt"], picture["data"], picture["content_type"], disappear_seconds)
