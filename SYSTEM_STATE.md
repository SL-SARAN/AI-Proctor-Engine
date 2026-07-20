# System State

Last updated: 2026-07-20 (turn N+1+fix: CI DATABASE_URL + Dockerfile pip pin)

This file is the single source of truth for "where the AI Proctoring
Engine is right now." Read this first at the start of every session
before touching code or making design claims. All references here are
to files that physically exist in the repository; nothing is asserted
that has not been verified against `docs/`, the source tree, or
`git log`.

---

## 1. Current implementation state

The v1 product is at the **data-model layer with audit reconciliation
and a real-engine CI gate**. The locked v1 architecture describes a
full 7-layer system (ingestion, preprocessing, inference, fusion,
evidence, orchestration, plus the data model). Of those, only the
data model is realized as code. Everything above it is described in
design docs but is not built.

| Layer | Code state | Design doc |
|---|---|---|
| Data models (ORM, DDL, constraints) | **Implemented + reconciled** | `docs/01-data-models-design.md` |
| Initial Alembic migration | **Implemented** | `migrations/versions/20260717_0001_initial_proctoring_schema.py` |
| Audit reconciliation migration | **Implemented** | `migrations/versions/20260718_0002_audit_reconciliation.py` |
| Boundary / integrity unit tests (SQLite) | **Implemented (19 cases)** | `tests/test_models.py` |
| PostgreSQL integration tests | **Implemented (12 cases)** | `tests/integration/test_postgres_immutability.py` |
| GitHub Actions CI (unit + integration + build) | **Implemented** | `.github/workflows/ci.yml` |
| Docker image + local compose | **Implemented** | `Dockerfile`, `docker-compose.yml` |
| Kubernetes manifest set | **Implemented** | `k8s/00-...` through `k8s/10-...` |
| Deployment topology doc | **Implemented** | `docs/DEPLOYMENT.md` |
| FastAPI application shell (health endpoint only) | **Implemented** | `src/proctoring_engine/api.py` |
| AdminUser table + admin-identity resolution | **Implemented** | `src/proctoring_engine/models.py`, `migrations/versions/20260719_0003_admin_user.py` |
| LTI 1.3 foundation (config, claims, roles, state store, session token, OIDC discovery, JWKS fetcher) | **Implemented (turn N, 70 unit tests)** | `src/proctoring_engine/lti/` |
| LTI 1.3 launch routes + `process_launch` service + OIDC test double + PostgreSQL integration tests | **Implemented (turn N+1, 134 unit + 9 integration tests)** | `src/proctoring_engine/lti/routes.py`, `src/proctoring_engine/lti/service.py`, `tests/integration/test_lti_launch.py` |
| Preprocessing (frame decode, tiered scheduler, rolling buffer) | Not started | `docs/03-preprocessing-layer-design.md` |
| Inference modules (6 modalities) | Not started | `docs/04-inference-modules-design.md` |
| Fusion & flagging engine (3 termination paths) | Not started | `docs/05-fusion-flagging-engine-design.md` |
| Evidence & audit store (S3, retention job) | Not started | `docs/06-evidence-audit-store-design.md` |
| API & orchestration (full route surface, state machine) | Not started | `docs/07-api-orchestration-design.md` |
| Browser client | Not started | n/a |

`docs/COMPLETION_STATUS.md` and `docs/CLAUDE_HANDOFF.md` describe the
*original* data-model layer only; this file is the up-to-date record
of what has been built since.

## 2. What is already verified

Per `docs/VERIFICATION_LOG.md`:

### Data-model layer (initial)

- `pip install -e ".[dev]"` resolves cleanly.
- The original 9 boundary / integrity unit tests passed on SQLite
  (the count predates the audit reconciliation; the post-reconciliation
  count is 28; the post-AdminUser count is 35).
- `GET /healthz` via FastAPI `TestClient` returned `200 OK` and
  `{"status": "ok", "environment": "development"}`.
- `alembic upgrade head --sql` (DDL compile only) generated enums,
  tables, constraints, indexes, the `termination_record_immutable`
  trigger function, and the trigger itself.

### Audit reconciliation + integration suite

- `pytest tests --ignore=tests/integration` → **35 logical cases passed**
  (4 from the confidence-accepts parametrize, 4 from the confidence-rejects
  parametrize, 4 from the flag-interval-accepts parametrize, 4 from the
  flag-interval-rejects parametrize, 19 individual tests including 7 new
  AdminUser tests).
