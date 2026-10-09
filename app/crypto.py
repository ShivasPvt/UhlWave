"""Encrypted storage: chat messages and files are encrypted inside the database.

Everything goes through two column types used in models.py:

  EncryptedText    message text, file names, what @ai read from files
  EncryptedBytes   the bytes of photos, voice notes, files and profile pictures

They encrypt on the way into the database and decrypt on the way out, so the
rest of the code reads and writes normal strings and bytes. Anyone who gets a
copy of chat.db (or the Postgres database) sees only scrambled data.

The cipher is AES-256-GCM, which also detects tampering. Each value gets its own
random 12-byte nonce. Stored form:  text  "enc1:" + base64(nonce + ciphertext)
                                    bytes b"enc1" + nonce + ciphertext

THE KEY: set MESSAGE_KEY in .env (make one with
    python -c "import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
). Without it, a key is created once in a file called `.message_key` next to the
app (git-ignored). LOSE THE KEY AND THE MESSAGES CAN'T BE READ, so when you
deploy, put the same key in the host's environment and keep a copy somewhere safe.

What this protects: a stolen database file or backup. What it doesn't: someone
who controls the running server (it holds the key). For that, use secret chats,
which are end-to-end encrypted in the browser (see static/e2ee.js).
"""
import base64
import hashlib
import logging
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import LargeBinary, Text
from sqlalchemy.types import TypeDecorator

log = logging.getLogger(__name__)

TEXT_PREFIX = "enc1:"
BYTES_PREFIX = b"enc1"
KEY_FILE = Path(__file__).resolve().parents[1] / ".message_key"


def _load_key() -> bytes:
    raw = os.getenv("MESSAGE_KEY", "").strip()
    if not raw and KEY_FILE.exists():
        raw = KEY_FILE.read_text().strip()
    if not raw:
        raw = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
        try:
            KEY_FILE.write_text(raw + "\n")
            log.warning("Created a new message encryption key in %s. Back it up, or set MESSAGE_KEY.", KEY_FILE)
        except OSError:
            log.error("Couldn't save the encryption key; set MESSAGE_KEY in .env or messages will be unreadable after a restart.")
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except ValueError:
        key = b""
    if len(key) == 32:
        return key
    # Not exactly 32 base64 bytes (e.g. a host-generated random string): any long
    # random value still works, stretched to a 32-byte key with SHA-256.
    if len(raw) >= 32:
        return hashlib.sha256(raw.encode()).digest()
    raise RuntimeError("MESSAGE_KEY must be 32 random bytes, base64-encoded (44 characters).")


_aes = AESGCM(_load_key())


def encrypt_bytes(data: bytes) -> bytes:
    nonce = secrets.token_bytes(12)
    return BYTES_PREFIX + nonce + _aes.encrypt(nonce, data, None)


def decrypt_bytes(blob: bytes) -> bytes:
    if not blob.startswith(BYTES_PREFIX):
        return blob  # stored before encryption was switched on
    body = blob[len(BYTES_PREFIX):]
    return _aes.decrypt(body[:12], body[12:], None)


def encrypt_text(text: str) -> str:
    return TEXT_PREFIX + base64.b64encode(encrypt_bytes(text.encode())[len(BYTES_PREFIX):]).decode()


def decrypt_text(value: str) -> str:
    if not value.startswith(TEXT_PREFIX):
        return value  # stored before encryption was switched on
    body = base64.b64decode(value[len(TEXT_PREFIX):])
    return _aes.decrypt(body[:12], body[12:], None).decode()


def is_encrypted_text(value: str | None) -> bool:
    return bool(value) and value.startswith(TEXT_PREFIX)


class EncryptedText(TypeDecorator):
    """A text column that is encrypted at rest. Don't filter on it in SQL
    (the database only sees ciphertext); filter in Python after loading."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else encrypt_text(value)

    def process_result_value(self, value, dialect):
        return None if value is None else decrypt_text(value)


class EncryptedBytes(TypeDecorator):
    """A binary column that is encrypted at rest."""

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else encrypt_bytes(bytes(value))

    def process_result_value(self, value, dialect):
        return None if value is None else decrypt_bytes(bytes(value))
