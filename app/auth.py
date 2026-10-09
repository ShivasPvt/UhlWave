"""Register, log in (username + password, or Google), log out, and "who am I".

Passwords are hashed with bcrypt. With Google sign-in the browser gets a signed
ID token from Google and hands it to us; we check Google's signature and that
the token was issued for *our* client ID before trusting the email inside it. After login the server gives the browser a
signed JWT inside an httpOnly cookie: JavaScript can't read it (so a script
injected into the page can't steal it), and the browser sends it automatically
with every request, including when it opens the WebSocket.
"""
from datetime import datetime, timedelta, timezone

import re

import bcrypt
import jwt
from google.auth.exceptions import GoogleAuthError, TransportError
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from .config import COOKIE_SECURE, GOOGLE_ALLOWED_DOMAIN, GOOGLE_CLIENT_ID, JWT_EXPIRE_HOURS, JWT_SECRET
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


def create_token(user: User) -> str:
    expires = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS)
    # "v" ties the token to the user's token_version; "Log out of all devices" bumps it.
    payload = {"sub": str(user.id), "v": user.token_version, "exp": expires}
    return jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)


def user_from_token(token: str | None, session: Session) -> User | None:
    if not token:
        return None
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        user_id = int(payload["sub"])
        version = int(payload.get("v", 0))
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        return None
    user = session.get(User, user_id)
    if user is None or user.is_bot or version != user.token_version:
        return None
    return user


def get_current_user(request: Request, session: Session = Depends(get_session)) -> User:
    """Dependency for routes that need a logged-in user."""
    user = user_from_token(request.cookies.get(COOKIE_NAME), session)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Log in to continue.")
    return user


def set_login_cookie(response: Response, user: User) -> None:
    response.set_cookie(
        COOKIE_NAME,
        create_token(user),
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        max_age=JWT_EXPIRE_HOURS * 3600,
    )


# ---- Google sign-in helpers ------------------------------------------------

def verify_google_credential(credential: str) -> dict:
    """Return the token's claims if Google really signed it for our app.

    Checks the signature against Google's public keys, the expiry, the issuer
    and that the audience is our GOOGLE_CLIENT_ID. Raises ValueError otherwise.
    """
    return google_id_token.verify_oauth2_token(credential, google_requests.Request(), GOOGLE_CLIENT_ID)


def _username_from_email(session: Session, email: str) -> str:
    """riya.sharma@gmail.com -> "riya_sharma", or "riya_sharma2" if that's taken."""
    base = re.sub(r"[^a-z0-9_]+", "_", email.split("@")[0].lower()).strip("_")[:24]
    if len(base) < 3:
        base = (base + "_user")[:24].strip("_") or "user"
    candidate, n = base, 1
    while session.exec(select(User).where(User.username == candidate)).first():
        n += 1
        candidate = f"{base}{n}"
    return candidate


# ---- routes --------------------------------------------------------------

USERNAME_PATTERN = r"^[A-Za-z0-9_]+$"


# Passwords people pick most often; nobody gets to use these.
COMMON_PASSWORDS = {
    "password", "password1", "password123", "passw0rd", "12345678", "123456789", "1234567890", "qwerty123",
    "qwertyuiop", "iloveyou", "11111111", "abc12345", "abcd1234", "letmein1", "welcome1", "admin123",
    "football1", "princess1", "sunshine1", "monkey123", "india123", "iitmandi", "iitmandi123", "uhlwave1",
}


def password_problem(password: str, username: str) -> str | None:
    """Why a new password isn't good enough, or None. The sign-up page checks the
    same rules as you type (static/auth.js); this is the check that counts."""
    if len(password) < 8:
        return "Use at least 8 characters."
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        return "Use both letters and numbers."
    lowered = password.lower()
    if username and len(username) >= 3 and username.lower() in lowered:
        return "Don't put your username in your password."
    if lowered in COMMON_PASSWORDS or len(set(password)) < 4:
        return "That password is too easy to guess. Try a short phrase with a number."
    return None


class RegisterBody(BaseModel):
    username: str = Field(min_length=3, max_length=30, pattern=USERNAME_PATTERN)
    password: str = Field(min_length=6, max_length=128)


class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(body: RegisterBody, response: Response, session: Session = Depends(get_session)):
    username = body.username.lower()
    problem = password_problem(body.password, username)
    if problem:
        raise HTTPException(422, problem)
    if session.exec(select(User).where(User.username == username)).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "That username is taken. Try another.")
    user = User(username=username, password_hash=hash_password(body.password))
    session.add(user)
    session.commit()
    session.refresh(user)
    set_login_cookie(response, user)
    return user_out(user)


@router.post("/login")
def login(body: LoginBody, response: Response, session: Session = Depends(get_session)):
    user = session.exec(select(User).where(User.username == body.username.lower())).first()
    if user is None or user.is_bot or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong username or password.")
    set_login_cookie(response, user)
    return user_out(user)


class GoogleBody(BaseModel):
    credential: str = Field(min_length=1, max_length=8000)


@router.get("/config")
def auth_config():
    """Public settings the login page needs. A client ID is not a secret."""
    return {"google_client_id": GOOGLE_CLIENT_ID or None}


@router.post("/google")
def google_login(body: GoogleBody, response: Response, session: Session = Depends(get_session)):
    """Sign in (or sign up, the first time) with a Google account."""
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Google sign-in isn't set up on this server.")
    try:
        claims = verify_google_credential(body.credential)
    except TransportError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Couldn't reach Google. Try again in a moment.")
    except (ValueError, GoogleAuthError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Google sign-in failed. Please try again.")

    email = (claims.get("email") or "").lower()
    if not email or not claims.get("email_verified"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your Google account needs a verified email.")
    if GOOGLE_ALLOWED_DOMAIN and not email.endswith("@" + GOOGLE_ALLOWED_DOMAIN):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Use your @{GOOGLE_ALLOWED_DOMAIN} account to sign in.")

    sub = str(claims["sub"])
    user = session.exec(select(User).where(User.google_sub == sub)).first()
    if user is None:
        # Same email seen before (e.g. Google account re-created): link it.
        user = session.exec(select(User).where(User.email == email)).first()
    if user is not None and user.is_bot:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Google sign-in failed. Please try again.")

    created = user is None
    if created:
        user = User(username=_username_from_email(session, email), password_hash="!")
    user.google_sub, user.email = sub, email
    session.add(user)
    session.commit()
    session.refresh(user)

    set_login_cookie(response, user)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return user_out(user)


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return user_out(user)
