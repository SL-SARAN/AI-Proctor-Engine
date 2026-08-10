# System State

Last updated: 2026-08-01 (turn N+8: browser client + capture skeleton)

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
| AdminUser table + admin-identity resolution | **Implemented (part of initial schema)** | `src/proctoring_engine/models.py` — `AdminUser` is created by the initial migration's `Base.metadata.create_all` |
| LTI 1.3 foundation (config, claims, roles, state store, session token, OIDC discovery, JWKS fetcher) | **Implemented (turn N, 70 unit tests)** | `src/proctoring_engine/lti/` |
| LTI 1.3 launch routes + `process_launch` service + OIDC test double + PostgreSQL integration tests | **Implemented (turn N+1, 134 unit + 9 integration tests)** | `src/proctoring_engine/lti/routes.py`, `src/proctoring_engine/lti/service.py`, `tests/integration/test_lti_launch.py` |
| Authenticated WebSocket protocol (envelope, sparse frames, kill-switch, ack) | **Implemented (turn N+2, 88 unit tests)** | `src/proctoring_engine/websocket/` |
| Preprocessing (frame decode, audio pipeline, tiered scheduler, rolling buffer) | **Implemented (turn N+3, 127 unit tests)** | `src/proctoring_engine/preprocessing/`, `docs/03-preprocessing-layer-design.md` |
| Inference modules (6 modalities) | **Implemented (turn N+4, 79 unit tests)** | `src/proctoring_engine/inference/`, `docs/04-inference-modules-design.md` |
| Fusion & flagging engine (3 termination paths + exemption suppression) | **Implemented (turn N+5, 68 unit tests)** | `src/proctoring_engine/fusion/`, `docs/05-fusion-flagging-engine-design.md` |
| Evidence & audit store (S3, retention job) | **Implemented (turn N+6, 58 unit tests)** | `src/proctoring_engine/evidence/`, `docs/06-evidence-audit-store-design.md` |
| API & orchestration (full route surface, state machine) | **Implemented (turn N+7, 73 unit tests)** | `src/proctoring_engine/orchestration/`, `docs/07-api-orchestration-design.md` |
| Browser client skeleton (WS client, browser events, rolling buffer, kill-switch UI) | **Implemented (turn N+8, 63 Vitest tests)** | `client/`, `docs/02-ingestion-layer-design.md` |
| Client-side inference (FaceDetector, FaceLandmarker + capture loop) | **Implemented (turn N+9, 113 Vitest tests)** | `client/src/media-capture.ts`, `client/src/face-inference.ts`, `client/src/capture-loop.ts`, `docs/04-inference-modules-design.md` |

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
  container applied the initial migration (which creates the entire
  current schema — all 12 tables, all 9 enum types, the
  `termination_record_immutable` trigger) and the second migration
  (the `flag_immutable` trigger). The `admin_users` table and
  `admin_role` enum are part of the initial schema.
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

### WebSocket protocol (turn N+2)

- `pytest tests/test_websocket.py` → **88 passed**
  (envelope validation, server message serialisation, delivery service, telemetry event buffer, real testclient WebSocket dispatch).
- Total unit tests passing on SQLite: 230.

### Preprocessing (turn N+3)

- `pytest tests/test_preprocessing.py` → **127 passed**
  (frame decode, BGR→RGB swap, BGRA→RGB alpha strip, YOLO
  pass-through, PCM-16 LE/BE decode, VAD-rate resample,
  frame-splitting with tail pad, RMS dBFS calculation, modality
  scheduler default/custom/validation, rolling-buffer config/entry
  validation, eviction, `NullRollingBuffer`, `RollingBuffer`
  protocol, `_approx_decoded_size`, package-level export surface).
- Found and fixed a defect in `scheduler.py`: the `_Default`
  slotted-dataclass's class-level attributes are descriptors, not
  int values — replaced with plain `Final[int]` module constants.
- Dependencies added: `numpy>=1.26,<3`, `opencv-python-headless>=4.10,<5`,
  `Pillow>=10.4,<12`.
- Total unit tests passing on SQLite: **357**.

### Inference modules (turn N+4)

