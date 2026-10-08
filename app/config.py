"""All settings come from environment variables (or a local .env file).

Never hard-code secrets here. Copy .env.example to .env and fill it in.
"""
import os

from dotenv import load_dotenv

load_dotenv()


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "sqlite:///./chat.db")
    # Supabase / Neon / Render hand out "postgres://" or "postgresql://" URLs.
    # SQLAlchemy needs to be told which driver to use, so we add "+psycopg".
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


DATABASE_URL = _database_url()

JWT_SECRET = os.getenv("JWT_SECRET", "dev-only-secret-change-me-before-deploying")
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "72"))

# Set to "true" when deployed behind HTTPS (Render does this) so the
# login cookie is only ever sent over an encrypted connection.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"

# Any OpenAI-compatible chat completions API works: Groq, Gemini, OpenAI...
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "llama-3.1-8b-instant")

# How many recent messages the AI sees as context when it replies.
AI_CONTEXT_MESSAGES = int(os.getenv("AI_CONTEXT_MESSAGES", "20"))

BOT_USERNAME = "ai"
