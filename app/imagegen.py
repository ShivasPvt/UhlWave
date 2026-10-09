"""Making pictures from a description.

Two ways in:
  * type  /imagine a red panda reading in the library  in any chat
  * ask @ai to draw something; it calls the generate_image tool (see tools.py)

Either way @ai posts the picture in the chat as a normal photo, so it's saved
and everyone in the chat sees it.

Providers (set IMAGE_PROVIDER in .env, or leave it on "auto"):
  huggingface   FLUX.1-schnell through Hugging Face (needs HF_TOKEN; free monthly credits)
  pollinations  Pollinations (POLLINATIONS_API_KEY recommended; without a key it
                tries the old keyless address, which may be slow or switched off)
"""
import contextvars
import logging
import time
from collections import defaultdict, deque
from urllib.parse import quote

import httpx

from .config import (
    HF_IMAGE_MODEL, HF_TOKEN, IMAGE_PROVIDER, IMAGES_PER_HOUR, POLLINATIONS_API_KEY, POLLINATIONS_MODEL,
)

log = logging.getLogger(__name__)

MAX_PROMPT = 400
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}


class ImageGenError(Exception):
    """Something to tell the person in plain words."""


# ---- rate limit -------------------------------------------------------------------

_made: dict[str, deque] = defaultdict(deque)


def check_quota(key: str) -> None:
    """Allow IMAGES_PER_HOUR pictures per person (or per chat, for @ai) each hour."""
    now = time.monotonic()
    times = _made[key]
    while times and now - times[0] > 3600:
        times.popleft()
    if len(times) >= IMAGES_PER_HOUR:
        minutes = int((3600 - (now - times[0])) // 60) + 1
        raise ImageGenError(f"That's {IMAGES_PER_HOUR} pictures this hour, the limit. Try again in {minutes} min.")
    times.append(now)


# ---- providers --------------------------------------------------------------------

def _provider_order() -> list[str]:
    if IMAGE_PROVIDER in ("huggingface", "pollinations"):
        return [IMAGE_PROVIDER]
    return ["huggingface", "pollinations"] if HF_TOKEN else ["pollinations"]


async def _huggingface(client: httpx.AsyncClient, prompt: str) -> httpx.Response:
    if not HF_TOKEN:
        raise ImageGenError("Image generation needs HF_TOKEN in .env for Hugging Face.")
    return await client.post(
        f"https://router.huggingface.co/hf-inference/models/{HF_IMAGE_MODEL}",
        headers={"Authorization": f"Bearer {HF_TOKEN}", "Accept": "image/png"},
        json={"inputs": prompt},
    )


async def _pollinations(client: httpx.AsyncClient, prompt: str) -> httpx.Response:
    params = {"model": POLLINATIONS_MODEL, "width": 1024, "height": 1024, "nologo": "true"}
    if POLLINATIONS_API_KEY:
        return await client.get(
            f"https://gen.pollinations.ai/image/{quote(prompt, safe='')}",
            params={**params, "key": POLLINATIONS_API_KEY},
        )
    # No key: the older free address. It may be slow, or turned off one day.
    return await client.get(f"https://image.pollinations.ai/prompt/{quote(prompt, safe='')}", params=params)


async def generate_image(prompt: str) -> tuple[bytes, str]:
    """Make one picture. Returns (image bytes, content type). Raises ImageGenError."""
    prompt = " ".join((prompt or "").split())[:MAX_PROMPT]
    if len(prompt) < 3:
        raise ImageGenError("Describe the picture you want, like: /imagine a snowy mountain at sunrise")
    problems = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=10), follow_redirects=True) as client:
        for provider in _provider_order():
            try:
                response = await (_huggingface if provider == "huggingface" else _pollinations)(client, prompt)
            except ImageGenError as error:
                problems.append(str(error))
                continue
            except httpx.TimeoutException:
                problems.append(f"{provider} took too long")
                continue
            except Exception:
                log.warning("Image request to %s failed", provider, exc_info=True)
                problems.append(f"couldn't reach {provider}")
                continue
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if response.status_code == 200 and content_type in IMAGE_TYPES and response.content:
                return response.content, content_type
            log.warning("%s returned %s %s: %s", provider, response.status_code, content_type, response.text[:200] if not content_type.startswith("image/") else "")
            if response.status_code in (401, 403):
                problems.append(f"{provider} rejected the key")
            elif response.status_code == 402:
                problems.append(f"{provider} is out of free credits")
            elif response.status_code == 429:
                problems.append(f"{provider} is busy")
            else:
                problems.append(f"{provider} couldn't make it")
    raise ImageGenError("I couldn't make that picture (" + "; ".join(problems) + "). Try again in a minute.")


# ---- pictures @ai made while answering ---------------------------------------------
# The AI's generate_image tool runs inside ai_reply(). Pictures it makes are collected
# here and posted to the chat once the AI has finished its answer.

made_images: contextvars.ContextVar[list | None] = contextvars.ContextVar("made_images", default=None)
image_quota_key: contextvars.ContextVar[str] = contextvars.ContextVar("image_quota_key", default="ai")
