"""Abuse limits shared across the app.

  * client_ip()          the visitor's real address, safe behind Render's proxy
  * Limiter              "at most N in the last M seconds", kept in memory
  * login / PIN lockout  wrong guesses counted per account in the database, so
                         restarting the server doesn't reset them
  * message_problem()    stops one person flooding a chat
  * ai_problem()         stops one person using up the AI key for everyone

In-memory limits are per server process, which is all this app runs on.
"""
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from starlette.requests import HTTPConnection

from .config import (
    AI_REPLIES_PER_HOUR, LOGIN_LOCK_MINUTES, LOGIN_MAX_FAILURES, MESSAGE_BURST, MESSAGE_BURST_SECONDS, TRUST_PROXY,
)
from .models import User


def client_ip(conn: HTTPConnection) -> str:
    """Who is asking. Behind a proxy (TRUST_PROXY=true) that's the LAST address in
    X-Forwarded-For: the one the proxy itself saw. Earlier entries come from the
    visitor and can be made up, so they're never used."""
    if TRUST_PROXY:
        forwarded = conn.headers.get("x-forwarded-for", "")
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        if hops:
            return hops[-1]
    return conn.client.host if conn.client else "unknown"


class Limiter:
    """Allow `limit` hits per key in any `window` seconds."""

    def __init__(self, limit: int, window: float) -> None:
        self.limit, self.window = limit, window
        self.hits: dict[str, deque] = defaultdict(deque)

    def _trim(self, key: str, now: float) -> deque:
        hits = self.hits[key]
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        return hits

    def blocked(self, key: str) -> float:
        """Seconds until the next hit is allowed (0 = allowed now). Doesn't count a hit."""
        now = time.monotonic()
        hits = self._trim(key, now)
        if len(hits) < self.limit:
            return 0.0
        return self.window - (now - hits[0])

    def hit(self, key: str) -> float:
        """Count a hit if allowed. Returns 0 when allowed, else seconds to wait."""
        wait = self.blocked(key)
        if not wait:
            self.hits[key].append(time.monotonic())
        if len(self.hits) > 20000:  # forget idle keys now and then
            now = time.monotonic()
            for k in [k for k, v in self.hits.items() if not v or now - v[-1] >= self.window]:
                self.hits.pop(k, None)
        return wait

    def clear(self, key: str) -> None:
        self.hits.pop(key, None)


def _minutes(seconds: float) -> int:
    return max(1, int(seconds // 60) + (1 if seconds % 60 else 0))


def _naive(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=None) if dt is not None and dt.tzinfo else dt


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---- wrong passwords -------------------------------------------------------------

# Also limit by address, so one visitor can't spray guesses across many accounts.
login_ip_limiter = Limiter(limit=50, window=15 * 60)


def login_wait_seconds(user: User | None) -> float:
    """How long this account must wait before another password attempt (0 = go ahead)."""
    if user is None or user.login_failures < LOGIN_MAX_FAILURES or user.login_failed_at is None:
        return 0.0
    unlock = _naive(user.login_failed_at) + timedelta(minutes=LOGIN_LOCK_MINUTES)
    return max(0.0, (unlock - _now()).total_seconds())


def record_login_failure(user: User) -> None:
    """Count a wrong password. The count restarts once the lock window has passed."""
    window_over = user.login_failed_at is None or _now() - _naive(user.login_failed_at) > timedelta(minutes=LOGIN_LOCK_MINUTES)
    user.login_failures = 1 if window_over else user.login_failures + 1
    user.login_failed_at = datetime.now(timezone.utc)


def clear_login_failures(user: User) -> None:
    user.login_failures, user.login_failed_at = 0, None


def too_many_logins(seconds: float) -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        f"Too many wrong passwords. Try again in {_minutes(seconds)} min.",
        headers={"Retry-After": str(int(seconds) + 1)},
    )


# ---- wrong chat-lock PINs -------------------------------------------------------------

PIN_MAX_FAILURES = 5
PIN_WINDOW = timedelta(minutes=5)


def pin_wait_seconds(user: User) -> float:
    if user.pin_failures < PIN_MAX_FAILURES or user.pin_failed_at is None:
        return 0.0
    return max(0.0, (_naive(user.pin_failed_at) + PIN_WINDOW - _now()).total_seconds())


def record_pin_failure(user: User) -> int:
    """Count a wrong PIN; returns how many tries are left in this window."""
    window_over = user.pin_failed_at is None or _now() - _naive(user.pin_failed_at) > PIN_WINDOW
    user.pin_failures = 1 if window_over else user.pin_failures + 1
    user.pin_failed_at = datetime.now(timezone.utc)
    return max(0, PIN_MAX_FAILURES - user.pin_failures)


def clear_pin_failures(user: User) -> None:
    user.pin_failures, user.pin_failed_at = 0, None


# ---- flooding and the AI key --------------------------------------------------------

message_limiter = Limiter(limit=MESSAGE_BURST, window=MESSAGE_BURST_SECONDS)
ai_limiter = Limiter(limit=AI_REPLIES_PER_HOUR, window=3600)


def message_problem(user_id: int) -> str | None:
    """Why this person can't send right now (too fast), or None."""
    wait = message_limiter.hit(f"user:{user_id}")
    if wait:
        return f"You're sending messages very fast. Wait {int(wait) + 1} s and try again."
    return None


def ai_problem(user_id: int) -> str | None:
    """Count one @ai answer for this person; None if allowed, else what to tell them."""
    wait = ai_limiter.hit(f"user:{user_id}")
    if wait:
        return f"You've asked @ai {AI_REPLIES_PER_HOUR} times this hour, the limit. Try again in {_minutes(wait)} min."
    return None