- `pytest tests/test_inference.py` → **79 passed, 1 skipped**
  (ConfidenceInterval boundary validation, BoundingBox boundary
  validation, InferenceResult base + 5 modality subclasses, face
  presence env-var / missing-file guards, identity cosine similarity
  including orthogonal / identical / near-match / anti-correlated /
  zero-vector / unequal-length / empty / 128-d, IdentityBackend ABC
  instantiation guard, IdentityMatchRunner with stub backend covering
  match / mismatch / exact-threshold / invalid-threshold,
  FaceRecognitionBackend import-skip, EAR formula at open / closed /
  degenerate, iris-offset at centred / off-centre / zero-width,
  solvePnP head-pose synthetic, FaceLandmarkerRunner env-var /
  missing-file guards, denylist constants + COCO class IDs,
  filter_denylist_detections at empty / no-match / single-match /
  mixed, ObjectDetectorRunner env-var / missing-file guards, VAD
  silence / speech / elevated-rms / empty / aggressiveness 0-3 /
  invalid 4, browser events for all 7 valid types / invalid / empty
  detail / detail with data / None detail).
- The skip is the `face_recognition` (dlib) backend import test —
  `dlib` requires MSVC Build Tools on Windows, so it's
  `pytest.importorskip` gated.
- Dependencies added: `webrtcvad-wheels>=2.0,<3`,
  `face-recognition>=1.3,<2` (Linux k8s deployable; Windows skip
  via `importorskip`).
- Total unit tests passing on SQLite: **436**.

### User decisions resolved this turn (turn N+4)

| Question | Answer |
|---|---|
| Identity-match library | `face_recognition` (dlib) |
| MediaPipe model bundles | `MP_FACE_DETECTOR_BUNDLE` + `MP_FACE_LANDMARKER_BUNDLE` env vars |
| YOLO weights | `YOLO_WEIGHTS_PATH` env var |
| Layer scope | All six modalities in one atomic turn |

### Fusion & flagging engine (turn N+5)

- `pytest tests/test_fusion.py` → **68 passed** (zero-tolerance
  path boundaries, gaze-away ladder at all four limits, accumulated
  score at exactly the threshold, window expiry, exemption
  suppression with all mismatch modes, book severity across all three
  `ReferenceMaterialPolicy` values, browser event accumulation).
- New `src/proctoring_engine/fusion/` package:
  - `_types.py` — `GazeAwayEvent` + `FlagDecision` dataclasses
  - `aggregator.py` — `PolicySnapshot`, `SessionContext`,
    `SessionAggregator` (three termination paths in one class)
  - `exemptions.py` — `ExemptionRecord` +
    `find_matching_exemption` pure function
  - `book_severity.py` — `should_flag_book` pure function
- All thresholds live in `PolicySnapshot` (never hardcoded).
- Accumulated-score path uses `medium_score_termination_threshold = 0`
  as the documented "disable" sentinel — schema constraint
  `ck_policy_medium_score_threshold_nonnegative` permits 0.
- User resolved the accumulated-score open decision this turn:
  single accumulator across all MEDIUM signals, weights in
  `PolicyConfig`, threshold set pre-exam as part of the versioned
  snapshot, not adjustable mid-session.
- Resume/reinstatement: **superseded (2026-07-25) — see correction
  below.** This turn's original call was "explicitly out of v1;
  termination is final from the engine's perspective." That was
  reached without visibility into the fuller live-proctor-override
  design worked through separately, which specifically requires an
  engine-side undo mechanism for the accumulated-score
  act-immediately/fast-track-undo path to mean anything.
  **Corrected resolution: the fast-track undo via the existing
  `ProctorReview` overturn path stands** — `ExamSession.status` gains
  `reinstated`. This does not reopen the separate,
  still-genuinely-open question of whether the *LMS's own* attempt/grade
  lifecycle can be resumed (depends on the unresolved
  own-client-vs.-embedded-native-quiz architecture) — only the
  engine-side session state. See `05-fusion-flagging-engine-design.md`
  Path 3 for the full design.
- Total unit tests passing on SQLite: **504** (436 prior + 68 new).

### Evidence & audit store (turn N+6)

