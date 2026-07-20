# Context

This file is the **cross-session mental model** for the AI Proctoring
Engine. It exists so a new Claude session, or a Claude session
returning after a long gap, can pick up exactly where the last one
left off without re-deriving the project from scratch and without
making claims that contradict the locked spec, the design docs, or
the code that is already on disk.

Read these four files together at session start, in this order:

1. `SYSTEM_STATE.md` — what is built, what is not, what is verified,
   what is open.
2. `SKILLS_ALIGNMENT.md` — the role contract, the guardrails, the
   per-turn protocol.
3. `ARCHITECTURE.md` — the project flow, layer-by-layer, the
   cross-cutting invariants.
4. `CLAUDE.md` (this directory) — the short navigation aid for new
   sessions.

Then read the original spec (`docs/proctoring-engine-v1-spec.md`) and
the relevant design doc before touching the corresponding layer.

---

## 1. What this project is

A **low-latency AI Proctoring Engine** that:

- Integrates with an LMS via **LTI 1.3** to establish a proctored
  exam session.
- Captures webcam and microphone in the browser, runs lightweight
  checks client-side, sends sparse frames and audio chunks to a
  server for heavy inference.
- Runs six modalities: face presence / count, identity match vs.
  enrollment, head pose / gaze, object detection (denylist), audio
  voice-activity detection, browser events.
- Fuses those signals into `Flag`s and decides whether to
  **auto-terminate** the session under a configurable policy.
- Persists a defensible audit trail in Postgres (immutable `Flag`
  and `TerminationRecord` rows) plus Cloudflare R2 (S3-compatible
  object storage, checksummed evidence clips with
  `retention_expires_at`).
- Enforces a rolling-buffer + context evidence retention model —
  normal operation transmits nothing extra; flagged moments trigger
  a flush of the local client buffer to the server.
- Provides a reviewer / admin surface for human override of any flag
  (via `ProctorReview` — never by editing the flag itself).

## 2. What this project is not (yet)

- It is not yet a working product end-to-end. Only the **data-model
  layer** is realized, with the audit reconciliation and a real-engine
  CI gate in place.
- It has no browser client, no WebSocket transport, no LTI launch, no
  inference workers, no admin UI, no production deployment.
- The 28 unit tests (SQLite) and 13 integration tests (PostgreSQL)
  that pass today cover the data-model layer. They do not exercise
  LTI, WebSocket, inference, fusion, or storage.

## 3. Locked decisions (do not re-derive or re-litigate)

These are decisions the user has explicitly confirmed across prior
sessions. Each is anchored to the file that records it. If you need
to change one, get user sign-off and update both the spec and the
relevant design doc in the same turn.