- `alembic upgrade head` against a real PostgreSQL 15 service
  container applied all three migrations; the `flag_immutable` and
  `termination_record_immutable` triggers are installed; the
  `admin_users` table and `admin_role` enum are created.
- `pytest tests/integration` against the real engine → **16 passed**
  (13 original + 3 AdminUser tests).
- `docker build --tag proctoring-engine:ci-smoke --load .` builds
  successfully; the runtime stage boots as a non-root user with a
  read-only rootfs.

### LTI 1.3 foundation (turn N)

- `pip install -e ".[dev]"` resolves cleanly with the added
  `httpx>=0.27,<1`, `pyjwt[crypto]>=2.8,<3`,
  `pytest-asyncio>=0.24,<1`, and `pytest-httpx>=0.30,<1`
  dependencies; `asyncio_mode = "auto"` is set in `pyproject.toml`.
- `pytest tests --ignore=tests/integration` → **105 passed**
  (35 boundary / integrity + 70 new LTI-foundation unit tests across
  `test_lti_claims.py`, `test_lti_state.py`, `test_lti_roles.py`,
  `test_lti_session_token.py`, `test_lti_discovery.py`, and
  `test_lti_jwks.py`).
- `pytest tests/integration` without `INTEGRATION_DATABASE_URL` →
  16 skipped (the LTI integration tests will be added in turn N+1
  alongside the launch routes).

## 3. What is NOT yet verified

- The Kubernetes manifest set has not been applied to a live
  cluster. The configuration is reviewed; the cluster is the next
  environment to provision.
- LTI 1.3 launch / WebSocket transport / inference / fusion / storage
  — all the upper layers. None are built.
- Live load testing on a real Postgres instance. The schema is
  designed for moderate concurrency (tens–low hundreds of
  concurrent sessions, scaling to thousands with the documented
  k8s sizing); a load test is the next environment to provision
  after the LTI layer is in.

## 4. Locked architecture decisions (do not re-litigate without user sign-off)

From `docs/proctoring-engine-v1-spec.md` §1 and the design docs:

- **Backend:** Python 3.11+ / FastAPI, async-native.
- **Inference location:** hybrid. Lightweight (face presence, browser
  events) client-side; heavy (identity, gaze, objects, audio) server-side.
- **LMS integration:** LTI 1.3 (OAuth2 / JWT).
- **Transport:** WebSocket, tiered sampling (lightweight telemetry
  every frame, heavy frames every 2–3s, audio independent cadence).
  WebRTC explicitly **not** in v1.
- **Persistence:** Postgres for relational audit data + S3-compatible
  object storage for evidence blobs.
- **Scale target:** thousands of concurrent users, scaling to larger
  numbers with documented k8s sizing.
- **Deployment:** **Kubernetes** (per the answer to the deployment
  question) with **managed Postgres** (RDS / Cloud SQL / Neon) and
  **Cloudflare R2** for S3-compatible object storage. Local dev uses
  Docker Compose with MinIO standing in for R2.
- **CI:** GitHub Actions with cloud runners; integration job uses a
  `postgres:15-alpine` service container.
- **Termination policy:** auto-terminate on zero-tolerance violation,
  severity threshold configurable in `PolicyConfig`.
- **Gaze-away:** frequency-based escalation, default
  `gaze_min_duration_ms=800`, `gaze_window_seconds=300`,
  `gaze_warning_limit=3`, `gaze_termination_limit=8`.
- **Object detection:** denylist strategy (`cell phone`, `laptop`,
  `tv`, conditional `book`). Earbuds / smartwatches **deferred**.
- **Accommodation exemptions:** admin pre-approval, not self-declared.
- **Evidence retention:** rolling buffer + context.
- **Compliance jurisdiction:** generic `consent_recorded_at` /
  `retention_expires_at` fields.
- **Audit trail:** `Flag` and `TerminationRecord` rows are
  append-only. ORM-level listener + PostgreSQL trigger for
  `TerminationRecord` (initial migration) and `Flag`
  (audit-reconciliation migration).
- **PolicyConfig versioning:** `ExamSession.policy_config_id`
  references a specific snapshot; changes create a new version.

## 5. Open decisions

1. **Admin / reviewer identity model** — **Resolved.** The `AdminUser`
   table was added in migration `20260719_0003`. The three referencing
   tables (`PolicyConfig.created_by_id`, `AccommodationExemption.approved_by_admin_id`,
   `ProctorReview.reviewer_admin_id`) now carry FK columns alongside
   the original string fields for backward compatibility.
