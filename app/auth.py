"""Register, log in, log out, and "who am I".

Passwords are hashed with bcrypt. After login the server gives the browser a
signed JWT inside an httpOnly cookie: JavaScript can't read it (so a script
injected into the page can't steal it), and the browser sends it automatically
with every request, including when it opens the WebSocket.
"""
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from .config import COOKIE_SECURE, JWT_EXPIRE_HOURS, JWT_SECRET
from .db import get_session
from .models import User
from .services import user_out

COOKIE_NAME = "access_token"
ALGORITHM = "HS256"

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ---- password + token helpers -------------------------------------------

def hash_password(password: str) -> str:
    # bcrypt only looks at the first 72 bytes, so we cut there explicitly.
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode()[:72], password_hash.encode())
    except ValueError:  # not a real bcrypt hash (e.g. the bot account)
        return False


def create_token(user_id: int) -> str:
    expires = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS)
    return jwt.encode({"sub": str(user_id), "exp": expires}, JWT_SECRET, algorithm=ALGORITHM)


def user_from_token(token: str | None, session: Session) -> User | None:
    if not token:
        return None
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None
    user = session.get(User, user_id)
    if user is None or user.is_bot:
        return None
    return user


def get_current_user(request: Request, session: Session = Depends(get_session)) -> User:
    """Dependency for routes that need a logged-in user."""
    user = user_from_token(request.cookies.get(COOKIE_NAME), session)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Log in to continue.")
    return user


def _set_login_cookie(response: Response, user_id: int) -> None:
    response.set_cookie(
        COOKIE_NAME,
        create_token(user_id),
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        max_age=JWT_EXPIRE_HOURS * 3600,
    )


# ---- routes --------------------------------------------------------------

class RegisterBody(BaseModel):
    username: str = Field(min_length=3, max_length=30, pattern=r"^[A-Za-z0-9_]+$")
    password: str = Field(min_length=6, max_length=128)


class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(body: RegisterBody, response: Response, session: Session = Depends(get_session)):
    username = body.username.lower()
    if session.exec(select(User).where(User.username == username)).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "That username is taken. Try another.")
    user = User(username=username, password_hash=hash_password(body.password))
    session.add(user)
    session.commit()
    session.refresh(user)
    _set_login_cookie(response, user.id)
    return user_out(user)


@router.post("/login")
def login(body: LoginBody, response: Response, session: Session = Depends(get_session)):
    user = session.exec(select(User).where(User.username == body.username.lower())).first()
    if user is None or user.is_bot or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong username or password.")
    _set_login_cookie(response, user.id)
    return user_out(user)


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return user_out(user)