| Decision | Source |
|---|---|
| Backend: Python 3.11+ / FastAPI, async-native | `docs/proctoring-engine-v1-spec.md` §1 |
| Hybrid inference: lightweight client-side, heavy server-side | `docs/proctoring-engine-v1-spec.md` §1, §2 |
| LMS integration: LTI 1.3 (OAuth2 / JWT) | `docs/proctoring-engine-v1-spec.md` §1, `docs/02` §1 |
| Transport: WebSocket with tiered sampling (2–3s heavy frames) | `docs/proctoring-engine-v1-spec.md` §1, `docs/02` §2–§4 |
| Persistence: Postgres (audit) + S3-compatible (evidence) | `docs/proctoring-engine-v1-spec.md` §1 |
| Scale: thousands of concurrent users, scaling larger with documented k8s sizing | `docs/proctoring-engine-v1-spec.md` §1, `docs/DEPLOYMENT.md` §3 |
| Deployment: **Kubernetes** for production, Docker Compose for local dev | `docs/DEPLOYMENT.md` §1, §2 |
| State: **Managed Postgres** (RDS / Cloud SQL / Neon) | `docs/DEPLOYMENT.md` §1, §4 |
| Object storage: **Cloudflare R2** (S3-compatible, zero egress) | `docs/DEPLOYMENT.md` §1, §4 |
| CI: **GitHub Actions** with cloud runners; integration job uses `postgres:15-alpine` service container | `docs/DEPLOYMENT.md` §5, `.github/workflows/ci.yml` |
| Termination: auto-terminate on zero-tolerance violation, severity threshold in `PolicyConfig` | `docs/proctoring-engine-v1-spec.md` §1, §3 |
| Gaze-away: frequency-based escalation, default `gaze_min_duration_ms=800`, `gaze_window_seconds=300`, `gaze_warning_limit=3`, `gaze_termination_limit=8` | `docs/proctoring-engine-v1-spec.md` §3.1 |
| Object detection: denylist (`cell phone`, `laptop`, `tv`, conditional `book`). Earbuds / smartwatches **deferred from v1**. | `docs/proctoring-engine-v1-spec.md` §3.2 |
| Accommodation exemptions: admin pre-approval, not self-declared | `docs/proctoring-engine-v1-spec.md` §3.2 |
| Evidence retention: rolling buffer + context, not full-session recording | `docs/proctoring-engine-v1-spec.md` §4 |
| Jurisdiction-specific retention: **not** assumed; `consent_recorded_at` and `retention_expires_at` are generic fields | `docs/proctoring-engine-v1-spec.md` §1 |
| `PolicyConfig` is a versioned snapshot; `ExamSession.policy_config_id` references a specific version | `docs/01-data-models-design.md`, `PolicyConfig` notes |
| `Flag` and `TerminationRecord` rows are append-only (ORM + DB trigger) | `docs/01-data-models-design.md`, `docs/06` §4 |
| `ProctorReview` is the correct mechanism for correcting a flag — sits alongside, never edits | `docs/01-data-models-design.md`, `docs/06` §4 |
| One `EvidenceArtifact` per `Flag` in v1; the `uq_evidence_artifacts_one_per_flag` unique constraint enforces this | `docs/01` EvidenceArtifact, `migrations/.../20260718_0002_audit_reconciliation.py` |
| The `flag_immutable` trigger is the database-level mirror of the ORM listener | `migrations/.../20260718_0002_audit_reconciliation.py`, `src/proctoring_engine/models.py` |
| **LTI 1.3 session token shape: signed JWT, server-side secret (HS256)** | Turn N (`SYSTEM_STATE.md` §2) |
| **LTI 1.3 session token TTL: 14 400 s (4 h)** | Turn N (`SYSTEM_STATE.md` §2) |
| **LTI 1.3 policy lookup: custom LTI claim `policy_config_name`** | Turn N (`SYSTEM_STATE.md` §2) |
| **LTI 1.3 OIDC test double: `pytest-httpx` mocked transport** | Turn N (`SYSTEM_STATE.md` §2) |
| **LTI 1.3 in-memory launch-state store: process-local, single replica acceptable for v1** | Turn N (`src/proctoring_engine/lti/state.py`, `docs/02` §1) |

## 4. Open decisions (do not silently resolve)

These are explicitly called out in the design docs as **not yet
confirmed** by the user. A turn that resolves any of them silently
is non-conformant under `SKILLS_ALIGNMENT.md` §7.

1. **Admin / reviewer identity model** — **Resolved.** The `AdminUser`
   table was added in migration `20260719_0003`. The three referencing
   tables (`PolicyConfig.created_by_id`,
   `AccommodationExemption.approved_by_admin_id`,
   `ProctorReview.reviewer_admin_id`) now carry FK columns alongside
   the original string fields for backward compatibility.
2. **Accumulated-score termination path** — `MEDIUM` flags adding
   weighted increments to `ExamSession.accumulated_medium_score`,
   with a `medium_score_termination_threshold` that triggers a
   `CRITICAL` flag. The schema is now ready (both fields exist);
   the fusion-engine implementation and the user-confirmation that
   the path is wanted at all are still open.
3. **Embedding storage mechanism** — `pgvector` extension vs.
   application-computed float array. Settled for v1 as JSONB float
   array. Revisitable if a "search across many embeddings" use case
   appears.

## 5. File map (what to read for what)

