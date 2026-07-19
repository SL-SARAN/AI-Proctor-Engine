# Known Issues, Errors, and Deliberate Gaps

Last updated: 2026-07-19

This file records the current state of issues, errors, and deliberate
gaps in the AI Proctoring Engine. It is organized to match the
project's working environment: a developer running the project in VS
Code on Windows with a local `.venv`, optionally `docker compose up`
for Postgres + MinIO, and a real PostgreSQL 15+ for the integration
test suite.

Older entries from the original data-model-only development phase
(sandbox-only Python, missing dependencies, pytest cache warnings) are
preserved at the bottom as historical record under
"Resolved issues" so the audit trail is intact. New issues raised in
the current development environment are at the top under
"Current known issues."

## Current known issues

### 1. Starlette / httpx deprecation warning on the healthz smoke test

`proctoring_engine.api:app` boots and serves `/healthz` correctly
(200 OK, `{"status": "ok", "environment": "development"}`). The
`TestClient` call that the smoke test uses emits a one-time
`StarletteDeprecationWarning: Using httpx with starlette.testclient
is deprecated; install httpx2 instead.` This is a third-party
dependency warning, not a service failure. The recommended fix is to
widen the `httpx` dev pin in `pyproject.toml` to include
`httpx>=0.28` and evaluate whether the new `httpx2` alias is the
right replacement (Starlette's own deprecation notice points to it);
deferred to a future turn because it does not block the data-model
layer and is unrelated to the spec.

### 2. Production deployment is configuration-only, not yet exercised

The Kubernetes manifests in `k8s/` and the deployment topology in
`docs/DEPLOYMENT.md` are reviewed and consistent with the locked
spec, but they have not been applied to a live cluster yet. The
gating step is the LTI 1.3 launch layer, which is the next atomic
work; once the API has at least one real route that talks to the
LMS, the cluster can be provisioned and the manifests applied.
The `docs/DEPLOYMENT.md` §6 "Scaling story" documents the path
beyond a small cluster for when the workload grows there.

### 3. The audit-reconciliation migration's downgrade is partial by design

`migrations/versions/20260718_0002_audit_reconciliation.py` renames
the `session_status` enum value `created` → `pending` and adds three
new enum values (`session_status.under_review`,
`flag_status.overturned`, `review_decision.needs_more_info`). The
`downgrade()` removes the new columns, constraints, and triggers in
reverse order, but Postgres 15 does not support `ALTER TYPE ...
DROP VALUE`, so the new enum values cannot be removed by a
downgrade alone. The downgrade is documented as forward-only; the
right operational pattern is to add a forward-only migration that
re-introduces the old behavior if a rollback is required, not to
run `alembic downgrade base` and expect a clean revert.

## Deliberate v1 implementation gaps

These are not defects in the data-model layer. They are required
subsequent work from the supplied specification.

- No browser client, browser-event listener, local rolling evidence
  buffer, or fullscreen / tab enforcement.
- No authenticated WebSocket protocol, sparse-frame upload path,
  acknowledgement handling, or client kill-switch implementation.
- No LTI 1.3 / OIDC launch validation, JWKS / key rotation, grade
  passback, or LMS termination callback.
- No face detection / mesh, face embedding, identity matching,
  object detection, VAD, audio ingestion, gaze aggregation, or
  asynchronous worker queue.
- No S3-compatible evidence-storage adapter (Cloudflare R2 in
  production, MinIO locally), envelope encryption / KMS, lifecycle
  rule, background retention purge, or artifact upload verification.
- No `AdminUser` table or reviewer / admin API. The admin-identity
  open decision is the next atomic layer; once it lands, the admin
  surface becomes unblocked.
- No calibration data or false-positive evaluation for gaze,
  identity, face count, or monitor detection. Production thresholds
  must not be accepted as validated solely because defaults exist
  in `PolicyConfig`.

## Risks to resolve before production

1. Privacy, consent, notice text, data-minimization rules, and
   retention periods must be configured for the applicable
   jurisdiction after legal review.
2. The selected identity model determines embedding dimensionality
   and matching thresholds. Do not build approximate-nearest-neighbor
   indexes until that choice locks. The v1 schema stores the
   embedding as a JSONB float array; revisit when a "search across
   many embeddings" use case appears.
3. The current schema cannot guarantee that an exemption's
   participant matches a specific session solely with a foreign key
   because the exemption references a logical `exam_reference`.
   Enforce that match in the service layer, or introduce an explicit
   exam table if exam lifecycle management needs it.
4. The PostgreSQL triggers stop normal `UPDATE` and `DELETE` on
   `flag` and `termination_records`. Database roles with superuser /
   DDL rights remain an operational trust boundary; production audit
   hardening should also use restricted roles and immutable / off-site
   log export.
5. WebSocket affinity breaks down past ~10 API replicas on a single
   ingress. The migration path to a stateful WebSocket gateway
   (Cloudflare Durable Objects / Ably / Pusher) is documented in
   `docs/DEPLOYMENT.md` §6.1. Revisit when the workload grows there.
6. The integration test suite in `tests/integration/` is skipped
   unless `INTEGRATION_DATABASE_URL` is set. Local development
   without that env var runs only the SQLite unit suite. To run
   the integration suite locally:
   - Bring up Postgres with `docker compose up -d postgres`, or
     point `INTEGRATION_DATABASE_URL` at any PostgreSQL 15+
     instance.
   - Set `export INTEGRATION_DATABASE_URL="postgresql+psycopg://proctoring:proctoring@127.0.0.1:5432/proctoring_test"`.
   - Run `pytest tests/integration -p no:cacheprovider`.
   The fixture in `tests/integration/conftest.py` drops and
   recreates the database between session runs.

## Recent issue-resolution history (2026-07-18 → 2026-07-19)

The audit-reconciliation turn introduced 6 test defects that were
caught and fixed in the same review. The audit-trail below is
preserved so future contributors can see what shape the failure
modes took.

| Test | Defect | Fix |
|---|---|---|
| `test_flag_confidence_interval_rejects_invalid_triples` (3 of 4 cases) | The `Flag(...)` constructor call was outside the `with pytest.raises(...)` block, so the `@validates` decorator's `ValueError` propagated out of the test function before `pytest.raises` could observe it. | Moved the constructor and the commit inside the `with` block. |
| `test_policy_medium_score_threshold_rejects_negative` | The test expected `IntegrityError` (the SQL check), but the `@validates` decorator on `medium_score_termination_threshold` raises `ValueError` first. | Loosened the test to `pytest.raises((ValueError, IntegrityError))` and added a comment documenting the belt-and-suspenders design. |
| `test_exam_session_rejects_negative_accumulated_medium_score` | Same pattern as above. | Same fix. |
| `test_evidence_artifact_unique_per_flag` | SQLite reports unique-constraint failures as `UNIQUE constraint failed: evidence_artifacts.flag_id` — not by constraint name, as Postgres does. The match pattern `uq_evidence_artifacts_one_per_flag` therefore did not match. | Changed the match pattern to `evidence_artifacts.flag_id`. The integration test still matches on the constraint name against the real engine. |
| `test_flag_suppressed_by_exemption_round_trips` | The test set `flag.suppressed_by_exemption_id = exemption.id` *after* the flag was committed. The `flag_immutable` ORM listener (added in the audit reconciliation) correctly rejected the `UPDATE`. | Restructured the test to set `suppressed_by_exemption_id` at construction time, which is the only legal way to populate that field under the append-only rule. |
| Test-file docstring | Claimed "19 logical test cases." Actual count after the reconciliation: 28 (4 parametrize blocks × 4 cases = 16, plus 12 individual tests). | Updated the docstring to match. |

After the fixes: `pytest tests/ --ignore=tests/integration` →
**28 passed in 0.47s**.

## Resolved issues (historical, from the original data-model-only development phase)

These entries are kept for the audit trail. The issues they describe
are not present in the current development environment.

| Item | Original impact | Resolution |
|---|---|---|
| `python` and `py` were not on the system PATH in the original sandbox. | Normal `python` / `pytest` commands could not run directly. | **Resolved.** The developer is now executing in VS Code on Windows with `.venv/Scripts/python.exe` on the project-relative path; `.venv/Scripts/python.exe -m pytest tests/` works directly. The README's `py -3.11 -m venv .venv` instructions remain the recommended setup for a fresh checkout. |
| The original sandbox Python lacked FastAPI and SQLAlchemy. | Tests and runtime imports could not execute initially. | **Resolved.** `pip install -e ".[dev]"` is the one-shot install. Currently installed: FastAPI 0.139.2, SQLAlchemy 2.0.51, Alembic 1.18.5, psycopg 3.3.4. |
| No PostgreSQL server was available in the original sandbox. | The `termination_record_immutable` trigger and the enum types were not integration-tested. | **Resolved.** The integration test suite in `tests/integration/` runs against a real PostgreSQL 15+ in CI (`postgres:15-alpine` service container) and locally via `docker compose up -d postgres`. The integration suite is the source of truth for the audit-trail guarantee. |
| Pytest could not write its cache under the original sandbox. | One `PytestCacheWarning` per run. | **Resolved locally by convention.** Use `pytest -p no:cacheprovider` (or set `PYTEST_DISABLE_PLUGIN_AUTOLOAD=cacheprovider` in `.env`). Not a code defect. |
| Starlette / httpx deprecation warning on the healthz smoke test. | One warning per smoke test. | **Still open** — see "Current known issues" §1. |
