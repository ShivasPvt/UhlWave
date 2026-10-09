"""How the chat looks for you: wallpaper, bubble colours and shape, font and size.

Saved on the server, so your look follows you to every device you log in on.

  GET    /api/settings/appearance        your current choices (defaults filled in)
  PUT    /api/settings/appearance        save choices (any subset of the fields)
  POST   /api/settings/wallpaper         upload a photo as your wallpaper (raw image body)
  GET    /api/wallpapers/{id}            show your wallpaper photo (only to you)
"""
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session

from .auth import get_current_user
from .db import get_session
from .models import Attachment, User
from .profiles import delete_attachment, read_image_upload, save_image, serve_image

router = APIRouter(prefix="/api", tags=["appearance"])

MAX_WALLPAPER_BYTES = 5 * 1024 * 1024
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
PATTERNS = ["contours", "dots", "grid", "snow"]
# Fonts offered in Settings. All are on Google Fonts; the page loads the one you pick.
FONTS = ["Atkinson Hyperlegible", "Nunito", "Lexend", "Merriweather", "Space Grotesk", "Comic Neue", "JetBrains Mono"]
SHAPES = ["round", "soft", "square"]

DEFAULTS = {
    "wallpaper": "default",          # "default", "color", "pattern" or "image"
    "wallpaper_value": None,         # "#hex", a pattern name, or the photo's id
    "bubble_mine": "#0e9f96",
    "bubble_theirs": "#ffffff",
    "bubble_shape": "round",
    "font": "Atkinson Hyperlegible",
    "font_size": 15,
}


def appearance_out(user: User) -> dict:
    saved = {}
    if user.appearance:
        try:
            saved = json.loads(user.appearance)
        except ValueError:
            saved = {}
    out = {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}
    out["wallpaper_url"] = f"/api/wallpapers/{out['wallpaper_value']}" if out["wallpaper"] == "image" and out["wallpaper_value"] else None
    out["choices"] = {"patterns": PATTERNS, "fonts": FONTS, "shapes": SHAPES, "font_sizes": [13, 20]}
    return out


class AppearanceBody(BaseModel):
    wallpaper: str | None = Field(default=None, pattern="^(default|color|pattern)$")
    wallpaper_value: str | None = Field(default=None, max_length=20)
    bubble_mine: str | None = None
    bubble_theirs: str | None = None
    bubble_shape: str | None = None
    font: str | None = None
    font_size: int | None = Field(default=None, ge=13, le=20)

    @field_validator("bubble_mine", "bubble_theirs")
    @classmethod
    def _colour(cls, v):
        if v is not None and not HEX.match(v):
            raise ValueError("Colours look like #12ab9f.")
        return v

    @field_validator("bubble_shape")
    @classmethod
    def _shape(cls, v):
        if v is not None and v not in SHAPES:
            raise ValueError("Pick one of the offered shapes.")
        return v

    @field_validator("font")
    @classmethod
    def _font(cls, v):
        if v is not None and v not in FONTS:
            raise ValueError("Pick one of the offered fonts.")
        return v


def _save(session: Session, user: User, values: dict) -> dict:
    current = appearance_out(user)
    old_image = current["wallpaper_value"] if current["wallpaper"] == "image" else None
    merged = {k: current[k] for k in DEFAULTS}
    merged.update(values)
    new_image = merged["wallpaper_value"] if merged["wallpaper"] == "image" else None
    if old_image and str(old_image) != str(new_image):
        delete_attachment(session, int(old_image))
    user.appearance = json.dumps(merged)
    session.add(user)
    session.commit()
    return appearance_out(user)


@router.get("/settings/appearance")
def get_appearance(me: User = Depends(get_current_user)):
    return appearance_out(me)


@router.put("/settings/appearance")
def set_appearance(body: AppearanceBody, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    values = body.model_dump(exclude_none=True)
    if "wallpaper" in values:
        kind = values["wallpaper"]
        value = values.get("wallpaper_value")
        if kind == "color" and not (value and HEX.match(value)):
            raise HTTPException(422, "Pick a wallpaper colour.")
        if kind == "pattern" and value not in PATTERNS:
            raise HTTPException(422, "Pick one of the offered patterns.")
        if kind == "default":
            values["wallpaper_value"] = None
    else:
        values.pop("wallpaper_value", None)
    return _save(session, me, values)


@router.post("/settings/wallpaper")
async def upload_wallpaper(request: Request, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    data, content_type = await read_image_upload(request, MAX_WALLPAPER_BYTES)
    attachment = save_image(session, me, data, content_type, "wallpaper")
    return _save(session, me, {"wallpaper": "image", "wallpaper_value": str(attachment.id)})


@router.get("/wallpapers/{attachment_id}")
def show_wallpaper(attachment_id: int, me: User = Depends(get_current_user), session: Session = Depends(get_session)):
    current = appearance_out(me)
    if current["wallpaper"] != "image" or str(current["wallpaper_value"]) != str(attachment_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Wallpaper not found.")
    attachment = session.get(Attachment, attachment_id)
    if attachment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Wallpaper not found.")
    return serve_image(attachment)
