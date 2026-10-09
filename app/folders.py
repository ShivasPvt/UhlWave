"""Organising your own chat list: archive, categories and locked chats.

These only change *your* view. The other people in a chat never know you've
archived, categorised or locked it.

  PATCH  /api/conversations/{id}/prefs   {"archived": true} or {"category_id": 3 | null}
  GET    /api/categories                 your categories
  POST   /api/categories                 make one: {"name": "Hostel"}
  PATCH  /api/categories/{id}            rename
  DELETE /api/categories/{id}            delete (its chats go back to "no category")

Locked chats sit behind a PIN, separate from your password:
  GET    /api/locked                     {"has_pin": true, "count": 2}
  PUT    /api/locked/pin                 set or change the PIN: {"pin": "4821", "current_pin": "1234"}
  DELETE /api/locked/pin                 remove the PIN; every locked chat is unlocked: {"pin": "4821"}
  POST   /api/locked/open                {"pin": "4821"}: opens the folder for 15 minutes
  POST   /api/locked/close               close it again now
  POST   /api/conversations/{id}/lock    {"pin": "4821"}: move a chat into the locked folder
  POST   /api/conversations/{id}/unlock  {"pin": "4821"}: take it back out

Entering the PIN sets a short-lived "chat_unlock" cookie (15 minutes), so photos
inside locked chats load too. POST /api/locked/close removes it straight away.
Without it, locked chats are left out of the chat list and search, and their
messages and files can't be loaded.
"""
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlmodel import Session, col, select, update

from .auth import ALGORITHM, get_current_user
from .config import COOKIE_SECURE, JWT_SECRET
from .db import get_session
from .models import Category, ConversationMember, User

router = APIRouter(prefix="/api", tags=["folders"])

UNLOCK_COOKIE = "chat_unlock"
UNLOCK_MINUTES = 15
MAX_CATEGORIES = 10
PIN_PATTERN = r"^\d{4,8}$"

# Wrong PINs: after 5 in 5 minutes, stop checking for a while. Kept in memory.
_pin_failures: dict[int, deque] = defaultdict(deque)
MAX_PIN_TRIES = 5
PIN_WINDOW_SECONDS = 300


# ---- PIN helpers -----------------------------------------------------------------

def _check_pin(user: User, pin: str | None) -> None:
    if not user.lock_pin_hash:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Set a chat lock PIN in Settings first.")
    failures = _pin_failures[user.id]
    now = time.monotonic()
    while failures and now - failures[0] > PIN_WINDOW_SECONDS:
        failures.popleft()
    if len(failures) >= MAX_PIN_TRIES:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many wrong PINs. Try again in 5 minutes.")
    ok = False
    if pin:
        try:
            ok = bcrypt.checkpw(pin.encode(), user.lock_pin_hash.encode())
        except ValueError:
            ok = False
    if not ok:
        failures.append(now)
        left = MAX_PIN_TRIES - len(failures)
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Wrong PIN. {left} {'try' if left == 1 else 'tries'} left." if left else "Wrong PIN.")
    failures.clear()


def unlock_token(user: User) -> str:
    expires = datetime.now(timezone.utc) + timedelta(minutes=UNLOCK_MINUTES)
    payload = {"sub": str(user.id), "v": user.token_version, "purpose": "unlock", "exp": expires}
    return jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)


def set_unlock_cookie(response: Response, user: User) -> None:
    response.set_cookie(
        UNLOCK_COOKIE, unlock_token(user), httponly=True, samesite="lax", secure=COOKIE_SECURE,
        max_age=UNLOCK_MINUTES * 60, path="/",
    )


def is_unlocked(request: Request, user: User) -> bool:
    """Has this browser entered the PIN in the last 15 minutes?"""
    token = request.cookies.get(UNLOCK_COOKIE)
    if not token:
        return False
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return False
    return (
        payload.get("purpose") == "unlock"
        and payload.get("sub") == str(user.id)
        and payload.get("v") == user.token_version
    )


def locked_ids(session: Session, user_id: int) -> set[int]:
    return set(session.exec(
        select(ConversationMember.conversation_id).where(
            ConversationMember.user_id == user_id, col(ConversationMember.locked).is_(True)
        )
    ).all())


def require_open(session: Session, request: Request, user: User, conversation_id: int) -> None:
    """Refuse to show a locked chat to a browser that hasn't entered the PIN."""
    if conversation_id in locked_ids(session, user.id) and not is_unlocked(request, user):
        raise HTTPException(status.HTTP_423_LOCKED, "This chat is locked. Enter your PIN to open it.")


# ---- per-chat preferences ------------------------------------------------------

class PrefsBody(BaseModel):
    archived: bool | None = None
    category_id: int | None = None
    clear_category: bool = False