- `pytest tests/test_evidence.py` → **58 passed** (settings loading
  with all required and optional vars, SHA-256 checksum boundaries,
  storage key building/parsing across all four artifact types,
  `InMemoryEvidenceStore` upload/download/delete/exists/checksum,
  `seal_evidence` happy path / invalid type / upload failure /
  checksum mismatch / frame / event_export, retention deletion with
  expired-only / unexpired-untouched / multiple-expired / empty-DB /
  blob-already-missing / storage-error-leaves-row, protocol
  compliance, package exports).
- New `src/proctoring_engine/evidence/` package:
  - `_settings.py` — `EvidenceStoreSettings` (frozen, slots) +
    `get_evidence_store_settings` from process env
  - `_protocol.py` — `EvidenceStore` runtime-checkable Protocol +
    `EvidenceStoreError` + `EvidenceNotFoundError`
  - `_checksum.py` — `compute_sha256`, `validate_sha256_hex`,
    `verify_checksum`
  - `_storage_key.py` — `build_storage_key`, `parse_storage_key`,
    `get_artifact_extension` (all four artifact types: frame,
    clip, audio, event_export)
  - `_s3.py` — `S3EvidenceStore` (production boto3 adapter with
    create-on-first-use, retry, bucket ensure) +
    `InMemoryEvidenceStore` (test double, thread-safe)
  - `service.py` — `SealEvidenceRequest` +
    `SealEvidenceResult` + `seal_evidence` (blob-first,
    row-second; verify remote checksum after upload; deletes
    blob on mismatch)
  - `retention.py` — `run_retention_deletion` + `RetentionDeletionResult`
    with `failed_artifact_ids` set to prevent infinite-loop on
    persistently failing blob deletion
- Storage key shape: `evidence/{session_id}/{flag_id}/{type}.{ext}`
  (mirrors `docs/06` §1).
- Retention ordering: blob-first, row-second; "blob-already-missing"
  is idempotent and continues to row deletion; "storage error"
  logs and skips without deleting the row.
