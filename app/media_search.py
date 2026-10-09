"""GIF and sticker search, through GIPHY.

The browser asks *our* server, and our server asks GIPHY, so the API key stays
on the server (a hackathon rule: no keys in client-side code).

  GET /api/gifs?q=cat&kind=gifs       search (no q = what's trending)
  GET /api/gifs?q=cat&kind=stickers   transparent stickers instead of GIFs
"""
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status

from .auth import get_current_user
from .config import GIPHY_API_KEY, GIPHY_RATING
from .models import User

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["gifs"])

GIPHY = "https://api.giphy.com/v1"


def _pick(images: dict, *names: str) -> dict:
    for name in names:
        if images.get(name, {}).get("url"):
            return images[name]
    return {}


def gif_out(item: dict) -> dict | None:
    images = item.get("images") or {}
    full = _pick(images, "fixed_height", "downsized", "original")
    small = _pick(images, "fixed_height_small", "fixed_width_small", "preview_gif", "fixed_height")
    if not full:
        return None
    return {
        "id": item.get("id"),
        "title": (item.get("title") or "").strip() or "GIF",
        "url": full["url"],
        "preview": small.get("url") or full["url"],
        "width": int(full.get("width") or 0) or None,
        "height": int(full.get("height") or 0) or None,
    }


@router.get("/gifs")
async def search_gifs(
    q: str = Query("", max_length=60),
    kind: str = Query("gifs", pattern="^(gifs|stickers)$"),
    me: User = Depends(get_current_user),
):
    if not GIPHY_API_KEY:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GIF search isn't set up yet. Add GIPHY_API_KEY to .env.")
    q = q.strip()
    endpoint = f"{GIPHY}/{kind}/{'search' if q else 'trending'}"
    params = {"api_key": GIPHY_API_KEY, "limit": 24, "rating": GIPHY_RATING, "bundle": "messaging_non_clips"}
    if q:
        params["q"] = q
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(endpoint, params=params)
        if response.status_code in (401, 403):
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GIPHY rejected the API key. Check GIPHY_API_KEY in .env.")
        if response.status_code == 429:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many GIF searches right now. Try again in a minute.")
        response.raise_for_status()
        data = response.json().get("data") or []
    except HTTPException:
        raise
    except Exception:
        log.warning("GIPHY request failed", exc_info=True)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Couldn't reach GIPHY. Try again in a moment.")
    return [g for g in (gif_out(item) for item in data) if g]
