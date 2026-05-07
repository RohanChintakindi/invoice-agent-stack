"""Database engine + session helpers shared across all verticals."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlmodel import Session, SQLModel, create_engine

DEFAULT_DB_URL = "sqlite:///./invoice_stack.db"


def get_db_url() -> str:
    return os.getenv("DB_URL", DEFAULT_DB_URL)


def make_engine(db_url: str | None = None):
    url = db_url or get_db_url()
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args)


def init_schema(engine) -> None:
    """Create all tables. Imports models so SQLModel knows about them."""
    # Local imports avoid circular references at module load.
    from shared import models  # noqa: F401
    from voice_agent import memory_models  # noqa: F401

    SQLModel.metadata.create_all(engine)


@contextmanager
def session_scope(engine) -> Iterator[Session]:
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
