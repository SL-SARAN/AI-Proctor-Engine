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
   table is part of the initial schema (created by the initial
   migration's `Base.metadata.create_all` from the post-resolution ORM
   in `src/proctoring_engine/models.py`). The three referencing tables
   (`PolicyConfig.created_by_id`,
   `AccommodationExemption.approved_by_admin_id`,
   `ProctorReview.reviewer_admin_id`) carry FK columns alongside the
   original string fields for backward compatibility. An earlier
   `20260719_0003_admin_user.py` migration was entirely redundant and
   has been removed; `tests/test_migration_chain.py` enforces the
   invariant that no post-initial migration may re-add anything the
   initial migration already emits.
2. **Accumulated-score termination path** — **Resolved
   (2026-07-25, turn N+5).** Path is wanted; single running
   accumulator across all `MEDIUM` flags on
   `ExamSession.accumulated_medium_score`. Threshold set by
   `PolicyConfig.medium_score_termination_threshold` before the
   exam starts as part of the versioned `PolicyConfig` snapshot
   (not adjustable mid-session). Threshold of 0 is the documented
   disable sentinel (`ck_policy_medium_score_threshold_nonnegative`
   permits 0). Weight per `rule_code` is supplied via the
   `PolicySnapshot.score_weights` constructor argument (production
   builds load this from `PolicyConfig.extra_rules` JSONB via the
   orchestration layer, not yet built); default 1.0 per `MEDIUM`
   flag. The `SessionAggregator` implements Path 3
   (`docs/05-fusion-flagging-engine-design.md`).
3. **Resume / reinstatement after termination** — **Resolved
   (2026-07-25, turn N+5): explicitly out of v1.** The engine is a
   proctoring sidecar; the LMS owns and delivers the quiz. The
   engine terminates the proctoring session (lock capture client,
   seal evidence, LTI AGS score submission). The instructor uses
   the LMS's native tools (e.g. Canvas "Moderate Quiz") to reopen
   attempts or adjust grades, informed by the engine's evidence
   dashboard. This avoids per-LMS proprietary integration for
   capability the LMS already has natively.
4. **Browser client capture architecture** — **Resolved (2026-07-25,
   turn N+5): LTI launch → capture client as the active browser
   tab.** The LMS quiz runs inside an iframe (or as a separate URL
   navigated to after proctoring setup). Capture client captures
   webcam/mic via `getUserMedia` and browser events on its own
   document (`visibilitychange`, `blur`, `fullscreenchange`,
   `copy`/`paste`/`contextmenu`). Browser-extension and companion-
   window approaches were rejected: extensions are a deployment
   barrier (per-LMS installation), can be disabled by the student,
   and break the "LTI is the integration contract" boundary.
5. **Embedding storage mechanism** — `pgvector` extension vs.
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
| Read the API / orchestration layer (turn N+7) | `src/proctoring_engine/orchestration/` (`_settings.py`, `_state_machine.py`, `_auth.py`, `_flag_persistence.py`, `_admin_service.py`, `_evidence_service.py`, `_schemas.py`, `_errors.py`, `_routes.py`) |
| Read the API / orchestration tests (turn N+7) | `tests/test_orchestration.py` (73 unit cases) |
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

The **evidence & audit store** is now complete (turn N+6). The
**API / orchestration layer** is the next atomic layer.

### What is built (turns N through N+6)

| Turn | Layer | Tests |
|---|---|---|
| N | LTI 1.3 foundation (`LtiSettings`, claims, roles, `LaunchStateStore`, session token, OIDC discovery, JWKS) | 70 unit |
| N+1 | LTI 1.3 launch routes + `process_launch` service + OIDC test double + PostgreSQL integration | 134 unit + 9 integration |
| N+2 | Authenticated WebSocket protocol (envelope, sparse frames, kill-switch, ack) | 88 unit |
| N+3 | Preprocessing (frame decode, audio pipeline, tiered scheduler, rolling buffer contract) | 127 unit |
| N+4 | Inference modules (face presence, identity match, head pose/gaze, object detection, audio VAD, browser events) | 79 unit |
| N+5 | Fusion & flagging engine (3 termination paths + exemption suppression + book severity) | 68 unit |
| N+6 | Evidence & audit store (S3-compatible adapter, checksums, retention deletion job) | 58 unit |
| N+7 | API / orchestration (full route surface, session state machine, internal `INTERNAL_TERMINATE_TOKEN`, admin CRUD, evidence flush) | 73 unit |
| N+8 | Browser client skeleton (WS subprotocol auth, browser events, rolling buffer, kill-switch UI), plus LTI fragment fix | 63 client + 4 Python |

**Total: 639 unit + 63 client tests passing, 16 PostgreSQL integration tests passing.**

### Library availability (2026-07-24 corrections applied)

- **MediaPipe Tasks API** — `FaceDetector`, `FaceLandmarker` (478 iris-refined landmarks, blendshapes). Bundle (`.task`) path via `MP_FACE_DETECTOR_BUNDLE` / `MP_FACE_LANDMARKER_BUNDLE` env vars.
- **`webrtcvad-wheels`** — prebuilt wheels, Windows-friendly.
- **YOLOv8 (Ultralytics)** — weights via `YOLO_WEIGHTS_PATH` env var.
- **`face_recognition` (dlib ResNet, 128-d)** — env-var contract; Windows tests `importorskip`-gated.

