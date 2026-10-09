"""Voice and video calls between two people.

The audio and video go straight from one browser to the other (WebRTC). The
server only passes along the short setup messages ("signalling") over the
existing WebSocket, and writes a line in the chat afterwards ("Video call · 2:31"
or "Missed voice call").

Browser -> server
  {"type": "call_invite",  "call_id": "…", "conversation_id": 7, "video": true}
  {"type": "call_accept",  "call_id": "…"}
  {"type": "call_decline", "call_id": "…"}
  {"type": "call_signal",  "call_id": "…", "data": {...}}   offer, answer or network candidate
  {"type": "call_end",     "call_id": "…"}

Server -> browser
  {"type": "call_ringing",  "call_id": "…"}                         to the caller
  {"type": "call_incoming", "call_id": "…", "conversation_id": 7, "from": {...}, "video": true}
  {"type": "call_accepted", "call_id": "…"}                         to the caller: send your offer now
  {"type": "call_signal",   "call_id": "…", "data": {...}}
  {"type": "call_ended",    "call_id": "…", "reason": "declined" | "hangup" | "no_answer" | "busy" | "offline" | "answered_elsewhere" | "disconnected"}

Only one-to-one chats can call (group calls need a media server). Calls are kept
in memory, so they need the single server this app runs on.
"""
import asyncio
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, WebSocket
from sqlmodel import Session

from .auth import get_current_user
from .config import STUN_URLS, TURN_CREDENTIAL, TURN_URLS, TURN_USERNAME
from .db import engine
from .manager import manager
from .models import Conversation, User
from .services import blocked_either_way, member_ids, save_message, user_out

router = APIRouter(prefix="/api/calls", tags=["calls"])

RING_SECONDS = 35


@dataclass
class Call:
    id: str
    conversation_id: int
    caller_id: int
    callee_id: int
    video: bool
    caller_ws: WebSocket
    callee_ws: WebSocket | None = None
    state: str = "ringing"  # then "active"
    answered_at: float | None = None
    timer: asyncio.Task | None = field(default=None, repr=False)


calls: dict[str, Call] = {}
in_call: dict[int, str] = {}  # user id -> call id


@router.get("/config")
def call_config(me: User = Depends(get_current_user)):
    """The servers browsers use to find a route to each other."""
    servers = [{"urls": STUN_URLS}] if STUN_URLS else []
    if TURN_URLS:
        servers.append({"urls": TURN_URLS, "username": TURN_USERNAME, "credential": TURN_CREDENTIAL})
    return {"iceServers": servers, "relay": bool(TURN_URLS)}


def _label(call: Call, answered: bool) -> str:
    kind = "video" if call.video else "voice"
    if not answered or call.answered_at is None:
        return f"Missed {kind} call"
    seconds = int(time.monotonic() - call.answered_at)
    return f"{kind.capitalize()} call · {seconds // 60}:{seconds % 60:02d}"


async def _log(call: Call, answered: bool) -> None:
    """Write the call into the chat, from the caller, so it shows in history."""
    with Session(engine) as session:
        caller = session.get(User, call.caller_id)
        if caller is None:
            return
        out = save_message(session, call.conversation_id, caller, _label(call, answered), disappear_seconds=0, type="call")
        recipients = member_ids(session, call.conversation_id)
    await manager.send_to_users(recipients, {"type": "message", "message": out, "client_id": None})


async def _send(ws: WebSocket | None, payload: dict) -> None:
    if ws is None:
        return
    try:
        await ws.send_json(payload)
    except Exception:
        pass


async def _finish(call: Call, reason: str, tell: list[int] | None = None) -> None:
    """End a call, tell whoever needs telling, and log it once."""
    if calls.pop(call.id, None) is None:
        return  # already finished
    for uid in (call.caller_id, call.callee_id):
        if in_call.get(uid) == call.id:
            del in_call[uid]
    if call.timer and not call.timer.done() and call.timer is not asyncio.current_task():
        call.timer.cancel()
    payload = {"type": "call_ended", "call_id": call.id, "reason": reason}
    for uid in tell if tell is not None else (call.caller_id, call.callee_id):
        if call.state == "active":
            await _send(call.caller_ws if uid == call.caller_id else call.callee_ws, payload)
        else:
            await manager.send_to_user(uid, payload)
    await _log(call, answered=call.state == "active")