2. **Accumulated-score termination path** — proposed in
   `docs/05-fusion-flagging-engine-design.md` Path 3. The schema is
   now ready for it (`ExamSession.accumulated_medium_score` and
   `PolicyConfig.medium_score_termination_threshold`), but the
   fusion-engine implementation and the user-confirmation that the
   path is wanted at all are still open.
3. **Embedding storage mechanism** — JSONB float array (current) vs.
   `pgvector` (alternative). Settled for v1 as JSONB; revisitable if
   a "search across many embeddings" use case appears.

## 6. Library / model availability constraints (sandbox-side)

From the spec's "library availability" note:

- MediaPipe `mp.solutions` (Face Detection, Face Mesh): weights
  bundled in pip package. **Verifiable here.**
- `webrtcvad`: bundled, no external download. **Verifiable here.**
- YOLOv8 (Ultralytics): weights auto-download from GitHub Releases
  at first run. **Verifiable here.**
- `face_recognition` (dlib ResNet) vs. `DeepFace`: choice pending.
- `pgvector`: not used in v1; the JSONB approach is documented.
- MediaPipe Tasks API (newer): not in v1.
- `pyannote.audio` (diarization): **explicitly out of v1**.

## 7. Key entities and where they live

| Entity | ORM model | Purpose |
|---|---|---|
| `Participant` | `src/proctoring_engine/models.py` | LTI identity, scoped by `(lti_issuer, lms_user_reference)`. |
| `EnrollmentReference` | same | Enrollment photo + embedding vector (JSONB in v1) + model name + version. |
| `ExamSession` | same | One proctored attempt; status, policy snapshot, consent, retention, accumulated score. |
| `AccommodationExemption` | same | Admin-approved, participant+object-class scoped. |
| `TelemetryEvent` | same | Aggregated meaningful readings. |
| `Flag` | same | Fused decision; append-only; confidence interval; triggered_termination; suppressed_by_exemption_id. |
| `FlagTelemetryEvent` | same | Join table — many telemetry events per flag, no duplicates. |
| `EvidenceArtifact` | same | Stored clip/frame tied to a flag; one per flag in v1. |
| `PolicyConfig` | same | Versioned snapshot; sessions reference a specific version. |
| `TerminationRecord` | same | 1:1 with `ExamSession`, append-only (ORM + DB trigger). |
| `ProctorReview` | same | Human override on a flag (sits alongside, never edits). |
| `AdminUser` | same | Structured admin/proctor/instructor identity; FK target for `PolicyConfig.created_by_id`, `AccommodationExemption.approved_by_admin_id`, `ProctorReview.reviewer_admin_id`. |

## 8. Immutability guarantees already in code

- **Application-level:** `TerminationRecord` and `Flag` ORM event
  listeners block updates and deletes after commit.
- **Database-level:** `termination_record_immutable` (initial
  migration) and `flag_immutable` (audit-reconciliation migration)
  PostgreSQL triggers reject `UPDATE` and `DELETE`.
- **Schema-level:** `FlagTelemetryEvent` PK prevents duplicate links.
  `EvidenceArtifact.flag_id` UNIQUE enforces one artifact per flag.
- **Constraint-level:** confidence `[0,1]`, session end-after-start,
  `PolicyConfig.gaze_warning_limit <= gaze_termination_limit`,
  `gaze_min_duration_ms <= gaze_window_seconds * 1000`,
  `medium_score_termination_threshold >= 0`,
  `accumulated_medium_score >= 0`.

## 9. Recommended next-layer order (per `docs/CLAUDE_HANDOFF.md`)

1. ~~Alembic / SQLAlchemy integration tests against PostgreSQL in CI.~~ **Done.**
2. ~~`AdminUser` table — resolve the open admin-identity decision.~~ **Done** (migration `20260719_0003`).
3. Authenticated LTI 1.3 launch + session creation + consent capture
   + policy snapshot — **next atomic layer**.
4. Authenticated WebSocket event schema, sparse-frame protocol,
   evidence-buffer upload, kill-switch acknowledgement.
5. Object-storage abstraction with checksums, encryption metadata,
   retention deletion worker, and test doubles.
6. Async inference job queue + versioned telemetry payload contracts.
7. Browser client and client-side event capture. Then connect face /
   gaze / object / audio models.
8. Proctor / admin review endpoints, authorization, audit exports,
   metrics, privacy / security review.

