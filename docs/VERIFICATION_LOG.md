# Verification Log

Last updated: 2026-07-20 (LTI 1.3 launch routes + service, turn N+1)

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

## Execution record (AdminUser layer)

| Check | Executed command | Result |
|---|---|---|
| Model unit tests (post-AdminUser) | `pytest tests --ignore=tests/integration` | 35 logical cases passed (28 reconciliation cases + 7 AdminUser cases) |
| Migration upgrade against PostgreSQL 15 | `alembic upgrade head` against `postgres:15-alpine` | All three migrations applied; `admin_users` table and `admin_role` enum created |
| AdminUser integration test suite | `INTEGRATION_DATABASE_URL=postgresql+psycopg://... pytest tests/integration` | 16 real-engine tests pass (13 reconciliation + 3 AdminUser) |

## Execution record (LTI 1.3 foundation, turn N)

| Check | Executed command | Result |
|---|---|---|
| Dependency installation | `pip install -e ".[dev]"` | Resolved; `httpx>=0.27,<1`, `pyjwt[crypto]>=2.8,<3`, `pytest-asyncio>=0.24,<1`, and `pytest-httpx>=0.30,<1` added |
| LTI foundation unit tests | `pytest tests --ignore=tests/integration` | **105 passed** in 2.13s (35 boundary / integrity + 70 new LTI-foundation unit cases across `test_lti_claims.py`, `test_lti_state.py`, `test_lti_roles.py`, `test_lti_session_token.py`, `test_lti_discovery.py`, `test_lti_jwks.py`) |
| LTI integration tests (off-CI) | `pytest tests/integration` without `INTEGRATION_DATABASE_URL` | 16 skipped cleanly; the LTI integration tests are scoped to turn N+1 |
| LTI module import smoke test | `.venv/Scripts/python.exe -c "from proctoring_engine.lti import claims, state, roles, session_token, discovery, jwks, config; ..."` | All 7 modules import without error |
| Session token round-trip | inline test | `decode_session_token(issue_session_token(...))` returns the original `participant_id`, `exam_session_id`, and `role`; bad signature, wrong `iss`, wrong `aud`, and missing claims are all rejected |

## Execution record (LTI 1.3 launch routes + service, turn N+1)

| Check | Executed command | Result |
|---|---|---|
| Dependency installation (LTI launch turn) | `pip install python-multipart` + `pyproject.toml` update | Resolved; `python-multipart>=0.0.9` added to dependencies |
| LTI launch service unit tests | `pytest tests/test_lti_service.py -v` | 14 passed (learner launch, instructor / admin-role upserts, retired / unknown policy, two-launch upsert semantics, `attempt_reference` is UUID4, session token round-trips, redirect URL routes by role, learner path does not create `AdminUser`, failed launch rolls back the participant) |
| LTI launch route unit tests | `pytest tests/test_lti_routes.py -v` | 17 passed (`/lti/login` happy path, missing params (400/422), discovery failure (502), target_link_uri mismatch (400); `/lti/launch` happy paths (learner + instructor), replay (state_unknown), expired (claims_invalid), signature invalid, wrong iss (issuer_invalid, *rejected before the OIDC fetch via the new `LaunchStateStore.peek` path*), wrong nonce, missing policy, wrong aud, unknown role URI) |
| LTI launch integration tests | `pytest tests/integration/test_lti_launch.py -v` | 9 cases, all gated on `INTEGRATION_DATABASE_URL`. They run against a real PostgreSQL engine; the integration-test runner provisions an ephemeral `proctoring_test` database, applies the full migration chain, and tears the schema down at session teardown. Verified: the JSONB `PolicyConfig.extra_rules` is not mutated; the JSONB `ExamSession.permitted_material_details` is the default empty dict; `accumulated_medium_score == 0`; `consent_recorded_at == started_at`; the `AdminUser` natural key matches the `Participant`'s; the upsert is transactional; the `attempt_reference` is a UUID4; the admin-role promotion is one-way |
| Combined unit test suite | `pytest tests --ignore=tests/integration` | **134 passed** in 6.6s |
| LTI launch module import smoke test | `.venv/Scripts/python.exe -c "from proctoring_engine.lti import process_launch, build_lti_router, LtiLaunchError, LtiLaunchErrorCode, LaunchResult, ..."` | All new exports import without error; the API module loads the router via a lifespan-managed factory |
| `LaunchStateStore.peek` | inline test | Returns the registered `lti_issuer` for a known state; returns `None` for an unknown or expired state; never consumes the entry |
| `LaunchStateStore.consume` returns `lti_issuer` | inline test | After `consume`, the returned tuple's second element is the `lti_issuer` the state was registered with; a follow-up `consume` raises `LaunchStateMissing` (one-shot) |

