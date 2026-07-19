"""Shared fixtures for the PostgreSQL integration test suite.

These tests are skipped unless ``INTEGRATION_DATABASE_URL`` is set in the
environment, so the SQLite-only unit tests can run unchanged in any
environment. When the variable is set, the tests apply the full migration
chain to a real PostgreSQL 15+ database and exercise the database-level
constraints (enum types, JSONB, the immutability triggers) that SQLite
cannot model.

The fixture tears the schema down between tests so each test gets a clean
slate, and yields a SQLAlchemy ``Session`` bound to that engine.
"""

from __future__ import annotations

import os
from collections.abc import Generator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker


def _integration_url() -> str | None:
    """Return the integration database URL, or None if not configured."""

    return os.environ.get("INTEGRATION_DATABASE_URL")


def _is_postgres(url: str) -> bool:
    """A narrow check that the URL targets PostgreSQL."""

    return url.startswith("postgres")


@pytest.fixture(scope="session")
def alembic_config() -> Config:
    """An Alembic ``Config`` bound to the project metadata.

    The test does not rely on the ``alembic.ini`` URL — it overrides the
    ``sqlalchemy.url`` option with the ``INTEGRATION_DATABASE_URL`` value
    so the same migration can be run against a local Postgres, a CI
    service container, or a developer-managed RDS instance.
    """

    url = _integration_url()
    if url is None:
        pytest.skip("INTEGRATION_DATABASE_URL is not set")

    root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    config = Config(os.path.join(root, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(root, "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    config.set_main_option("prepend_sys_path", f"{root}{os.pathsep}{root}/src")
    return config


@pytest.fixture(scope="session")
def integration_engine(alembic_config: Config) -> Generator[Engine, None, None]:
    """Apply migrations to a real PostgreSQL database and yield the engine.

    The schema is dropped and recreated between session runs so the test
    environment is deterministic. The cleanup is best-effort: a failure
    during teardown logs a warning rather than masking the test result.
    """

    url = alembic_config.get_main_option("sqlalchemy.url")
    assert url is not None
    if not _is_postgres(url):
        pytest.skip("INTEGRATION_DATABASE_URL is not a PostgreSQL URL")

    # Drop and recreate the database if possible. We do this by connecting
    # to the server's ``postgres`` admin database, terminating any
    # connections to the target, dropping, and recreating. This is the
    # standard "ephemeral CI database" pattern.
    _reset_database(url)

    engine = create_engine(url, pool_pre_ping=True, future=True)
    try:
        command.upgrade(alembic_config, "head")
        yield engine
    finally:
        try:
            _drop_schema(engine)
        finally:
            engine.dispose()


@pytest.fixture()
def db_session(integration_engine: Engine) -> Generator[Session, None, None]:
    """Yield a transactional ``Session`` against the integration engine.

    The session is bound to a SAVEPOINT so a single test can roll back
    without affecting other tests in the same session-scoped engine.
    """

    connection = integration_engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


@pytest.fixture()
def integration_engine_session(db_session: Session) -> Generator[Session, None, None]:
    """Alias of ``db_session`` for tests that prefer a more explicit name."""

    yield db_session


def _reset_database(url: str) -> None:
    """Drop and recreate the target database.

    The URL is parsed with a small hand-rolled parser rather than pulling
    in ``sqlalchemy.engine.url.make_url`` so the test fixture has no
    hidden dependency on the rest of the package's import-time state.
    """

    from urllib.parse import urlparse

    parsed = urlparse(url)
    db_name = (parsed.path or "").lstrip("/")
    if not db_name:
        raise RuntimeError("INTEGRATION_DATABASE_URL must include a database name")

    admin_url = url.replace(f"/{db_name}", "/postgres", 1)
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": db_name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    finally:
        admin_engine.dispose()


def _drop_schema(engine: Engine) -> None:
    """Drop the schema so the next session starts clean."""

    with engine.begin() as conn:
        # Drop the trigger first because it depends on the table.
        conn.execute(text("DROP TRIGGER IF EXISTS flag_immutable ON flags"))
        conn.execute(text("DROP TRIGGER IF EXISTS termination_record_immutable ON termination_records"))
        conn.execute(text("DROP FUNCTION IF EXISTS prevent_flag_mutation()"))
        conn.execute(text("DROP FUNCTION IF EXISTS prevent_termination_record_mutation()"))
        # Let the rest cascade via the schema.
        for stmt in (
            "DROP TABLE IF EXISTS proctor_reviews CASCADE",
            "DROP TABLE IF EXISTS termination_records CASCADE",
            "DROP TABLE IF EXISTS evidence_artifacts CASCADE",
            "DROP TABLE IF EXISTS flag_telemetry_events CASCADE",
            "DROP TABLE IF EXISTS flags CASCADE",
            "DROP TABLE IF EXISTS telemetry_events CASCADE",
            "DROP TABLE IF EXISTS accommodation_exemptions CASCADE",
            "DROP TABLE IF EXISTS enrollment_references CASCADE",
            "DROP TABLE IF EXISTS exam_sessions CASCADE",
            "DROP TABLE IF EXISTS policy_configs CASCADE",
            "DROP TABLE IF EXISTS participants CASCADE",
            "DROP TABLE IF EXISTS admin_users CASCADE",
            "DROP TYPE IF EXISTS admin_role",
            "DROP TYPE IF EXISTS reference_material_policy",
            "DROP TYPE IF EXISTS session_status",
            "DROP TYPE IF EXISTS telemetry_modality",
            "DROP TYPE IF EXISTS flag_severity",
            "DROP TYPE IF EXISTS flag_status",
            "DROP TYPE IF EXISTS evidence_kind",
            "DROP TYPE IF EXISTS delivery_status",
            "DROP TYPE IF EXISTS review_decision",
        ):
            conn.execute(text(stmt))
