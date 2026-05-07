"""Shared pytest fixtures: in-memory SQLite engine + session per test."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlmodel import Session, SQLModel, create_engine

# Importing the model modules registers them with SQLModel.metadata.
import shared.models  # noqa: F401
import voice_agent.memory_models  # noqa: F401


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine) -> Iterator[Session]:
    with Session(engine) as session:
        yield session


@pytest.fixture
def payer(session) -> str:
    """Insert a basic Payer and return its id."""
    from shared.models import Payer

    p = Payer(payer_id="acme", name="Acme Corp")
    session.add(p)
    session.commit()
    return p.payer_id