## Not yet verified

- Live deployment to a real Kubernetes cluster (the manifests exist;
  the cluster is the next environment to provision).
- Postgres write throughput under a sustained flag-fire rate (the
  workload is read-light and write-bursty; a load test is the next
  environment to provision).
- The LTI 1.3 launch routes + `process_launch` service + OIDC test
  double + PostgreSQL integration tests (turn N+1).
- The WebSocket transport and the kill-switch flow (the layer after
  the LTI routes close out).

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

### Unit (LTI 1.3 foundation, turn N — 70 cases)

`test_lti_claims.py` (14 cases): valid payload parses; missing
required claim raises; wrong LTI version raises; wrong message
type raises; deep-linking launch raises at the parse boundary;
empty roles list raises; empty `policy_config_name` raises on
`require_policy_config_name`; a launch without the custom claim
parses but `require_policy_config_name` raises; non-object
payload raises; unknown LTI-namespaced claim raises (strict
validation); combined context id is `<context.id>:<resource_link.id>`;
sub-models reject unknown fields (except `LtiCustomClaims`,
which uses `extra=allow` by design so the platform can pass
through other custom values); `require_lti_version_1_3` accepts
`1.3.0` and rejects other versions; `require_resource_link_request`
accepts resource-link and rejects other message types.

`test_lti_state.py` (11 cases): register + consume round-trips
the redirect URI; consume is one-shot (second call raises
`LaunchStateMissing`); consume of an unknown state raises
`LaunchStateMissing`; consume after the TTL raises
`LaunchStateExpired` and the entry is purged; a wrong nonce
raises `ValueError` and the entry is **not** consumed; a
re-registration overwrites the prior entry; `purge_expired` evicts
expired entries and reports the count; empty state or nonce is
rejected at register time; concurrent register + consume is
thread-safe (exactly one consumer wins); `new_state` and
`new_nonce` produce fresh tokens each call; the constructor
rejects non-positive TTLs.

`test_lti_roles.py` (6 cases): single role maps to the right
`AppRole`; multiple roles take the highest privilege (admin >
proctor > instructor > learner); `is_admin_route` returns `True`
for non-learner roles; unknown role URI raises `ValueError`;
empty role list raises `ValueError`; `AppRole` is a real enum
with the four expected members.

`test_lti_session_token.py` (8 cases): HS256 round-trip preserves
participant id, exam session id, and role; expired `exp` is
rejected; wrong `iss` is rejected; wrong `aud` is rejected; missing
required claim is rejected; signed with a different secret is
rejected; the issued token includes `iat`, `jti`, and the standard
OIDC shape; `decode_session_token` raises `SessionTokenError` for
malformed input. The constructor's `session_token_secret` length
check and the default TTL of 14 400 s are pinned by
`LtiSettings`'s own unit tests.

