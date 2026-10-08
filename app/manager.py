"""Keeps track of who is connected right now.

`active` maps a user id to the set of their open WebSockets. A set, because
one person can have the app open in two tabs or on phone and laptop at once.

This lives in memory, which is fine for a single server. If you ever run
several servers, swap this for Redis pub/sub so they can reach each other's users.
"""
from collections import defaultdict
from typing import Any, Iterable

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        self.active: dict[int, set[WebSocket]] = defaultdict(set)

    def connect(self, user_id: int, websocket: WebSocket) -> bool:
        """Register a socket. Returns True if the user just came online."""
        first = not self.active.get(user_id)
        self.active[user_id].add(websocket)
        return first

    def disconnect(self, user_id: int, websocket: WebSocket) -> bool:
        """Forget a socket. Returns True if the user is now fully offline."""
        sockets = self.active.get(user_id)
        if not sockets:
            return False
        sockets.discard(websocket)
        if not sockets:
            del self.active[user_id]
            return True
        return False

    def is_online(self, user_id: int) -> bool:
        return bool(self.active.get(user_id))

    async def send_to_user(self, user_id: int, payload: dict[str, Any]) -> None:
        for ws in list(self.active.get(user_id, ())):
            try:
                await ws.send_json(payload)
            except Exception:
                # The socket died without telling us; drop it.
                self.disconnect(user_id, ws)

    async def send_to_users(self, user_ids: Iterable[int], payload: dict[str, Any]) -> None:
        for user_id in set(user_ids):
            await self.send_to_user(user_id, payload)

    async def broadcast(self, payload: dict[str, Any], exclude: int | None = None) -> None:
        await self.send_to_users([uid for uid in self.active if uid != exclude], payload)


manager = ConnectionManager()
