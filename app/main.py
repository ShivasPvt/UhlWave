"""Entry point. Run locally with:  uvicorn app.main:app --reload"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, col, delete, select, update

from . import (
    api, appearance, auth, backup, calls, chat_requests, e2ee, folders, groups, media_search, messages, polls,
    profiles, settings, ws,
)
from .db import engine, init_db
from .models import Attachment, Message, Reaction, utcnow
from .services import ensure_bot_user

STATIC_DIR = Path(__file__).parent / "static"


def purge_expired_messages() -> int:
    """Permanently delete disappearing messages whose time is up."""
    with Session(engine) as session:
        expired = col(Message.expires_at).is_not(None) & (col(Message.expires_at) <= utcnow())
        expired_ids = select(Message.id).where(expired)
        attachment_ids = [a for a in session.exec(select(Message.attachment_id).where(expired)).all() if a]
        session.execute(delete(Reaction).where(col(Reaction.message_id).in_(expired_ids)))
        polls.delete_polls_for(session, expired_ids)
        session.execute(update(Message).where(col(Message.reply_to_id).in_(expired_ids)).values(reply_to_id=None))
        result = session.execute(delete(Message).where(expired))
        if attachment_ids:
            session.execute(delete(Attachment).where(col(Attachment.id).in_(attachment_ids)))
        session.commit()
        return result.rowcount or 0


async def purge_loop() -> None:
    # Expired messages are already hidden from every query; this clears them out of the database too.
    while True:
        try:
            purge_expired_messages()
        except Exception:
            logging.getLogger(__name__).exception("Couldn't purge expired messages")
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with Session(engine) as session:
        ensure_bot_user(session)
    purger = asyncio.create_task(purge_loop())
    yield
    purger.cancel()


app = FastAPI(title="UhlWave", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(api.router)
app.include_router(settings.router)
app.include_router(groups.router)
app.include_router(messages.router)
app.include_router(profiles.router)
app.include_router(chat_requests.router)
app.include_router(folders.router)
app.include_router(media_search.router)
app.include_router(appearance.router)
app.include_router(backup.router)
app.include_router(calls.router)
app.include_router(polls.router)
app.include_router(e2ee.router)
app.include_router(ws.router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _logged_in(request: Request) -> bool:
    with Session(engine) as session:
        return auth.user_from_token(request.cookies.get(auth.COOKIE_NAME), session) is not None


@app.get("/", include_in_schema=False)
def login_page(request: Request):
    if _logged_in(request):
        return RedirectResponse("/chat")
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/chat", include_in_schema=False)
def chat_page(request: Request):
    if not _logged_in(request):
        return RedirectResponse("/")
    return FileResponse(STATIC_DIR / "chat.html")


@app.get("/join/{code}", include_in_schema=False)
def join_link(code: str, request: Request):
    """Group invite links. Logged out? Log in first, then come back here."""
    safe = "".join(ch for ch in code if ch.isalnum() or ch in "-_")[:40]
    if not _logged_in(request):
        return RedirectResponse(f"/?next=/join/{safe}")
    return RedirectResponse(f"/chat#join={safe}")


@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True}
