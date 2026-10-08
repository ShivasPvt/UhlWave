"""Entry point. Run locally with:  uvicorn app.main:app --reload"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session

from . import api, auth, ws
from .db import engine, init_db
from .services import ensure_bot_user

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with Session(engine) as session:
        ensure_bot_user(session)
    yield


app = FastAPI(title="Valley chat", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(api.router)
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


@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True}
