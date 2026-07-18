"""Database engine and schema initialization helpers."""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from proctoring_engine.config import get_settings
from proctoring_engine.models import Base


def build_engine(database_url: str | None = None) -> Engine:
    """Create an engine without opening a database connection."""

    url = database_url or get_settings().database_url
    options: dict[str, object] = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **options)


engine = build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Generator[Session, None, None]:
    """Yield one transactional session for an API request."""

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def install_postgres_immutability(database_engine: Engine) -> None:
    """Install the database-level guard that makes termination records append-only."""

    if database_engine.dialect.name != "postgresql":
        return

    with database_engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE OR REPLACE FUNCTION prevent_termination_record_mutation()
                RETURNS trigger AS $$
                BEGIN
                    RAISE EXCEPTION 'termination_records are immutable';
                END;
                $$ LANGUAGE plpgsql;
                """
            )
        )
        connection.execute(
            text(
                """
                DROP TRIGGER IF EXISTS termination_record_immutable
                ON termination_records;
                CREATE TRIGGER termination_record_immutable
                BEFORE UPDATE OR DELETE ON termination_records
                FOR EACH ROW EXECUTE FUNCTION prevent_termination_record_mutation();
                """
            )
        )


def initialize_schema(database_engine: Engine | None = None) -> None:
    """Create the schema for local development only; production uses Alembic."""

    target_engine = database_engine or engine
    Base.metadata.create_all(target_engine)
    install_postgres_immutability(target_engine)