### Open decisions (do not silently resolve)

Per `SKILLS_ALIGNMENT.md` §7, these still require explicit user choice:

1. **`PolicyConfig.name` uniqueness vs. versioning** — `unique=True` blocks retiring old versions via same-name. Fix requires schema migration.
2. **Identity-match runtime-failure handling** — designed (fail-closed + break-glass override entity), but needs admin route implementation.

(Note: WebSocket affinity is resolved as Cloudflare Durable Objects, and Redis-backed LaunchStateStore is resolved; see below).

### Resolved this turn (turn N+5)

- **Accumulated-score termination path** — single running `accumulated_medium_score` across all `MEDIUM` flags; threshold set pre-exam via `PolicyConfig.medium_score_termination_threshold`; `0` is the documented disable sentinel. Per-`rule_code` weight is supplied to the aggregator via `PolicySnapshot.score_weights` (default 1.0); production builds load this from `PolicyConfig.extra_rules` JSONB via the orchestration layer.
- **Resume / reinstatement** — explicitly out of v1. Engine is a proctoring sidecar; LMS handles attempt lifecycle through its own tools.
- **Browser client capture architecture** — LTI launch → capture client as the active browser tab; LMS quiz in iframe. Browser-extension and companion-window approaches rejected.
- **Evidence-retention telemetry mismatch** (surfaced turn N+6) — `docs/06` §3 mentions a `TelemetryEvent.retention_expires_at` query, but the v1 ORM does not put that column on `TelemetryEvent`. Telemetry retention cascades off the parent `ExamSession.retention_expires_at` (`cascade="all, delete-orphan"`). The retention worker therefore scopes to `EvidenceArtifact` only in v1; the design doc has a minor inconsistency to revisit if per-event retention tuples become a requirement.

### Resolved this turn (turn N+4)

- Identity-match library: `face_recognition` (dlib).
- MediaPipe bundle path: env var.
- YOLO weights path: env var.

### Resolved this turn (turn N+8)

- **Session token delivery** — no longer travels in any query string. The LTI redirect uses the URL fragment (`#`); the WebSocket handshake uses the `Sec-WebSocket-Protocol` header. Resolves the leak outlined in `docs/02` §2.
- **Client test architecture** — Vanilla TypeScript + Vite + Vitest, run in a separate `client-ci` GitHub Actions job.
- **Client serving** — multi-stage Docker build produces `/client-dist/`, served by the FastAPI `StaticFiles` mount at `/client/`.

### Resolved this turn (turn N+9)

- **Client-side inference** — Three new modules added to `client/src/`:
  - `media-capture.ts` — `getUserMedia` wrapper with constraint
    validation, frame extraction, JPEG base64 encoding.
  - `face-inference.ts` — `FaceInferenceRunner` wrapping MediaPipe
    Tasks Vision `FaceDetector` (face count, every frame) and
    `FaceLandmarker` (478 iris-refined landmarks, heavy frames only).
    Lazy async init via `FilesetResolver.forVisionTasks()`.
  - `capture-loop.ts` — `CaptureLoop` coordinator driven by
    `requestAnimationFrame`. Light telemetry sent every 1s, heavy
    frames (JPEG + landmarks) every 2.5s. Heavy frames stored in the
    `RollingBuffer` for evidence retention. Iris-position-based
    gaze/off-screen classification (Landmark 468/473 vs. eye corners
    33/263).
- **RollingBuffer extended** — new `add(HeavyFrameEntry)` method to
  accept the extended metadata format from the capture loop. Existing
  `push()` API unchanged for backward compatibility.

### Resolved this turn (turn N+10)

- **Redis-backed `LaunchStateStore`** — `src/proctoring_engine/lti/state.py`
  refactored:
  - `LaunchStateStore` is now a runtime-checkable `Protocol`
    documenting the contract that route handlers depend on.
  - `InMemoryLaunchStateStore` (the previous class, renamed) is the
    single-replica default; its methods are `async def` so the
    `await` shape is uniform.
  - `RedisLaunchStateStore` (new) uses `redis.asyncio`. Atomic
    consume is enforced by a Lua script (`EVALSHA`-cached at
    construction) so a state cannot be consumed twice across
    concurrent callers in different replicas. Per-key TTL is
    enforced by Redis itself (`SET ... EX ttl_seconds`).
- **`LtiSettings` extended** with `state_store_backend` (one of
  `memory` or `redis`) and `redis_url`. The FastAPI lifespan
  in `api.py` wires `RedisLaunchStateStore` when the env vars say
  so; otherwise the in-memory default is used.
- **CONTEXT.md doc fix** — removed "WebSocket affinity / gateway
  architecture" from §4 (Open decisions). The other three files
  already recorded the resolution as Cloudflare Durable Objects
  (2026-07-25); CONTEXT.md was the stale one. The remaining open
  decisions are now correctly: `PolicyConfig.name` uniqueness
  schema migration, and identity-match runtime-failure handling.

### The next atomic layer

**Production deployment** (turn N+11). All client and server code
layers are now complete: the data model, ingestion, preprocessing,
server-side inference, fusion, evidence, orchestration, browser
client, client-side inference, and now the Redis-backed
launch-state store. The remaining unchecked item is the live
Kubernetes cluster provisioning and end-to-end smoke test on a
real cluster. The manifests under `k8s/00-…` through `k8s/10-…`
are reviewed; the cluster itself is the next environment to
provision.