`test_lti_discovery.py` (10 cases): happy-path fetch returns the
parsed `OidcDiscovery`; the document is cached per-issuer; the
fetched `issuer` is validated against the requested issuer; an
HTTP error surfaces as `OidcDiscoveryError`; a non-200 response
surfaces as `OidcDiscoveryError`; a non-JSON body surfaces as
`OidcDiscoveryError`; a document missing required fields surfaces
as `OidcDiscoveryError`; an `oidc_discovery_url` override is
honored; the cache returns the same document on a second call
without an extra HTTP request; concurrent fetches share a single
in-flight request.

`test_lti_jwks.py` (13 cases): happy-path returns the JWK with
the requested `kid`; an unknown `kid` triggers a re-fetch and
finds the rotated key; a cached JWKS within its TTL is reused
(no second request); a JWKS past its TTL is re-fetched; a `kid`
genuinely absent after a fresh fetch is a hard error; a non-200
response is reported as `JwksError`; a non-JSON body is reported
as `JwksError`; a document without a `keys` array is rejected;
a key without a `kid` is silently skipped; a key with an
inconsistent shape is rejected; a network error surfaces as
`JwksError`; `invalidate` clears the cache so the next call
re-fetches; the constructor rejects non-positive TTLs.

### Unit (LTI 1.3 launch routes + service, turn N+1 — 31 cases)

`test_lti_service.py` (14 cases): a successful learner launch
creates a `Participant` and a `PENDING` `ExamSession` with the
policy bound, `consent_recorded_at = started_at = now()`, and
`accumulated_medium_score == 0`; the session row does not copy
the policy's `extra_rules` JSONB (orthogonal fields); an
instructor launch upserts an `AdminUser` with `INSTRUCTOR`
role; an admin-role launch promotes the `AdminUser` to
`ADMIN`; admin-role promotion is one-way (a later lower-
privilege launch does not demote); a launch with an unknown
policy name raises `policy_not_found`; a launch with a
retired policy (`is_active=false`) raises `policy_not_found`;
two consecutive launches from the same natural key upsert the
participant and create two `ExamSession` rows; the
`attempt_reference` is a UUID4; the session token round-trips
through `decode_session_token`; the redirect URL routes
learners to the exam client and instructors to the admin
surface; a learner launch does not create an `AdminUser`; a
failed launch rolls back the participant.

`test_lti_routes.py` (17 cases): `/lti/login` happy path
302-redirects to the platform's authorization endpoint with
the right query string; missing `iss` and missing `login_hint`
return 400/422; OIDC discovery failure returns 502; a
`target_link_uri` mismatch returns 400 `claims_invalid`;
`/lti/launch` happy paths for learner and instructor
(instructor path creates the `AdminUser` row); replay
(consumed `state` returns 400 `state_unknown`); expired
`exp` returns 400 `claims_invalid`; signature from a key
not in the JWKS returns 400 `signature_invalid`; wrong `iss`
returns 400 `issuer_invalid` (rejected before the discovery
fetch via the new `LaunchStateStore.peek` path); wrong
`nonce` returns 400 `nonce_mismatch`; missing
`custom.policy_config_name` returns 400 `policy_not_found`;
wrong `aud` returns 400 `audience_invalid`; unknown role
URI returns 400 `claims_invalid`.

### Integration (LTI 1.3 launch, turn N+1 — 9 cases)

`test_lti_launch.py` (9 cases, gated on
`INTEGRATION_DATABASE_URL`): the JSONB `PolicyConfig.extra_rules`
is not mutated by the launch; the JSONB
`ExamSession.permitted_material_details` is the default empty
dict; `accumulated_medium_score` stays at 0;
`consent_recorded_at` and `started_at` are equal at creation
time (the documented "consent is start" choice); the resolved
`policy_config_id` is bound to the right policy; the
`AdminUser`'s natural key matches the `Participant`'s; two
consecutive launches from the same natural key produce one
`Participant` row and two `ExamSession` rows; the
`attempt_reference` is a UUID4; an admin-role launch promotes
the `AdminUser` to `ADMIN` and does not duplicate the
`Participant`; the `lti_context_id` is the documented
`<context.id>:<resource_link.id>` shape.