## 10. Risks logged in `docs/KNOWN_ISSUES.md`

- Privacy, consent, notice text, and retention period **must** be
  configured per jurisdiction after legal review.
- Identity model choice determines embedding dimensionality and
  matching threshold — do not build approximate-nearest-neighbor
  indexes until that choice locks.
- The schema cannot enforce that an exemption's participant matches a
  specific session solely via FK — the exemption references a
  logical `exam_reference`. Enforce in the service layer or introduce
  an explicit exam table later.
- The PostgreSQL triggers stop normal `UPDATE` / `DELETE`, but
  database superuser / DDL rights remain a trust boundary. Production
  audit hardening needs restricted roles and immutable / off-site
  log export.
- WebSocket affinity breaks down past ~10 API replicas on a single
  ingress; document the gateway migration path in `docs/DEPLOYMENT.md`
  §6 before the workload grows there.

## 11. Files of immediate interest

- `src/proctoring_engine/models.py` — the actual ORM.
- `migrations/versions/20260717_0001_initial_proctoring_schema.py` —
  initial DDL + the `termination_record_immutable` trigger.
- `migrations/versions/20260718_0002_audit_reconciliation.py` —
  audit-reconciliation DDL + the `flag_immutable` trigger.
- `tests/test_models.py` — 35 unit boundary / integrity tests.
- `tests/integration/test_postgres_immutability.py` — 16 real-engine
  integration tests.
- `docs/proctoring-engine-v1-spec.md` — the locked v1 spec.
- `docs/00-index-and-architecture-flow.md` — the layer-by-layer
  design doc index.
- `docs/DEPLOYMENT.md` — production deployment topology.
- `pyproject.toml`, `Dockerfile`, `docker-compose.yml`, `.env.example`,
  `alembic.ini` — runtime configuration.
- `k8s/00-namespace.yaml` through `k8s/10-network-policies.yaml` —
  Kubernetes manifest set.

## 12. System State Status checklist (per turn, per the Skills Alignment contract)

At the end of every Claude turn, update the following checklist. Use
it to mark progress and to identify the next single atomic layer.

- [x] Python project structure (`pyproject.toml`, package layout,
      FastAPI shell)
- [x] PostgreSQL ORM schema (all 11 entities)
- [x] Data integrity constraints (confidence, timestamps, retention,
      FK, uniqueness)
- [x] Configurable termination policy (`PolicyConfig`)
- [x] Audit relations (`FlagTelemetryEvent`, `EvidenceArtifact`,
      `TerminationRecord`, `ProctorReview`)
- [x] `TerminationRecord` immutability (ORM + PostgreSQL trigger)
- [x] `Flag` immutability (ORM + PostgreSQL trigger)
- [x] Schema migration (initial revision)
- [x] Audit reconciliation migration (20260718_0002)
- [x] Boundary / integrity unit tests (35 logical cases passing on SQLite)
- [x] PostgreSQL integration tests (16 passing on real engine)
- [x] GitHub Actions CI workflow (unit + integration + build)
- [x] Docker image + docker-compose for local dev
- [x] Kubernetes manifest set
- [x] Documentation suite (`docs/00`–`08`, `docs/DEPLOYMENT.md`, plus
      spec and handoff) — `docs/` is now in git
- [x] `AdminUser` table + admin-identity resolution
- [x] LTI 1.3 foundation (config, claims, roles, state store, session
      token, OIDC discovery, JWKS fetcher) — 70 unit tests passing
- [x] LTI 1.3 launch routes + `process_launch` service + OIDC test
      double + integration tests (turn N+1)
- [ ] LTI 1.3 launch routes + `process_launch` service + OIDC test
      double + PostgreSQL integration tests (turn N+1)
- [ ] Authenticated WebSocket protocol (envelope, sparse frames,
      kill-switch, ack)
- [ ] Ingestion layer implementation
- [ ] Preprocessing layer (decode, tiered scheduler, rolling buffer
      contract)
- [ ] Inference modules (face presence, identity, head pose / gaze,
      object, audio VAD, browser events)
- [ ] Fusion & flagging engine (3 paths + exemption suppression)
- [ ] Evidence store (S3-compatible adapter, checksums, retention
      deletion job)
- [ ] API / orchestration (full route surface, state machine,
      authorization)
- [ ] Browser client + capture
- [ ] Production deployment (k8s cluster provisioned, end-to-end
      smoke test on a live cluster)