async def _ring_timeout(call: Call) -> None:
    await asyncio.sleep(RING_SECONDS)
    if calls.get(call.id) is call and call.state == "ringing":
        await _finish(call, "no_answer")


def _other(call: Call, user_id: int) -> tuple[int, WebSocket | None]:
    if user_id == call.caller_id:
        return call.callee_id, call.callee_ws
    return call.caller_id, call.caller_ws


async def handle_event(user_id: int, username: str, event: dict, websocket: WebSocket) -> None:
    kind = event.get("type")
    call_id = event.get("call_id")
    if not isinstance(call_id, str) or not (8 <= len(call_id) <= 64):
        await websocket.send_json({"type": "error", "detail": "Calls need a call_id."})
        return

    if kind == "call_invite":
        await _invite(user_id, call_id, event, websocket)
        return

    call = calls.get(call_id)
    if call is None or user_id not in (call.caller_id, call.callee_id):
        await websocket.send_json({"type": "call_ended", "call_id": call_id, "reason": "gone"})
        return

    if kind == "call_accept" and user_id == call.callee_id and call.state == "ringing":
        call.state, call.callee_ws, call.answered_at = "active", websocket, time.monotonic()
        if call.timer:
            call.timer.cancel()
        await _send(call.caller_ws, {"type": "call_accepted", "call_id": call.id})
        # Stop the ringing on the callee's other tabs and phones.
        for ws in list(manager.active.get(user_id, ())):
            if ws is not websocket:
                await _send(ws, {"type": "call_ended", "call_id": call.id, "reason": "answered_elsewhere"})
    elif kind == "call_decline" and user_id == call.callee_id and call.state == "ringing":
        await _finish(call, "declined")
    elif kind == "call_signal" and call.state == "active":
        data = event.get("data")
        if isinstance(data, dict):
            _, ws = _other(call, user_id)
            await _send(ws, {"type": "call_signal", "call_id": call.id, "data": data})
    elif kind == "call_end":
        await _finish(call, "hangup")


async def _invite(user_id: int, call_id: str, event: dict, websocket: WebSocket) -> None:
    conversation_id = event.get("conversation_id")
    video = bool(event.get("video"))
    refuse = lambda reason: websocket.send_json({"type": "call_ended", "call_id": call_id, "reason": reason})  # noqa: E731

    with Session(engine) as session:
        conversation = session.get(Conversation, conversation_id) if isinstance(conversation_id, int) else None
        people = member_ids(session, conversation_id) if conversation else []
        if conversation is None or conversation.type not in ("direct", "secret") or user_id not in people:
            await refuse("not_allowed")
            return
        others = [uid for uid in people if uid != user_id]
        callee = session.get(User, others[0]) if others else None
        if callee is None or callee.is_bot or blocked_either_way(session, user_id, callee.id):
            await refuse("not_allowed")
            return
        caller_card = user_out(session.get(User, user_id))

    if user_id in in_call or call_id in calls:
        await refuse("busy_self")
        return
    call = Call(id=call_id, conversation_id=conversation_id, caller_id=user_id, callee_id=callee.id, video=video, caller_ws=websocket)
    if not manager.is_online(callee.id):
        await refuse("offline")
        await _log(call, answered=False)
        return
    if callee.id in in_call:
        await refuse("busy")
        await _log(call, answered=False)
        return

    calls[call_id] = call
    in_call[user_id] = in_call[callee.id] = call_id
    call.timer = asyncio.create_task(_ring_timeout(call))
    await websocket.send_json({"type": "call_ringing", "call_id": call_id})
    await manager.send_to_user(callee.id, {
        "type": "call_incoming", "call_id": call_id, "conversation_id": conversation_id,
        "from": caller_card, "video": video,
    })


async def on_disconnect(user_id: int, websocket: WebSocket, fully_offline: bool) -> None:
    """A browser went away. End its call if that socket was the one in the call."""
    call_id = in_call.get(user_id)
    call = calls.get(call_id) if call_id else None
    if call is None:
        return
    if call.state == "active":
        mine = call.caller_ws if user_id == call.caller_id else call.callee_ws
        if mine is websocket:
            other_id, _ = _other(call, user_id)
            await _finish(call, "disconnected", tell=[other_id])
    elif user_id == call.caller_id and call.caller_ws is websocket:
        await _finish(call, "hangup", tell=[call.callee_id])
    elif user_id == call.callee_id and fully_offline:
        await _finish(call, "offline", tell=[call.caller_id])