def _my_membership(session: Session, conversation_id: int, me: User) -> ConversationMember:
    m = session.get(ConversationMember, (conversation_id, me.id))
    if m is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found.")
    return m


@router.patch("/conversations/{conversation_id}/prefs")
def set_prefs(conversation_id: int, body: PrefsBody, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    m = _my_membership(session, conversation_id, me)
    if body.archived is not None:
        m.archived = body.archived
    if body.clear_category:
        m.category_id = None
    elif body.category_id is not None:
        category = session.get(Category, body.category_id)
        if category is None or category.user_id != me.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Category not found.")
        m.category_id = category.id
    session.add(m)
    session.commit()
    return {"archived": m.archived, "category_id": m.category_id, "locked": m.locked}


class PinBody(BaseModel):
    pin: str = Field(min_length=1, max_length=8)


@router.post("/conversations/{conversation_id}/lock")
def lock_chat(conversation_id: int, body: PinBody, response: Response, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    m = _my_membership(session, conversation_id, me)
    _check_pin(me, body.pin)
    m.locked, m.archived = True, False  # a locked chat lives in the locked folder only
    session.add(m)
    session.commit()
    # The folder stays shut: opening it always asks for the PIN again.
    response.delete_cookie(UNLOCK_COOKIE, path="/")
    return {"locked": True}


@router.post("/conversations/{conversation_id}/unlock")
def unlock_chat(conversation_id: int, body: PinBody, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    m = _my_membership(session, conversation_id, me)
    _check_pin(me, body.pin)
    m.locked = False
    session.add(m)
    session.commit()
    return {"locked": False}


# ---- the locked folder ------------------------------------------------------------

@router.get("/locked")
def locked_summary(request: Request, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    return {"has_pin": bool(me.lock_pin_hash), "count": len(locked_ids(session, me.id)), "open": is_unlocked(request, me)}


class SetPinBody(BaseModel):
    pin: str = Field(pattern=PIN_PATTERN)
    current_pin: str | None = None


@router.put("/locked/pin")
def set_pin(body: SetPinBody, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if me.lock_pin_hash:
        _check_pin(me, body.current_pin)
    me.lock_pin_hash = bcrypt.hashpw(body.pin.encode(), bcrypt.gensalt()).decode()
    session.add(me)
    session.commit()
    return {"has_pin": True}


@router.delete("/locked/pin")
def remove_pin(body: PinBody, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    _check_pin(me, body.pin)
    session.execute(
        update(ConversationMember).where(col(ConversationMember.user_id) == me.id).values(locked=False)
    )
    me.lock_pin_hash = None
    session.add(me)
    session.commit()
    return {"has_pin": False}


@router.post("/locked/open")
def open_locked(body: PinBody, response: Response, me: User = Depends(get_current_user)):
    _check_pin(me, body.pin)
    set_unlock_cookie(response, me)
    return {"open": True, "minutes": UNLOCK_MINUTES}


@router.post("/locked/close")
def close_locked(response: Response):
    response.delete_cookie(UNLOCK_COOKIE, path="/")
    return {"open": False}


# ---- categories ------------------------------------------------------------------

class CategoryBody(BaseModel):
    name: str = Field(min_length=1, max_length=24)


def _category_out(c: Category) -> dict:
    return {"id": c.id, "name": c.name}


@router.get("/categories")
def list_categories(me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    rows = session.exec(select(Category).where(Category.user_id == me.id).order_by(Category.id)).all()
    return [_category_out(c) for c in rows]


@router.post("/categories", status_code=status.HTTP_201_CREATED)
def create_category(body: CategoryBody, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "Give the category a name.")
    existing = session.exec(select(Category).where(Category.user_id == me.id)).all()
    if len(existing) >= MAX_CATEGORIES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"You can have up to {MAX_CATEGORIES} categories.")
    if any(c.name.lower() == name.lower() for c in existing):
        raise HTTPException(status.HTTP_409_CONFLICT, f'You already have a category called "{name}".')
    category = Category(user_id=me.id, name=name)
    session.add(category)
    session.commit()
    session.refresh(category)
    return _category_out(category)


def _my_category(session: Session, category_id: int, me: User) -> Category:
    category = session.get(Category, category_id)
    if category is None or category.user_id != me.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Category not found.")
    return category


@router.patch("/categories/{category_id}")
def rename_category(category_id: int, body: CategoryBody, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    category = _my_category(session, category_id, me)
    category.name = body.name.strip() or category.name
    session.add(category)
    session.commit()
    return _category_out(category)


@router.delete("/categories/{category_id}")
def delete_category(category_id: int, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    category = _my_category(session, category_id, me)
    session.execute(
        update(ConversationMember)
        .where(col(ConversationMember.user_id) == me.id, col(ConversationMember.category_id) == category.id)
        .values(category_id=None)
    )
    session.delete(category)
    session.commit()
    return {"ok": True}