- The `TelemetryEvent.retention_expires_at` query path appears in
  the design doc §3 but the ORM doesn't have that column on
  `TelemetryEvent` (the `ExamSession.cascade="all, delete-orphan"`
  relationship handles telemetry retention at the parent
  session's `retention_expires_at`). The retention worker
  therefore scopes to `EvidenceArtifact` only in v1.
- `boto3>=1.34,<2` added to `pyproject.toml`; new S3 env vars
  (`S3_ENDPOINT_URL`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`,
  `S3_BUCKET`, `S3_REGION`, `S3_CONNECT_TIMEOUT_SECONDS`,
  `S3_READ_TIMEOUT_SECONDS`) added to `.env.example` (matching
  the docker-compose.yml + k8s ConfigMap variables).
- Total unit tests passing on SQLite: **562** (504 prior + 58 new).

### API / orchestration layer (turn N+7)

- `pytest tests/test_orchestration.py` → **73 passed** (settings
  loading with min-32-byte bound, full state-machine transition table
  for all 5 × 5 status pairs, internal terminate token parsing +
  constant-time compare, learner / instructor / proctor / admin
  auth at the dependency level, all four admin route surfaces,
  session-status ownership rule, evidence-flush blob-first
  row-second route, flag-row persistence service with append-only
  invariant).
- New `src/proctoring_engine/orchestration/` package:
  - `_settings.py` — `OrchestrationSettings` (frozen, slots)
    with `INTERNAL_TERMINATE_TOKEN` (min 32 bytes,
    `hmac.compare_digest`) and `retention_default_seconds`.
  - `_state_machine.py` — `can_transition` / `assert_transition` /
    `apply_transition` over the closed SessionStatus transition
    table; `PENDING→ACTIVE` remains owned by the WebSocket
    handshake (no circular dependency).
  - `_auth.py` — `require_internal_terminate_token` (Bearer parser +
    closed error envelope), `require_admin_role` (joins through
    the participant row to the AdminUser's
    `lms_user_reference`, mirroring `process_launch`),
    `require_session_owner_or_admin` for `/sessions/{id}/status`.
  - `_flag_persistence.py` — `persist_flag_decision` translates a
    `FlagDecision` into immutable `Flag` + `FlagTelemetryEvent`
    rows in a single transaction; append-only invariant preserved
    via the existing `flag_immutable` PostgreSQL trigger.
  - `_admin_service.py` — `create_policy_version`,
    `create_exemption`, `list_flags_for_session`,
    `record_proctor_review`, plus typed errors mapped to
    404/409/422 by the routes layer.
  - `_evidence_service.py` — `seal_evidence_for_flag` wraps
    `seal_evidence` with a blob-first / row-second transaction
    and the deferred-gap evidence-flush route
    (`POST /sessions/{id}/flags/{flag_id}/evidence`).
  - `_schemas.py` — Pydantic v2 request / response models;
    `extra="forbid"` locks the wire shape.
  - `_routes.py` — `build_orchestration_router(deps)` mounts the
    seven routes from `docs/07` §1 + the evidence flush route;
    closed error-code envelope (`{"code": ..., "message": ...}`)
    is the contract for every failure path.
  - `_errors.py` — the closed code-to-HTTP-status mapping
    (`internal_token_required → 401`, etc.); shared between auth
    and routes so the two layers can't drift.
- `src/proctoring_engine/api.py` lifespan extended to construct
  and mount the orchestration router when settings are configured;
  same fail-closed pattern as the LTI + WebSocket layers.  A
  new `install_evidence_store` seam exposes the `EvidenceStore`
  for the lifespan to install.  If the evidence-store env vars
  are missing, the lifespan falls back to an in-memory store
  (non-persistent, dev-mode).
- New env vars: `INTERNAL_TERMINATE_TOKEN`,
  `ORCHESTRATION_RETENTION_DEFAULT_SECONDS`.
- Total unit tests passing on SQLite: **635** (562 prior + 73 new).

**New open decision surfaced this turn:**

8. **`PolicyConfig.name` uniqueness vs. versioning.** The v1 spec
   promises "name uniquely identifies a family of versions"
   (`docs/01-data-models-design.md`), but the v1 schema enforces
   `name` as a column-level `unique=True`.  Two POSTs with the
   same `name` collide, even when the first is retired via
   `retire_previous=True`.  Surfaced here so the v2 fix can be a
   schema migration: drop `unique=True` and replace with a
   partial unique constraint on `(name, is_active=True,
   retired_at IS NULL)`.  Until then, callers must give each
   version a distinct `name`.

## 3. What is NOT yet verified

- The Kubernetes manifest set has not been applied to a live
  cluster. The configuration is reviewed; the cluster is the next
  environment to provision.
- LTI 1.3 launch / WebSocket transport / inference / fusion / storage
  — all the upper layers. None are built.
- Live load testing on a real Postgres instance. The schema targets
  thousands of concurrent sessions (revised 2026-07-24 from the
  original "moderate, tens–low hundreds" figure) with the documented
  k8s sizing; a load test is the next environment to provision
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

  > **✅ Resolved (2026-07-24):** user confirmed "thousands" directly.
  > `docs/proctoring-engine-v1-spec.md` §1 has been updated to match —
  > it previously locked "moderate concurrency (tens–low hundreds)" and
  > that figure is now superseded, not just contradicted. Two downstream
  > consequences to carry into the next atomic layer (inference modules)
  > and beyond:
  > 1. **Worker-pool / GPU sizing for inference.** YOLOv8 (torch) and the
  >    MediaPipe Tasks API models are the heavy per-frame cost in this
  >    system. A design that was fine for "low hundreds" of sessions on
  >    a modest worker pool needs real batching/autoscaling analysis at
  >    thousands — this should be an explicit section in
  >    `docs/04-inference-modules-design.md`, not assumed to fall out of
  >    "horizontally scalable" for free.
  > 2. **WebSocket ingress affinity.** Already logged in §10 below as a
  >    risk ("breaks down past ~10 API replicas on a single ingress") —
  >    at "low hundreds" that limit was distant; at "thousands" it's
  >    close to immediately binding. The gateway migration path in
  >    `docs/DEPLOYMENT.md` §6 moves from "document for later" to
  >    "needed before the WebSocket layer can actually carry this load,"
  >    and should be revisited before treating that layer as done.
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
   table is part of the initial schema (created by the initial
   migration's `Base.metadata.create_all`). The three referencing
   tables (`PolicyConfig.created_by_id`,
   `AccommodationExemption.approved_by_admin_id`,
   `ProctorReview.reviewer_admin_id`) carry FK columns alongside the
   original string fields for backward compatibility. An earlier
   `20260719_0003_admin_user.py` migration was entirely redundant and
   has been removed; `tests/test_migration_chain.py` enforces the
   invariant that no post-initial migration may re-add anything the
   initial migration already emits.
2. **Identity-match library** — **Resolved (2026-07-24, turn N+4).**
   `face_recognition` (dlib ResNet, 128-d embeddings). Installable
   on the Linux k8s target; Windows tests `pytest.importorskip` the
   backend. The library is wired in via the `IdentityBackend` ABC
   so a future swap to ArcFace / DeepFace is a single-class change.
3. **MediaPipe model bundle (`.task`) distribution** — **Resolved
   (2026-07-24, turn N+4).** `MP_FACE_DETECTOR_BUNDLE` and
   `MP_FACE_LANDMARKER_BUNDLE` environment variables point to
   pre-baked bundle files in the container image. Constructors
   raise `EnvironmentError` if unset; unit tests run without the
   bundles, exercising only the env-var and file-existence guards.
4. **YOLOv8 weights distribution** — **Resolved (2026-07-24, turn
   N+4).** `YOLO_WEIGHTS_PATH` env var. `ObjectDetectorRunner` raises
   `EnvironmentError` if unset.
5. **Accumulated-score termination path** — **Resolved (2026-07-25,
   turn N+5).** Path is wanted. Single running accumulator across
   all `MEDIUM` flags on `ExamSession.accumulated_medium_score`
   (Numeric(10,4), default 0). Weight per `rule_code` is supplied
   to the `PolicySnapshot.score_weights` dataclass at session
   start; in production it will be loaded from the
   `PolicyConfig.extra_rules` JSONB column by the orchestration
   layer (not yet built). Default weight is `1.0` per
   `MEDIUM` flag regardless of rule code. Threshold set by
   `PolicyConfig.medium_score_termination_threshold`; `0` is the
   documented disable sentinel (the
   `ck_policy_medium_score_threshold_nonnegative` check permits 0).
   Implemented by `SessionAggregator._check_accumulated_threshold`
   in `src/proctoring_engine/fusion/aggregator.py`. Threshold is
   set before the exam starts as part of the versioned
   `PolicyConfig` snapshot — not adjustable mid-session.
6. **Embedding storage mechanism** — JSONB float array (current) vs.
   `pgvector` (alternative). Settled for v1 as JSONB; revisitable if
   a "search across many embeddings" use case appears.
7. **TelemetryEvent retention column** — `docs/06-evidence-audit-store-design.md`
   §3 says retention deletion "queries for `EvidenceArtifact` and
   `TelemetryEvent` rows where `retention_expires_at < now()`,"
   but the v1 ORM does not put a `retention_expires_at` column on
   `TelemetryEvent` — telemetry retention is governed by the
   parent `ExamSession.retention_expires_at` (the
   `cascade="all, delete-orphan"` relationship on
   `ExamSession.telemetry_events` cascades deletion when the
   session itself expires). The retention worker therefore
   scopes to `EvidenceArtifact` only. Flagging here as a
   design-doc/schema inconsistency to revisit in v2 if per-event
   retention tuples become a requirement.

## 6. Library / model availability constraints (sandbox-side)

> **Correction (2026-07-24):** the two bundled-and-verifiable claims below
> for `mp.solutions` and `webrtcvad` were wrong. `mp.solutions` no longer
> exists in current `mediapipe` releases (0.10.31+); `webrtcvad` has no
> Windows wheel. Both are corrected below — this is a planning-level fix,
> not an inference-modules-layer implementation detail, since the choice
> of library is a prerequisite the inference layer's design doc depends on.

- MediaPipe Tasks API (`FaceDetector`, `FaceLandmarker` w/ blendshapes):
  **this is now the v1 choice**, not a deferred "not in v1" item. Model
  bundle (`.task` file) downloaded from `storage.googleapis.com` at first
  run — **not** bundled in the pip wheel. Bake it into the container
  image at build time rather than fetching at runtime, given the k8s
  deployment target.
- ~~MediaPipe `mp.solutions`~~: removed from current `mediapipe` releases;
  do not use.
- `webrtcvad-wheels` (MIT fork of `webrtcvad`, identical API): prebuilt
  wheels for Windows/macOS/Linux, Python 3.6–3.13. **Verifiable here.**
- ~~`webrtcvad` (base)~~: no Windows wheel; needs MSVC Build Tools to
  compile there.
- YOLOv8 (Ultralytics): weights auto-download from GitHub Releases
  at first run. **Verifiable here.**
- `face_recognition` (dlib ResNet) vs. `DeepFace`: `face_recognition`
  resolved as the v1 choice (turn N+4). The library is wired in via
  the `IdentityBackend` ABC so a future swap to ArcFace / DeepFace is
  a single-class change. The `--no-deps` install sequence codified in
  the `Dockerfile` builder stage (lines 62-80) is the verified
  workaround for the `pkg_resources` removal in `setuptools>=82`.
- `uniface[cpu]` (MIT) wrapping MiniFASNetV2 (Apache 2.0) via ONNX
  Runtime: v1 choice for the liveness / anti-spoofing modality
  (turn N+12, item 11). Verified by direct install: imports cleanly,
  ~58 MB for `onnxruntime`. Weights `MiniFASNetV2.onnx` pinned to
  SHA-256 `b32929adc2d9c34b9486f8c4c7bc97c1b69bc0ea9befefc380e4faae4e463907`;
  load-time verification via `LIVENESS_MODEL_PATH` env var. Catches
  **print and screen-replay spoofing only** — not deepfakes or 3D
  masks (the underlying model's documented capability; v1 does not
  cover deepfakes or 3D masks).
- `pgvector`: not used in v1; the JSONB approach is documented.
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
2. ~~`AdminUser` table — resolve the open admin-identity decision.~~ **Done** (initial schema, no migration needed; regression-tested by `tests/test_migration_chain.py`).
3. ~~LTI 1.3 launch routes + `process_launch` service + OIDC test double + PostgreSQL integration tests.~~ **Done** (turn N+1).
4. ~~Authenticated WebSocket event schema, sparse-frame protocol,
   evidence-buffer upload, kill-switch acknowledgement.~~ **Done** (turn N+2).
5. ~~Preprocessing layer (decode, tiered scheduler, rolling buffer
   contract).~~ **Done** (turn N+3).
6. ~~Inference modules (6 modalities).~~ **Done** (turn N+4).
6. ~~Object-storage abstraction with checksums, encryption metadata,
   retention deletion worker, and test doubles.~~ **Done** (turn N+6).
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
- **WebSocket affinity breaks down past ~10 API replicas on a single
  ingress.** **Resolved (2026-07-25): Cloudflare Durable Objects** —
  not yet implemented in code. Pricing/regional-latency still needs a
  real check before committing budget.

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
- [x] Migration-chain structural regression tests
      (`tests/test_migration_chain.py` — no post-initial migration may
      re-add an enum value, column, table, index, or constraint that
      the initial migration's `Base.metadata.create_all` already emits;
      the `20260719_0003_admin_user.py` migration was deleted as
      redundant and this guard prevents recurrence)
- [x] Authenticated WebSocket protocol (envelope, sparse frames,
      kill-switch, ack)
- [x] Ingestion layer implementation
- [x] Preprocessing layer (decode, tiered scheduler, rolling buffer
      contract) — 127 unit tests passing (turn N+3)
- [x] Inference modules (face presence, identity, head pose / gaze,
      object, audio VAD, browser events) — 79 unit tests passing
      (turn N+4)
- [x] Fusion & flagging engine (3 paths + exemption suppression +
      book severity) — 68 unit tests passing (turn N+5)
- [x] Evidence store (S3-compatible adapter, checksums, retention
      deletion job) — 58 unit tests passing (turn N+6)
- [x] API / orchestration (full route surface, state machine,
      authorization) — 73 unit tests passing (turn N+7)
- [x] Browser client + capture (skeleton, WebSocket, browser events,
      rolling buffer, kill-switch UI) — 63 Vitest client tests + 4
      updated Python tests (639 total Python unit tests) passing
      (turn N+8). Session-token delivery fixed at both ends: URL
      fragment for the LTI redirect, `Sec-WebSocket-Protocol`
      subprotocol header for the WS handshake. Query-param token
      rejected by the server (no fallback).
- [x] Client-side inference (FaceDetector, FaceLandmarker via
      `@mediapipe/tasks-vision` + heavy-frame capture loop) — 113
      total Vitest client tests passing (turn N+9). Three new modules:
      `media-capture.ts` (getUserMedia wrapper + JPEG frame encoding),
      `face-inference.ts` (FaceDetector + FaceLandmarker wrappers with
      lazy async init), `capture-loop.ts` (RAF-driven capture loop
      coordinating light frames at 1s intervals and heavy frames at
      2.5s intervals). Heavy frames stored in RollingBuffer for
      evidence retention. Iris-position-based gaze/off-screen
      classification. `RollingBuffer.add()` extended for heavy-frame
      metadata. Build (`npm run build`) succeeds.
- [x] Redis-backed `LaunchStateStore` — 14 new tests passing
      (turn N+10). Extracts `LaunchStateStore` as a `Protocol`; renames
      the original in-memory class to `InMemoryLaunchStateStore`;
      adds `RedisLaunchStateStore` (async, uses `redis.asyncio`).
      Atomic consume via Lua script (`EVALSHA`-cached). Wire via
      `LTI_STATE_STORE_BACKEND=redis` + `REDIS_URL`. The route
      handlers `await` whichever store is wired in. New dep:
      `redis>=5.0,<6` + dev-only `fakeredis[lua]>=2.20,<3`. Total:
      653 Python unit + 113 client tests passing.
- [x] Cross-verification pass — 4 confirmed bugs + 1 contract drift +
      1 doc fix. `RollingBuffer.heavyFrameEntries` time-window
      eviction added; `main.ts` capture loop now gated on WS
      `'connected'` state; ack payload field names fixed
      (`seq`/`received_at`); kill-switch retry type and dedup guard
      added; WebSocket route doc fixed. Total: 120 client tests
      passing. (Turn after N+10.)
- [x] Dead client-side gaze code removed — `sendGazeTelemetry()` +
      client-side `FaceLandmarker` deleted (rejected by server-side
      `TelemetryLightPayload` validation; gaze is server-side only on
      the raw heavy frame JPEG). ~2 MB WASM + model download saved per
      session. Total: 116 client tests passing.
- [x] `PolicyConfig.name` partial unique constraint — dropped
      `unique=True`, added `CREATE UNIQUE INDEX ... WHERE is_active
      = true AND retired_at IS NULL` (`uq_policy_configs_active_name`).
      Initial migration's `Base.metadata.create_all` picks up the
      change; no post-initial migration needed. Migration chain test
      invariant preserved. Total: 655 Python unit tests passing.
- [x] `medium_score_action` + `liveness / anti-spoofing` modality
      (items 4 + 11, combined turn — same aggregator branching
      shape). `PolicyConfig.medium_score_action` (`auto_terminate` |
      `flag_for_review`) added; aggregator's `_check_accumulated_threshold`
      branches on the configured action. `SessionStatus.REINSTATED`
      added with `TERMINATED → REINSTATED` and `REINSTATED →
      UNDER_REVIEW` state-machine transitions. New
      `src/proctoring_engine/inference/liveness.py` wraps
      `uniface[cpu]` (MiniFASNetV2 / ONNX Runtime); weights pinned
      via SHA-256 at load time. New `PolicyConfig.liveness_check_enabled`
      / `liveness_check_action` / `liveness_score_threshold` fields
      + 2 SQL CHECK constraints. New `TelemetryModality.LIVENESS`,
      `RULE_LIVENESS_CHECK_FAILED` rule code, `process_liveness()`
      aggregator method. `LIVENESS_MODEL_PATH` env var. Catches print
      and screen-replay spoofing only — not deepfakes or 3D masks
      (documented honestly). Total: 686 Python unit + 116 client
      tests passing.
- [ ] Production deployment (k8s cluster provisioned, end-to-end
      smoke test on a live cluster)
