"""Database connection. SQLite locally, Postgres in production; same code for both."""
from sqlmodel import Session, SQLModel, create_engine

from .config import DATABASE_URL

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)


def init_db() -> None:
    from . import models  # noqa: F401  (importing registers the tables)

    SQLModel.metadata.create_all(engine)


def get_session():
    """FastAPI dependency: one database session per request."""
    with Session(engine) as session:
        yield session
