"""Letting @ai understand photos, voice notes and files, not just text.

  photo       -> sent to a model that can see images (LLM_VISION_MODEL)
  voice note  -> turned into text with Whisper (LLM_TRANSCRIBE_MODEL)
  file        -> text pulled out of PDFs, Word documents and plain-text/code files

Everything has a size limit (see config.py) so one big upload can't slow the AI
down or run up the bill. Transcripts and extracted text are saved on the
attachment, so each file is only processed once.
"""
import asyncio
import base64
import html
import io
import logging
import re
import zipfile
from pathlib import PurePosixPath

import httpx
from sqlmodel import Session

from .config import (
    AI_MAX_FILE_CHARS, AI_MAX_IMAGE_BYTES, AI_MAX_IMAGES, AI_MAX_PDF_PAGES,
    LLM_API_KEY, LLM_BASE_URL, LLM_TRANSCRIBE_MODEL,
)
from .db import engine
from .models import Attachment

log = logging.getLogger(__name__)

# Files we read as plain text, by extension or content type.
TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".html", ".css",
    ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".hpp", ".cs", ".go", ".rs", ".rb",
    ".php", ".sql", ".sh", ".tex", ".ipynb", ".log", ".ini", ".toml",
}
TEXT_TYPES = {"application/json", "application/xml", "application/javascript", "application/sql", "application/csv"}
MAX_UNZIPPED_DOCX = 20 * 1024 * 1024  # refuse "zip bombs" that unpack to something huge


# ---- reading files -------------------------------------------------------------

def extract_text(filename: str, content_type: str, data: bytes) -> str | None:
    """The readable text of a file, cut to AI_MAX_FILE_CHARS. None if we can't read this kind of file."""
    extension = PurePosixPath(filename.lower()).suffix
    try:
        if content_type == "application/pdf" or extension == ".pdf":
            text = _pdf_text(data)
        elif extension == ".docx":
            text = _docx_text(data)
        elif content_type.startswith("text/") or content_type in TEXT_TYPES or extension in TEXT_EXTENSIONS:
            text = data.decode("utf-8", errors="replace")
        else:
            return None
    except Exception:
        log.warning("Couldn't read %s", filename, exc_info=True)
        return None

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > AI_MAX_FILE_CHARS:
        text = text[:AI_MAX_FILE_CHARS] + "\n[… the rest of the file was cut off]"
    return text


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = reader.pages[:AI_MAX_PDF_PAGES]
    text = "\n\n".join((page.extract_text() or "") for page in pages)
    if len(reader.pages) > AI_MAX_PDF_PAGES:
        text += f"\n[… only the first {AI_MAX_PDF_PAGES} of {len(reader.pages)} pages were read]"
    return text


def _docx_text(data: bytes) -> str:
    """A .docx is a zip with the text in word/document.xml. Pull out the paragraphs."""
    with zipfile.ZipFile(io.BytesIO(data)) as docx:
        if docx.getinfo("word/document.xml").file_size > MAX_UNZIPPED_DOCX:
            raise ValueError("document.xml is too large")
        xml = docx.read("word/document.xml").decode("utf-8", errors="replace")
    paragraphs = []
    for paragraph in xml.split("</w:p>"):
        runs = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", paragraph, flags=re.S)
        if runs:
            paragraphs.append(html.unescape("".join(runs)))
    return "\n".join(paragraphs)


def image_data_url(content_type: str, data: bytes) -> str | None:
    """The photo as a data: URL the vision model accepts, or None if it's too big."""
    if len(data) > AI_MAX_IMAGE_BYTES:
        return None
    return f"data:{content_type};base64,{base64.b64encode(data).decode()}"


# ---- voice notes -----------------------------------------------------------------

async def transcribe(filename: str, content_type: str, data: bytes) -> str | None:
    """Speech to text with Whisper. None if it didn't work (no key, network, etc.)."""
    if not LLM_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{LLM_BASE_URL}/audio/transcriptions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                data={"model": LLM_TRANSCRIBE_MODEL, "response_format": "json"},
                files={"file": (filename, data, content_type)},
            )
            response.raise_for_status()
            return (response.json().get("text") or "").strip()
    except Exception:
        log.warning("Couldn't transcribe %s", filename, exc_info=True)
        return None


# ---- turning chat history into what the AI sees ---------------------------------

def _load(attachment_id: int) -> Attachment | None:
    with Session(engine) as session:
        return session.get(Attachment, attachment_id)


def _remember(attachment_id: int, text: str) -> None:
    with Session(engine) as session:
        attachment = session.get(Attachment, attachment_id)
        if attachment is not None:
            attachment.ai_text = text
            session.add(attachment)
            session.commit()


async def prepare_for_ai(messages: list[dict]) -> list[dict]:
    """Turn messages (as sent to browsers) into the history the AI reads.

    Each item has sender_is_bot, sender_username, content, and optionally
    images (data: URLs). Deleted messages and grey system lines are left out.
    """
    visible = [m for m in messages if not m["deleted"] and m["type"] != "system"]
    # Only the newest few photos are actually shown to the AI.
    photo_ids = [m["id"] for m in visible if m["type"] == "image" and m["attachment"]]
    shown_photos = set(photo_ids[-AI_MAX_IMAGES:]) if AI_MAX_IMAGES > 0 else set()

    history = []
    for m in visible:
        item = {"sender_is_bot": m["sender_is_bot"], "sender_username": m["sender_username"], "content": m["content"]}
        a = m["attachment"]
        if m["type"] in ("gif", "sticker"):
            item["content"] = f'[{"GIF" if m["type"] == "gif" else "sticker"}: {m["content"]}]'
        elif m["type"] == "poll" and m.get("poll"):
            options = ", ".join(o["text"] for o in m["poll"]["options"])
            item["content"] = f'[poll] {m["content"]} (options: {options})'
        elif m["type"] == "call":
            item["content"] = f'[call] {m["content"]}'
        elif m["type"] == "location" and m.get("location"):
            loc = m["location"]
            item["content"] = f'[shared location] {m["content"]} (latitude {loc["latitude"]}, longitude {loc["longitude"]})'
        elif m["type"] in ("image", "voice", "file") and a:
            item["content"], images = await _describe(m, a, show_photo=m["id"] in shown_photos)
            if images:
                item["images"] = images
        history.append(item)
    return history


async def _describe(message: dict, meta: dict, show_photo: bool) -> tuple[str, list[str]]:
    name = meta["filename"]
    attachment = await asyncio.to_thread(_load, meta["id"])
    if attachment is None:
        return f"[attachment: {name} (no longer available)]", []

    if message["type"] == "image":
        if not show_photo:
            return f"[earlier photo: {name}]", []
        url = image_data_url(attachment.content_type, attachment.data)
        if url is None:
            return f"[photo: {name} (too large for you to view)]", []
        return f"[photo: {name}]", [url]

    if message["type"] == "voice":
        text = attachment.ai_text
        if text is None:
            text = await transcribe(name, attachment.content_type, attachment.data)
            if text is None:
                return "[voice note (couldn't be transcribed)]", []
            await asyncio.to_thread(_remember, attachment.id, text)
        return f"[voice note] {text or '(no speech heard)'}", []

    # Other files: read once, then reuse.
    text = attachment.ai_text
    if text is None:
        text = await asyncio.to_thread(extract_text, name, attachment.content_type, attachment.data)
        await asyncio.to_thread(_remember, attachment.id, text or "")
    if not text:
        return f"[file: {name} (you can't read this kind of file)]", []
    return f'[file: {name}]\n"""\n{text}\n"""', []
