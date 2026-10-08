"""The AI participant. Talks to any OpenAI-compatible chat API (Groq, Gemini,
OpenAI...) using plain HTTP, so there's no vendor SDK to learn.

The API key is read on the server only. It never reaches the browser.
"""
import logging

import httpx

from .config import BOT_USERNAME, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    f"You are @{BOT_USERNAME}, a helpful assistant inside a student chat app. "
    "You can see the recent conversation. Each human message is prefixed with the "
    "sender's username. Reply briefly and conversationally, in plain text, as one "
    "chat message. Don't prefix your reply with your own name."
)


def build_messages(history: list[dict]) -> list[dict]:
    """Turn our stored messages into the format chat APIs expect."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in history:
        if m["sender_is_bot"]:
            messages.append({"role": "assistant", "content": m["content"]})
        else:
            messages.append({"role": "user", "content": f'{m["sender_username"]}: {m["content"]}'})
    return messages


async def generate_reply(history: list[dict]) -> str:
    if not LLM_API_KEY:
        return (
            "I'm not switched on yet. Add LLM_API_KEY to the server's .env file "
            "and restart, and I'll start replying properly."
        )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json={"model": LLM_MODEL, "messages": build_messages(history), "max_tokens": 500},
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        log.exception("AI request failed")
        return "I couldn't reach the AI service just now. Try again in a moment."
