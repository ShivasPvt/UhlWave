"""Database connection. SQLite locally, Postgres in production; same code for both."""
from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from .config import DATABASE_URL

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)


def init_db() -> None:
    from . import models  # noqa: F401  (importing registers the tables)

    SQLModel.metadata.create_all(engine)
    _add_missing_user_columns()
    _widen_encrypted_columns()
    encrypt_old_rows()


# Columns added after the first version. (table, column, SQL type, index to create or None)
_NEW_COLUMNS = [
    ("users", "email", "VARCHAR(254)", "unique"),
    ("users", "google_sub", "VARCHAR(255)", "unique"),
    ("users", "show_presence", "BOOLEAN NOT NULL DEFAULT TRUE", None),
    ("users", "send_typing", "BOOLEAN NOT NULL DEFAULT TRUE", None),
    ("users", "disappear_seconds", "INTEGER NOT NULL DEFAULT 0", None),
    ("users", "token_version", "INTEGER NOT NULL DEFAULT 0", None),
    ("messages", "expires_at", "TIMESTAMP", "index"),
    ("users", "send_read_receipts", "BOOLEAN NOT NULL DEFAULT TRUE", None),
    ("conversations", "created_by", "INTEGER", None),
    ("messages", "attachment_id", "INTEGER", None),
    ("messages", "edited_at", "TIMESTAMP", None),
    ("messages", "deleted_at", "TIMESTAMP", None),
    ("attachments", "ai_text", "TEXT", None),
    ("messages", "latitude", "FLOAT", None),
    ("messages", "longitude", "FLOAT", None),
    ("users", "bio", "VARCHAR(160)", None),
    ("users", "avatar_id", "INTEGER", None),
    ("users", "is_private", "BOOLEAN NOT NULL DEFAULT FALSE", None),
    ("users", "lock_pin_hash", "VARCHAR(100)", None),
    ("users", "appearance", "TEXT", None),
    ("conversations", "description", "VARCHAR(500)", None),
    ("conversations", "avatar_id", "INTEGER", None),
    ("conversations", "invite_code", "VARCHAR(40)", "unique"),
    ("conversation_members", "role", "VARCHAR(10) NOT NULL DEFAULT 'member'", None),
    ("conversation_members", "tag", "VARCHAR(30)", None),
    ("conversation_members", "archived", "BOOLEAN NOT NULL DEFAULT FALSE", None),
    ("conversation_members", "locked", "BOOLEAN NOT NULL DEFAULT FALSE", None),
    ("conversation_members", "category_id", "INTEGER", None),
    ("messages", "media_url", "VARCHAR(500)", None),
    ("messages", "pinned_at", "TIMESTAMP", None),
    ("messages", "pinned_by", "INTEGER", None),
    ("messages", "reply_to_id", "INTEGER", None),
    ("users", "e2ee_public_key", "TEXT", None),
    ("users", "e2ee_private_wrapped", "TEXT", None),
    ("users", "e2ee_updated_at", "TIMESTAMP", None),
]

# Run once, right after a column is first added, to fill it in for old rows.
_BACKFILL = {
    # Whoever made a group before roles existed becomes its admin.
    ("conversation_members", "role"): (
        "UPDATE conversation_members SET role = 'admin' WHERE EXISTS ("
        "SELECT 1 FROM conversations c WHERE c.id = conversation_members.conversation_id "
        "AND c.type = 'group' AND c.created_by = conversation_members.user_id)"
    ),
}


def _add_missing_user_columns() -> None:
    """create_all() makes new tables but never changes existing ones. Databases
    made by an older version lack these columns, so add them here."""
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, name, sql_type, index in _NEW_COLUMNS:
            if name in {c["name"] for c in inspector.get_columns(table)}:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))
            if (table, name) in _BACKFILL:
                conn.execute(text(_BACKFILL[(table, name)]))
            if index:
                unique = "UNIQUE " if index == "unique" else ""
                conn.execute(text(f"CREATE {unique}INDEX IF NOT EXISTS ix_{table}_{name} ON {table} ({name})"))


def get_session():
    """FastAPI dependency: one database session per request."""
    with Session(engine) as session:
        yield session


def _widen_encrypted_columns() -> None:
    """Encrypted values are longer than the plain ones. Postgres enforces column
    lengths, so older databases need these columns turned into TEXT."""
    if engine.dialect.name != "postgresql":
        return  # SQLite doesn't enforce lengths
    with engine.begin() as conn:
        for table, column in (("messages", "content"), ("attachments", "filename")):
            conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE TEXT"))


def encrypt_old_rows(batch: int = 500) -> int:
    """Encrypt anything stored before encryption was switched on. Safe to run on
    every start: rows that are already encrypted are skipped. Returns how many it changed."""
    from .crypto import BYTES_PREFIX, TEXT_PREFIX, encrypt_bytes, encrypt_text

    changed = 0
    with engine.begin() as conn:
        for table, column in (("messages", "content"), ("attachments", "filename"), ("attachments", "ai_text"), ("poll_options", "text")):
            rows = conn.execute(text(
                f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL AND {column} NOT LIKE :p"
            ), {"p": TEXT_PREFIX + "%"}).all()
            for row_id, value in rows:
                conn.execute(text(f"UPDATE {table} SET {column} = :v WHERE id = :id"), {"v": encrypt_text(value), "id": row_id})
                changed += 1
        last_id = 0
        while True:
            rows = conn.execute(text(
                "SELECT id, data FROM attachments WHERE id > :last ORDER BY id LIMIT :n"
            ), {"last": last_id, "n": batch}).all()
            if not rows:
                break
            for row_id, data in rows:
                last_id = row_id
                data = bytes(data)
                if not data.startswith(BYTES_PREFIX):
                    conn.execute(text("UPDATE attachments SET data = :d WHERE id = :id"), {"d": encrypt_bytes(data), "id": row_id})
                    changed += 1
    return changed
