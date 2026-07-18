"""Shared SQLite session fixture with relational constraints enabled."""

from __future__ import annotations

from collections.abc import Generator

import pytest
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session, sessionmaker

from proctoring_engine.database import build_engine
from proctoring_engine.models import Base


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    engine: Engine = build_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()

