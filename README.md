# AI Proctoring Engine

This repository implements the data-model layer defined by `proctoring-engine-v1-spec.md`: an auditable PostgreSQL schema, a minimal FastAPI service shell, Alembic schema setup, and unit tests for the stated integrity boundaries.

## Scope of this implementation

Implemented now:

- PostgreSQL-ready ORM models and constraints for sessions, participants, enrollment references, telemetry, flags, evidence, configurable policy, termination records, exemptions, and reviews.
- A versioned initial Alembic migration and an immutable `TerminationRecord` database trigger for PostgreSQL.
- A minimal FastAPI health endpoint so the service has a runnable application boundary.
- SQLite-backed unit tests for confidence limits, timestamp ordering, foreign keys, policy ordering, telemetry links, and ORM-level termination-record immutability.

Not implemented yet: webcam/browser clients, WebSocket transport, LTI launch and callback verification, object/face/audio inference workers, S3 storage adapters, evidence encryption/key management, and any production deployment configuration. Those require subsequent implementation phases described in [docs/CLAUDE_HANDOFF.md](docs/CLAUDE_HANDOFF.md).

## Local setup

Python 3.11 or newer and PostgreSQL are required for normal service use.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
alembic upgrade head
uvicorn proctoring_engine.api:app --reload
```

The default database URL is intentionally not usable without configuration. Set `DATABASE_URL` to a PostgreSQL database before applying migrations.

## Verification

```powershell
pytest
```

The test suite uses SQLite only for portable constraint tests. Apply the Alembic migration to PostgreSQL before claiming production readiness; PostgreSQL-specific vector/search tuning and the termination immutability trigger must be checked there.

