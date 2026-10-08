"""The real-time part. Every logged-in browser keeps one WebSocket open to /ws.

Messages are small JSON objects with a "type" field.

Browser -> server
  {"type": "message", "conversation_id": 7, "content": "hi", "client_id": "tmp-123"}
  {"type": "typing",  "conversation_id": 7, "is_typing": true}

Server -> browser
  {"type": "message",  "message": {...}, "client_id": "tmp-123"}
  {"type": "typing",   "conversation_id": 7, "user_id": 2, "username": "riya", "is_typing": true}
  {"type": "presence", "user_id": 2, "online": false}
  {"type": "error",    "detail": "..."}
"""
import asyncio
import json
import re

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlmodel import Session, select

from .auth import COOKIE_NAME, user_from_token
from .bot import generate_reply
from .config import AI_CONTEXT_MESSAGES
from .db import engine
from .manager import manager
from .models import Conversation, Message, User, utcnow
from .services import ensure_bot_user, is_member, member_ids, members, message_out, save_message

router = APIRouter()

MAX_MESSAGE_LENGTH = 4000
MENTION_AI = re.compile(r"(^|\W)@ai\b", re.IGNORECASE)

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

    # 2. Register the socket and tell everyone if this person just came online.
    if manager.connect(user_id, websocket):
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
            else:
                await websocket.send_json({"type": "error", "detail": f"Unknown event type: {kind!r}"})
    except WebSocketDisconnect:
        pass
    finally:
        # 4. Clean up. If that was their last open tab, they're now offline.
        if manager.disconnect(user_id, websocket):
            with Session(engine) as session:
                db_user = session.get(User, user_id)
                if db_user:
                    db_user.last_seen_at = utcnow()
                    session.add(db_user)
                    session.commit()
            await manager.broadcast({"type": "presence", "user_id": user_id, "online": False})


async def handle_message(user_id: int, event: dict, websocket: WebSocket) -> None:
    conversation_id = event.get("conversation_id")
    content = event.get("content")
    client_id = event.get("client_id")

    if not isinstance(conversation_id, int) or not isinstance(content, str):
        await websocket.send_json({"type": "error", "detail": "Message needs conversation_id and content."})
        return
    content = content.strip()
    if not content:
        return
    if len(content) > MAX_MESSAGE_LENGTH:
        await websocket.send_json({"type": "error", "detail": "Message is too long.", "client_id": client_id})
        return

    with Session(engine) as session:
        # Never trust the browser: check this person really is in the conversation.
        if not is_member(session, conversation_id, user_id):
            await websocket.send_json({"type": "error", "detail": "You're not in that conversation.", "client_id": client_id})
            return
        sender = session.get(User, user_id)
        # Save first, then deliver. If it's on screen, it's in the database.
        out = save_message(session, conversation_id, sender, content)
        recipients = member_ids(session, conversation_id)
        should_ai_reply = wants_ai_reply(session, conversation_id, content)

    await manager.send_to_users(recipients, {"type": "message", "message": out, "client_id": client_id})

    if should_ai_reply:
        task = asyncio.create_task(ai_reply(conversation_id))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)


async def handle_typing(user_id: int, username: str, event: dict) -> None:
    conversation_id = event.get("conversation_id")
    if not isinstance(conversation_id, int):
        return
    with Session(engine) as session:
        if not is_member(session, conversation_id, user_id):
            return
        others = [uid for uid in member_ids(session, conversation_id) if uid != user_id]
    await manager.send_to_users(others, {
        "type": "typing",
        "conversation_id": conversation_id,
        "user_id": user_id,
        "username": username,
        "is_typing": bool(event.get("is_typing")),
    })


def wants_ai_reply(session: Session, conversation_id: int, content: str) -> bool:
    """The AI answers when someone writes @ai, or always in a DM with the AI itself."""
    if MENTION_AI.search(content):
        return True
    conversation = session.get(Conversation, conversation_id)
    return conversation is not None and conversation.type == "direct" and any(
        u.is_bot for u in members(session, conversation_id)
    )


async def ai_reply(conversation_id: int) -> None:
    with Session(engine) as session:
        bot = ensure_bot_user(session)
        bot_id, bot_name = bot.id, bot.username
        rows = session.exec(
            select(Message, User)
            .join(User, Message.sender_id == User.id)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id.desc())
            .limit(AI_CONTEXT_MESSAGES)
        ).all()
        history = [message_out(m, u) for m, u in reversed(rows)]
        recipients = member_ids(session, conversation_id)

    typing = {"type": "typing", "conversation_id": conversation_id, "user_id": bot_id, "username": bot_name}
    await manager.send_to_users(recipients, {**typing, "is_typing": True})
    reply = await generate_reply(history)
    await manager.send_to_users(recipients, {**typing, "is_typing": False})

    with Session(engine) as session:
        bot = session.get(User, bot_id)
        out = save_message(session, conversation_id, bot, reply[:MAX_MESSAGE_LENGTH])
    await manager.send_to_users(recipients, {"type": "message", "message": out, "client_id": None})