| If you need to… | Read |
|---|---|
| Understand what is built today | `SYSTEM_STATE.md` |
| Understand the role contract / guardrails | `SKILLS_ALIGNMENT.md` |
| Understand the project flow / layer contracts | `ARCHITECTURE.md` |
| Understand the deployment topology | `docs/DEPLOYMENT.md` |
| Read the locked v1 spec | `docs/proctoring-engine-v1-spec.md` |
| Read the per-layer design | `docs/00-index-and-architecture-flow.md` (index) and the relevant `docs/0X-…-design.md` |
| Read the actual ORM | `src/proctoring_engine/models.py` |
| Read the initial migration (DDL + immutability trigger) | `migrations/versions/20260717_0001_initial_proctoring_schema.py` |
| Read the audit reconciliation migration | `migrations/versions/20260718_0002_audit_reconciliation.py` |
| Read the boundary / integrity unit tests | `tests/test_models.py` |
| Read the integration test suite | `tests/integration/test_postgres_immutability.py` |
| Read the CI workflow | `.github/workflows/ci.yml` |
| Read the verification record | `docs/VERIFICATION_LOG.md` |
| Read what is intentionally deferred | `docs/KNOWN_ISSUES.md` |
| Read what is done vs. not done | `docs/COMPLETION_STATUS.md` |
| Read the handoff to the next implementer | `docs/CLAUDE_HANDOFF.md` |
| Read the LTI 1.3 foundation (turn N) | `src/proctoring_engine/lti/` (`config.py`, `claims.py`, `roles.py`, `state.py`, `session_token.py`, `discovery.py`, `jwks.py`) |
| Read the LTI 1.3 foundation tests (turn N) | `tests/test_lti_*.py` (71 unit cases) |
| Run the project locally | `README.md`, `pyproject.toml`, `.env.example`, `alembic.ini`, `Dockerfile`, `docker-compose.yml` |
| Deploy to production | `docs/DEPLOYMENT.md`, `k8s/` |

## 6. How to operate across sessions

At the **start** of every session:

1. Read `SYSTEM_STATE.md` §12 (the System State Status checklist).
   It tells you what is built and what the next atomic layer is.
2. Read `SKILLS_ALIGNMENT.md` §5 (incremental lifecycle) and §9
   (how this turn operates).
3. Re-read the relevant `docs/0X-…-design.md` for the layer you are
   about to touch. Do not assume it is unchanged from memory.
4. If a number, API, or library function is in question, verify it
   against the actual source (`src/`, `pyproject.toml`) or the design
   doc — not from training data.

During a turn, run the **ReAct loop** from `SKILLS_ALIGNMENT.md` §2
(THOUGHT → ACTION → OBSERVATION) before any code, architecture, or
configuration is emitted.

At the **end** of every turn:

1. Update `SYSTEM_STATE.md` §12 to mark the atomic layer as completed
   and to identify the next one.
2. If architecture changed, update `ARCHITECTURE.md` in the same turn
   (see `ARCHITECTURE.md` §8).
3. If a new open decision was surfaced, surface it in the turn's
   output — do not silently resolve it.
4. If a previously open decision was resolved, update this file's §4
   to remove it, update the design doc, and update `ARCHITECTURE.md`.

## 7. Cross-cutting rules to never violate

These are the things that, if violated, would corrupt the audit
trail or the system semantics in a way that cannot be fixed by a
subsequent turn:

- Never write a `Flag` or `TerminationRecord` row that is later
  UPDATEd or DELETEd by application code. Use `ProctorReview` for
  corrections.
- Never persist a `TelemetryEvent` (or any other per-session row tied
  to evidence) before `ExamSession.consent_recorded_at` is set.
- Never silently drop a flagged object-detection event when an
  exemption matches. Downgrade severity, set
  `Flag.suppressed_by_exemption_id`, and keep the underlying
  `TelemetryEvent` in the audit log.
- Never transmit the client rolling buffer during normal operation.
  Flush only on a confirmed flag.
- Never reuse an LTI student / instructor token to call the internal
  `/sessions/{id}/terminate` route. That route is internal
  service-to-service only and uses `INTERNAL_TERMINATE_TOKEN`.
- Never hardcode a threshold into a conditional. The threshold
  belongs in `PolicyConfig`.
- Never invent a number, API, or library. Cite the source, or halt
  and ask.
- Never emit `TODO`, `…`, `pass  # implement`, or a stub function.
  One atomic layer per turn, complete end-to-end.
- Never skip the integration test when the schema changes. The
  immutability triggers are the audit story; they must be verified
  against the real engine in CI.

## 8. The boundary / integrity tests passing today

Per `docs/VERIFICATION_LOG.md` and the test files:

