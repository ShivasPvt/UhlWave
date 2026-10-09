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
# Groq retired llama-3.1-8b-instant in August 2026; gpt-oss-20b is its replacement.
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-20b")

# How many recent messages the AI sees as context when it replies.
AI_CONTEXT_MESSAGES = int(os.getenv("AI_CONTEXT_MESSAGES", "20"))

# ---- What the AI can look at besides text (see app/ai_media.py) ----
# Photos go to a model that can see images; voice notes are turned into text first.
LLM_VISION_MODEL = os.getenv("LLM_VISION_MODEL", "qwen/qwen3.8-27b")
LLM_TRANSCRIBE_MODEL = os.getenv("LLM_TRANSCRIBE_MODEL", "whisper-large-v3-turbo")
# Limits, so one big upload can't slow the AI down or run up the bill.
AI_MAX_IMAGES = int(os.getenv("AI_MAX_IMAGES", "2"))                         # newest photos it looks at per reply
AI_MAX_IMAGE_BYTES = int(os.getenv("AI_MAX_IMAGE_BYTES", str(4 * 1024 * 1024)))  # bigger photos are skipped
AI_MAX_FILE_CHARS = int(os.getenv("AI_MAX_FILE_CHARS", "12000"))             # text read from each file
AI_MAX_PDF_PAGES = int(os.getenv("AI_MAX_PDF_PAGES", "20"))

# ---- Tools @ai can use: weather and places (see app/tools.py) ----
AI_TOOLS = os.getenv("AI_TOOLS", "true").lower() == "true"
# Where "here" is when nobody has shared a location: the IIT Mandi campus.
AI_DEFAULT_PLACE = os.getenv("AI_DEFAULT_PLACE", "IIT Mandi, Kamand, Himachal Pradesh")
AI_DEFAULT_LATITUDE = float(os.getenv("AI_DEFAULT_LATITUDE", "31.776"))
AI_DEFAULT_LONGITUDE = float(os.getenv("AI_DEFAULT_LONGITUDE", "76.986"))

BOT_USERNAME = "ai"

# Google sign-in ("Continue with Google"). Create an OAuth client ID of type
# "Web application" at https://console.cloud.google.com/apis/credentials and
# add your site (e.g. http://localhost:8000) under "Authorized JavaScript origins".
# Leave empty to switch Google sign-in off.
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()

# Optional: only let people with emails at this domain in, e.g. "students.iitmandi.ac.in".
# Leave empty to allow any Google account (including plain @gmail.com).
GOOGLE_ALLOWED_DOMAIN = os.getenv("GOOGLE_ALLOWED_DOMAIN", "").strip().lower().lstrip("@")

# ---- GIFs and stickers (see app/media_search.py) ----
# Free key: https://developers.giphy.com/dashboard/ ("Create an API key", choose API).
# Without one, the built-in stickers still work; GIF search is just switched off.
GIPHY_API_KEY = os.getenv("GIPHY_API_KEY", "").strip()
GIPHY_RATING = os.getenv("GIPHY_RATING", "pg-13")

# ---- Image generation: "/imagine a red panda" or "@ai draw …" (see app/imagegen.py) ----
# "auto" tries Hugging Face if HF_TOKEN is set, otherwise Pollinations.
IMAGE_PROVIDER = os.getenv("IMAGE_PROVIDER", "auto").strip().lower()
# Pollinations (https://enter.pollinations.ai): free credits; a key makes it reliable.
POLLINATIONS_API_KEY = os.getenv("POLLINATIONS_API_KEY", "").strip()
POLLINATIONS_MODEL = os.getenv("POLLINATIONS_MODEL", "flux")
# Hugging Face (https://huggingface.co/settings/tokens): a free account comes with monthly credits.
HF_TOKEN = os.getenv("HF_TOKEN", "").strip()
HF_IMAGE_MODEL = os.getenv("HF_IMAGE_MODEL", "black-forest-labs/FLUX.1-schnell")
# How many pictures one person can make per hour, so nobody burns through the free credits.
IMAGES_PER_HOUR = int(os.getenv("IMAGES_PER_HOUR", "10"))

# ---- Voice and video calls (see app/calls.py) ----
# Calls connect browsers directly. STUN finds a route on most networks; strict
# networks (some colleges, mobile data) also need a TURN relay. Free TURN:
# https://www.metered.ca/stun-turn  -> paste the URL(s), username and password here.
STUN_URLS = [u.strip() for u in os.getenv("STUN_URLS", "stun:stun.l.google.com:19302,stun:stun1.l.google.com:19302").split(",") if u.strip()]
TURN_URLS = [u.strip() for u in os.getenv("TURN_URLS", "").split(",") if u.strip()]
TURN_USERNAME = os.getenv("TURN_USERNAME", "").strip()
TURN_CREDENTIAL = os.getenv("TURN_CREDENTIAL", "").strip()
