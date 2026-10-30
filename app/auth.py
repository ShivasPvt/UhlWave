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
import time
import secrets

import bcrypt
import jwt
from google.auth.exceptions import GoogleAuthError, TransportError
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from .config import COOKIE_SECURE, GOOGLE_ALLOWED_DOMAIN, GOOGLE_CLIENT_ID, JWT_EXPIRE_HOURS, JWT_SECRET
from .db import get_session
from .models import RevokedToken, User
from .security import (
    clear_login_failures, client_ip, login_ip_limiter, login_wait_seconds, record_login_failure, too_many_logins,
)
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
    # "jti" names this one login, so logging out can cancel exactly this cookie.
    payload = {"sub": str(user.id), "v": user.token_version, "exp": expires, "jti": secrets.token_urlsafe(16)}
    return jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)


def _token_claims(token: str | None) -> dict | None:
    """The claims of a valid login token, or None. Tokens made for anything else
    (like the 15-minute chat-unlock cookie) carry a "purpose" and are refused here."""
    if not token:
        return None
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None
    if "purpose" in payload:
        return None
    return payload


def user_from_token(token: str | None, session: Session) -> User | None:
    payload = _token_claims(token)
    if payload is None:
        return None
    try:
        user_id = int(payload["sub"])
        version = int(payload.get("v", 0))
    except (KeyError, ValueError, TypeError):
        return None
    jti = payload.get("jti")
    if jti and session.get(RevokedToken, str(jti)) is not None:
        return None  # this login was logged out
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
    while username_problem(session, candidate) is not None:
        n += 1
        candidate = f"{base}{n}"
    return candidate


# ---- routes --------------------------------------------------------------

USERNAME_PATTERN = r"^[A-Za-z0-9_]+$"
USERNAME_RE = re.compile(r"^[a-z0-9_]{3,30}$")

# Names nobody can take, so no one can pretend to be the app, the AI or the staff.
RESERVED_USERNAMES = {
    "ai", "admin", "administrator", "root", "system", "support", "help", "helpdesk", "staff", "team",
    "moderator", "mod", "mods", "official", "uhlwave", "uhl_wave", "security", "everyone", "here",
    "me", "you", "null", "undefined", "anonymous", "deleted", "deleted_user", "iitmandi", "iit_mandi",
}


def username_problem(session: Session, username: str, me_id: int | None = None) -> str | None:
    """Why this username can't be used, or None if it's free. Usernames are
    stored in lowercase, so "Meera" and "meera" are the same name: one per person."""
    name = (username or "").strip().lower()
    if len(name) < 3:
        return "Usernames need at least 3 characters."
    if len(name) > 30:
        return "Usernames can be at most 30 characters."
    if not USERNAME_RE.match(name):
        return "Use only letters, numbers and underscores."
    if name in RESERVED_USERNAMES:
        return "That username is reserved. Try another."
    owner = session.exec(select(User).where(User.username == name)).first()
    if owner is not None and owner.id != me_id:
        return "That username is taken. Try another."
    return None


def suggest_usernames(session: Session, wanted: str, count: int = 3) -> list[str]:
    """A few free names close to what someone typed: meera -> meera_27, meera07, meera_iitm."""
    base = re.sub(r"[^a-z0-9_]+", "", (wanted or "").lower())[:24].strip("_") or "user"
    if len(base) < 3:
        base = (base + "_user")[:24]
    ideas = [f"{base}_{secrets.randbelow(90) + 10}", f"{base}{secrets.randbelow(900) + 100}", f"{base}_iitm",
             f"the_{base}", f"{base}_{secrets.randbelow(9000) + 1000}"]
    out = []
    for idea in ideas:
        idea = idea[:30]
        if idea not in out and username_problem(session, idea) is None:
            out.append(idea)
        if len(out) == count:
            break
    return out


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
    name_problem = username_problem(session, username)
    if name_problem:
        code = status.HTTP_409_CONFLICT if "taken" in name_problem or "reserved" in name_problem else 422
        raise HTTPException(code, name_problem)
    user = User(username=username, password_hash=hash_password(body.password))
    session.add(user)
    try:
        session.commit()
    except IntegrityError:  # someone grabbed the same name a moment earlier
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "That username was just taken. Try another.")
    session.refresh(user)
    set_login_cookie(response, user)
    return user_out(user)


# Live "is this username free?" for the sign-up and Settings forms. Limited per
# visitor so it can't be used to list every account quickly.
_name_checks: dict[str, list[float]] = {}
NAME_CHECKS_PER_MINUTE = 60


@router.get("/username-available")
def username_available(request: Request, username: str = "", session: Session = Depends(get_session)):
    now = time.monotonic()
    who = client_ip(request)
    recent = [t for t in _name_checks.get(who, []) if now - t < 60]
    if len(recent) >= NAME_CHECKS_PER_MINUTE:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Slow down a little, then try again.")
    recent.append(now)
    _name_checks[who] = recent
    if len(_name_checks) > 5000:  # forget old visitors
        for key in [k for k, v in _name_checks.items() if not v or now - v[-1] > 60]:
            _name_checks.pop(key, None)

    me = user_from_token(request.cookies.get(COOKIE_NAME), session)
    name = username.strip().lower()
    problem = username_problem(session, name, me.id if me else None)
    taken = problem is not None and ("taken" in problem or "reserved" in problem)
    return {
        "username": name,
        "available": problem is None,
        "yours": bool(me and me.username == name),
        "reason": problem,
        "suggestions": suggest_usernames(session, name) if taken else [],
    }


# Checked against when the username doesn't exist, so a wrong username takes as long
# as a wrong password and can't be told apart by timing.
_DUMMY_HASH = bcrypt.hashpw(b"no-such-user", bcrypt.gensalt()).decode()


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response, session: Session = Depends(get_session)):
    ip = client_ip(request)
    wait = login_ip_limiter.blocked(f"ip:{ip}")
    if wait:
        raise too_many_logins(wait)
    user = session.exec(select(User).where(User.username == body.username.strip().lower())).first()
    # An account that has had too many wrong passwords recently pauses for a while,
    # whoever is trying. Correct guesses during the pause are refused too.
    wait = login_wait_seconds(user)
    if wait:
        raise too_many_logins(wait)
    if user is None or user.is_bot:
        verify_password(body.password, _DUMMY_HASH)
        login_ip_limiter.hit(f"ip:{ip}")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong username or password.")
    if not verify_password(body.password, user.password_hash):
        record_login_failure(user)
        session.add(user)
        session.commit()
        login_ip_limiter.hit(f"ip:{ip}")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong username or password.")
    if user.login_failures:
        clear_login_failures(user)
        session.add(user)
        session.commit()
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
def logout(request: Request, response: Response, session: Session = Depends(get_session)):
    """Log out this browser. The cookie is also cancelled on the server, so a copy
    of it (from another device or a stolen backup) stops working too."""
    payload = _token_claims(request.cookies.get(COOKIE_NAME))
    if payload and payload.get("jti") and session.get(RevokedToken, str(payload["jti"])) is None:
        expires = datetime.fromtimestamp(int(payload.get("exp", 0)), tz=timezone.utc)
        session.add(RevokedToken(jti=str(payload["jti"]), expires_at=expires))
        session.commit()
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return user_out(user)
