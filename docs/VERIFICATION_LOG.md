# Verification Log

Last updated: 2026-07-18

## Planned verification

| Check | Command | Expected result |
|---|---|---|
| Dependency installation | `python -m pip install -e ".[dev]"` | Project and dev dependencies resolve |
| Model unit tests (SQLite) | `pytest tests --ignore=tests/integration` | All 19 boundary cases pass |
| Integration tests (PostgreSQL) | `INTEGRATION_DATABASE_URL=... pytest tests/integration` | All real-engine tests pass |
| Service import | `python -c "from proctoring_engine.api import app; print(app.version)"` | Prints `0.1.0` |
| Alembic migration upgrade | `alembic upgrade head` | Tables, enums, indexes, checks, triggers from both revisions applied |
| Alembic migration SQL compile | `alembic upgrade head --sql` | DDL generated without errors |

## Execution record (data-model layer)

| Check | Executed command | Result |
|---|---|---|
| Dependency installation | `pip install -e ".[dev]"` | Resolved |
| Model unit tests (initial) | `pytest tests/test_models.py` | 9 passed; one `PytestCacheWarning` from sandbox-restricted cache writes; tests unaffected |
| Model unit tests (regression) | `pytest tests/test_models.py -p no:cacheprovider` | 9 passed cleanly |
| Service health check | `GET /healthz` via `TestClient` | 200 OK, `{"status": "ok", "environment": "development"}` |
| PostgreSQL DDL compilation (initial revision) | `alembic upgrade head --sql` | Generated enum types, tables, constraints, indexes, the termination_record_immutable trigger function, and the trigger |

## Execution record (audit reconciliation + integration suite)

| Check | Executed command | Result |
|---|---|---|
| Model unit tests (post-reconciliation, first run) | `pytest tests --ignore=tests/integration` | **7 failed, 21 passed.** Six test defects introduced in the reconciliation turn, listed in `docs/KNOWN_ISSUES.md` §"Recent issue-resolution history." |
| Model unit tests (post-reconciliation, after fixes) | `pytest tests --ignore=tests/integration` | **28 logical cases passed** in 0.47s. All six defects fixed; see `docs/KNOWN_ISSUES.md` for the fix-by-fix record. |
| Alembic upgrade against PostgreSQL 15 | `alembic upgrade head` against `postgres:15-alpine` service container | All migrations applied; both `termination_record_immutable` and `flag_immutable` triggers installed; the `under_review`, `overturned`, and `needs_more_info` enum values created |
| Integration test suite | `INTEGRATION_DATABASE_URL=postgresql+psycopg://... pytest tests/integration` | 13 real-engine tests pass; the immutability triggers reject direct `UPDATE` and `DELETE` SQL as expected |
| Docker image smoke build | `docker build --tag proctoring-engine:ci-smoke --load .` | Image built; the runtime stage boots as a non-root user with the application source on a read-only rootfs |
| Local development environment | `.venv/Scripts/python.exe -c "import fastapi, sqlalchemy, alembic, psycopg"` | All four core deps import; FastAPI 0.139.2, SQLAlchemy 2.0.51, Alembic 1.18.5, psycopg 3.3.4 |
| FastAPI healthz smoke test | `.venv/Scripts/python.exe -c "from proctoring_engine.api import app; from starlette.testclient import TestClient; ..."` | 200 OK, `{"status": "ok", "environment": "development"}`. One benign `StarletteDeprecationWarning` about `httpx2` — see `docs/KNOWN_ISSUES.md` §1. |

## Not yet verified

- Live deployment to a real Kubernetes cluster (the manifests exist;
  the cluster is the next environment to provision).
- Postgres write throughput under a sustained flag-fire rate (the
  workload is read-light and write-bursty; a load test is the next
  environment to provision).
- The LTI 1.3 launch / callback path (next atomic layer).
- The WebSocket transport and the kill-switch flow (the next
  atomic layer after LTI).

## Test coverage implemented

### Unit (SQLite, 19 cases)

- Telemetry confidence accepts `0.0` and `1.0`.
- Telemetry confidence rejects values below `0.0` and above `1.0`
  before persistence.
- Flag confidence interval accepts valid triples
  (`(0, 0, 0)`, `(1, 1, 1)`, `(0.5, 0.5, 0.5)`, `(0.1, 0.5, 0.9)`).
- Flag confidence interval rejects invalid triples
  (negative components, components above 1, score outside the
  interval).
- `ExamSession` rejects an end time earlier than its start time.
- `ExamSession` rejects a non-existent participant foreign key.
- `ExamSession.status` defaults to `pending`.
- `ExamSession.accumulated_medium_score` defaults to 0.
- `ExamSession.accumulated_medium_score` rejects a negative value.
- `PolicyConfig` rejects a gaze warning threshold greater than its
  termination threshold.
- `PolicyConfig` rejects a gaze `min_duration_ms` exceeding the
  window.
- `PolicyConfig` rejects a negative `medium_score_termination_threshold`.
- `FlagTelemetryEvent` rejects duplicate links of the same telemetry
  event to one flag.
- `EnrollmentReference.embedding_model_version` rejects an empty
  string.
- `EvidenceArtifact.flag_id` unique constraint rejects a second
  artifact on the same flag.
- `TerminationRecord` rejects an ORM mutation after it is committed.
- `Flag` rejects an ORM mutation after it is committed.
- `Flag` rejects an ORM delete after it is committed.
- `Flag.triggered_termination` defaults to false.
- `Flag.suppressed_by_exemption_id` round-trips with the related
  `AccommodationExemption`.

### Integration (PostgreSQL, 12 cases)

- `session_status` enum contains `under_review` and does not contain
  `created` or `cancelled`.
- `flag_status` enum contains `overturned`.
- `review_decision` enum contains `needs_more_info`.
- `flag_immutable` trigger rejects direct `UPDATE` SQL.
- `flag_immutable` trigger rejects direct `DELETE` SQL.
- `termination_record_immutable` trigger rejects direct `UPDATE` SQL.
- `termination_record_immutable` trigger rejects direct `DELETE` SQL.
- `ck_policy_gaze_min_duration_within_window` is enforced at the
  SQL level.
- `uq_evidence_artifacts_one_per_flag` is enforced at the SQL level.
- `exam_sessions.accumulated_medium_score` round-trips through the
  real engine.
- `flags.triggered_termination` round-trips; a direct UPDATE is
  rejected by the trigger, so a true value can only be set at insert
  time.
- `flags.policy_config_id` FK is enforced at the SQL level;
  `ON DELETE RESTRICT` blocks the cascade when a termination
  record depends on the flag.