**Unit (SQLite, 28 logical cases in `tests/test_models.py`):** 4
parametrized cases for telemetry-confidence boundaries, 4
parametrized cases for telemetry-confidence rejection, 4 parametrized
cases for flag-confidence-interval acceptance, 4 parametrized cases
for flag-confidence-interval rejection, plus 12 individual tests for:
session timestamp ordering, session missing-FK, session default
status, accumulated-medium-score default and rejection, policy
gaze-warning-not-greater-than-termination, policy
gaze-min-within-window, policy medium-score-threshold non-negative,
flag-telemetry dedupe, embedding-model-version validation,
one-artifact-per-flag, termination-record ORM mutation rejection,
flag ORM update rejection, flag ORM delete rejection,
`triggered_termination` default, and `suppressed_by_exemption_id`
round-trip.

**Integration (PostgreSQL, 13 cases in
`tests/integration/test_postgres_immutability.py`):** enum-value
verification (3), `flag_immutable` trigger update/delete rejection
(2), `termination_record_immutable` trigger update/delete rejection
(2), `ck_policy_gaze_min_duration_within_window` enforcement, the
`uq_evidence_artifacts_one_per_flag` constraint, accumulated-score
round-trip, triggered-termination round-trip, FK enforcement, and
the `ON DELETE RESTRICT` behavior on `flags.policy_config_id`.

## 9. The shape of the next session

The **LTI 1.3 foundation** (turn N) is now in: `LtiSettings`, claim
parsing, role mapping, the in-memory `LaunchStateStore`, the HS256
session token, OIDC discovery, and JWKS fetchers — 71 unit tests
passing. Turn N+1 closes out the ingestion layer: the launch
routes, the `process_launch` service, an OIDC test double, and
the PostgreSQL integration tests. This is described in detail in
`docs/02-ingestion-layer-design.md` §1 and
`docs/08-test-strategy-design.md` §"Ingestion layer".

Specifically, turn N+1 delivers:

- `GET /lti/login` and `POST /lti/launch` FastAPI routes that wire
  the foundation modules together.
- The OIDC test double (`tests/integration/oidc_test_double.py`):
  a small helper that generates an RSA keypair, exposes a
  discovery document + JWKS endpoint, and signs launch JWTs against
  the generated key. Wraps `pytest-httpx` so the unit and
  integration tests can run without a real socket.
- The `process_launch` service: upsert `Participant` on
  `(lti_issuer, lms_user_reference)`, resolve the active
  `PolicyConfig` by `custom.policy_config_name`, create the
  `ExamSession` with `status=PENDING` and `policy_config_id` set,
  bind `consent_recorded_at = started_at = now()`, and issue the
  HS256 session token. Instructor / admin-role launches also
  upsert an `AdminUser` row so the admin surfaces (a later layer)
  can attribute `PolicyConfig.created_by_id` correctly.
- Role-based branching: learner launches redirect to the exam
  client; instructor / admin / proctor launches redirect to the
  admin surface.
- Integration tests against a real PostgreSQL engine that verify
  the launch's persistence invariants (the JSONB `extra_rules`
  column is not mutated; `accumulated_medium_score` default is
  preserved; the launch is transactional; an `AdminUser` row is
  upserted for the admin path).

Tests, per the boundary cases the spec calls out in
`docs/02-ingestion-layer-design.md` §1 and
`docs/08-test-strategy-design.md` §"Ingestion layer":

- Unit: `process_launch` boundary cases (missing policy name, no
  active policy, two launches from the same
  `(lti_issuer, lms_user_reference)` upsert correctly), and
  endpoint tests (`/lti/login` happy-path redirect, `/lti/launch`
  happy-path token issuance, replay of `state` returns 400,
  expired `exp` returns 400, signature from a key not in the JWKS
  returns 400, wrong `iss` returns 400, wrong `nonce` returns 400,
  missing `custom.policy_config_name` returns 400, retired policy
  returns 400, instructor-role launch upserts `AdminUser`).
- Integration: same suite as the unit tests but against the real
  PostgreSQL engine, verifying that the launch does not mutate
  the policy snapshot and that the upsert is correct across
  schema columns.
- Endpoint-to-end: a successful launch returns a 302 to the
  exam client and creates the right rows; a malformed launch
  returns 400 and creates nothing; a replayed `state` returns
  400 (not 200) — fail-closed, not fail-open.

Layer depends on:

- `AdminUser` table (done) — for the instructor-role path.
- `pyjwt[crypto]`, `httpx`, `pytest-asyncio`, `pytest-httpx` —
  added in turn N's `pyproject.toml` updates.
- The OIDC test double is reused by both unit and integration
  tests so the launch path is exercised end-to-end without a
  real LMS.
