# Architecture

This file is the **iteration surface** for the AI Proctoring Engine.
It is derived from `docs/proctoring-engine-v1-spec.md` and the
per-layer design docs in `docs/0X-*.md`, and it is the place where
architectural decisions are recorded as they are made or revised
during implementation. The locked spec remains the source of truth
for requirements; this file captures the resulting **project flow** —
what runs, in what order, with what contracts, and what we are
deliberately not doing yet.

When a decision changes (for example, a previously deferred option is
now adopted, or a new open decision is resolved), update the relevant
section here **and** flag it in the next turn's output. Do not let
this file drift silently from the code or the design docs.

---

## 1. System context

A student taking a proctored exam inside an LMS launches the
proctoring tool via LTI 1.3. The tool opens a WebSocket, runs a
hybrid inference pipeline against the student's webcam and microphone,
and may auto-terminate the session and notify the LMS. Human proctors
/ instructors review flagged sessions through an admin surface. The
system persists a defensible audit trail (Postgres + S3-compatible
object storage) and enforces a configurable retention window.

```
                          ┌────────────────────┐
                          │   LMS (Canvas /    │
                          │   Moodle / BB)     │
                          └─────────┬──────────┘
                                    │ LTI 1.3 (OIDC + JWT/JWKS)
                                    ▼
┌──────────────────────────────────────────────────────────────┐
│                Cloudflare (TLS + DDoS + edge)               │
└──────────────────────────────┬───────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────┐
│       Kubernetes cluster  (api + worker tiers, autoscaled)  │
│  ┌──────────────────────────┐  ┌──────────────────────────┐  │
│  │   API (FastAPI/uvicorn)  │  │  Inference worker pool   │  │
│  │   WebSocket + REST       │  │  (6 modalities, async)   │  │
│  └────────────┬─────────────┘  └─────────────┬────────────┘  │
│               │                              │               │
│   Ingestion & preprocessing                  │               │
│   decode · normalize per model ·             │               │
│   tiered sampling · rolling buffer contract  │               │
│               │                              │               │
│   Fusion & flagging engine                   │               │
│   zero-tolerance path · gaze ladder ·        │               │
│   accumulated-score path · exemption suppr.  │               │
│               │                              │               │
│   Evidence & audit store                     │               │
│   immutable Flag + TerminationRecord         │               │
│   (Postgres triggers + ORM listeners)        │               │
└───────────────┬──────────────────────────────┬───────────────┘
                │                              │
                ▼                              ▼
      ┌──────────────────┐          ┌──────────────────┐
      │ Managed Postgres │          │   Cloudflare R2  │
      │ (RDS / Cloud SQL │          │  (S3-compatible  │
      │  / Neon)         │          │   object store)  │
      └──────────────────┘          └──────────────────┘
```

## 2. Layer-by-layer project flow

The repository implements this top-to-bottom. Each layer's status,
design doc, and code anchor is enumerated; the checklist mirrors
`SYSTEM_STATE.md` §12.

| # | Layer | Status | Design doc | Code anchor |
|---|---|---|---|---|
| 1 | Data models | Implemented + reconciled | `docs/01-data-models-design.md` | `src/proctoring_engine/models.py` |
| 2 | Initial Alembic migration | Implemented | (DB-level trigger) | `migrations/versions/20260717_0001_initial_proctoring_schema.py` |
| 3 | Audit reconciliation migration | Implemented | (DB-level `flag_immutable` trigger) | `migrations/versions/20260718_0002_audit_reconciliation.py` |
| 4 | Boundary / integrity unit tests | Implemented (19 cases) | `docs/08-test-strategy-design.md` | `tests/test_models.py` |
| 5 | PostgreSQL integration tests | Implemented (12 cases) | `docs/08` integration section | `tests/integration/test_postgres_immutability.py` |
| 6 | GitHub Actions CI | Implemented | `.github/workflows/ci.yml` | `.github/workflows/ci.yml` |
| 7 | Docker + docker-compose | Implemented | `Dockerfile`, `docker-compose.yml` | (same) |
| 8 | Kubernetes manifest set | Implemented | `k8s/00-` through `k8s/10-` | (same) |
| 9 | FastAPI app shell (health only) | Implemented | `docs/07-api-orchestration-design.md` | `src/proctoring_engine/api.py` |
| 10 | `AdminUser` table | **Implemented (2026-07-19, part of initial schema)** | `docs/01` open decision | `src/proctoring_engine/models.py` (initial migration's `Base.metadata.create_all` emits it; redundant `20260719_0003_admin_user.py` was removed) |
| 11a | LTI 1.3 foundation (config, claims, roles, state store, session token, OIDC discovery, JWKS fetcher) | **Implemented (turn N, 70 unit tests)** | `docs/02-ingestion-layer-design.md` §1, §6 | `src/proctoring_engine/lti/` |
| 11b | LTI 1.3 launch routes + `process_launch` service + OIDC test double + PostgreSQL integration tests | **Implemented (turn N+1, 134 unit + 9 integration tests)** | `docs/02-ingestion-layer-design.md` §1, §6 | `src/proctoring_engine/lti/routes.py`, `src/proctoring_engine/lti/service.py`, `tests/integration/test_lti_launch.py` |
| 12 | Authenticated WebSocket protocol | Pending | `docs/02-ingestion-layer-design.md` §2–§4 | — |
| 13 | Preprocessing layer | Pending | `docs/03-preprocessing-layer-design.md` | — |
| 14 | Inference modules (6 modalities) | Pending | `docs/04-inference-modules-design.md` | — |
| 15 | Fusion & flagging engine | Pending | `docs/05-fusion-flagging-engine-design.md` | — |
| 16 | Evidence store | Pending | `docs/06-evidence-audit-store-design.md` | — |
| 17 | API & orchestration | Pending | `docs/07-api-orchestration-design.md` | — |
| 18 | Browser client + capture | Pending | n/a | — |
| 19 | Live cluster provisioning + e2e smoke | Pending | `docs/DEPLOYMENT.md` | — |

## 3. Data flow per session (end-to-end contract)

This is the project flow as it must run when all layers are
implemented. The contract between layers is fixed; the implementation
of each layer is what each turn delivers.

```
[LTI 1.3 launch]
   │
   │  iss, login_hint, target_link_uri, lti_message_hint
   ▼
GET /lti/login
   │  state, nonce, redirect_uri
   ▼
[Platform OIDC auth → id_token JWT]
   │
   ▼
POST /lti/launch
   │  validate JWKS, nonce, state, roles
   │  upsert Participant, create ExamSession (status=PENDING)
   │  bind policy_config_id (snapshot)
   │  record consent_recorded_at
   │  issue short-lived session token
   │
   ▼
WS /ws/session/{session_id}?token=...
   │  on connect: ExamSession.PENDING → ACTIVE
   │  enforce ExamSession.consent_recorded_at
   │
   ├──► client → server (envelope)
   │     • telemetry_light  (face_presence, head_pose — results only, never pixels)
   │     • telemetry_heavy_frame (JPEG every 2–3s for server-side inference)
   │     • audio_chunk (PCM/Opus, sample rate ∈ {8k, 16k, 32k, 48k}, frame 10/20/30ms)
   │     • browser_event (visibilitychange, blur, focus, fullscreenchange, copy, paste, contextmenu)
   │
   ├──► preprocessing
   │     • decode JPEG, channel-order fix for MediaPipe, byte array for YOLO
   │     • crop+align face for embedding (depends on face-presence bbox)
   │     • resample audio to webrtcvad-accepted rate, split into accepted frame sizes, compute RMS
   │     • tiered scheduling: gaze and object on every heavy frame; identity every Nth; audio per chunk
   │     • client-side rolling buffer (10–15s @ 200–500ms cadence) — never transmitted in normal flow
   │
   ├──► inference (6 modules)
   │     [1] face_presence:    per-frame face count + bbox + confidence
   │     [2] identity_match:   cosine similarity vs EnrollmentReference, mean ± std over window
   │     [3] head_pose_gaze:   per-frame off_screen bool (EAR-blink-gated; head pose OR iris offset)
   │     [4] object_detection: YOLOv8, denylist-filtered, per-frame list
   │     [5] audio_vad:        per-frame speech/silence, RMS dB; aggregation by anomaly rule
   │     [6] browser_event:    passthrough, confidence=1.0 (deterministic)
   │
   ├──► fusion & flagging engine (per ExamSession aggregator)
   │     • Path 1 (zero-tolerance): face_count≥2 for 2–3 consecutive frames → CRITICAL second_person
   │     • Path 2 (gaze ladder):  Stage-2 aggregate GazeAwayEvent if off_screen ≥ gaze_min_duration_ms
   │                              count over gaze_window_seconds ≥ gaze_warning_limit → MEDIUM gaze_away_frequency
   │                              ≥ gaze_termination_limit → CRITICAL gaze_away_frequency
   │     • Path 3 (accumulated):   weighted MEDIUM increments → ExamSession.accumulated_medium_score
   │                              ≥ medium_score_termination_threshold → CRITICAL accumulated_score
   │     • Book handling:         object_detection always logs; flag only if
   │                              ExamSession.allowed_reference_materials == closed_book
   │     • Exemption check:       before finalizing a flag involving an object_class, check
   │                              AccommodationExemption. Downgrade severity + set
   │                              Flag.suppressed_by_exemption_id; never silently drop.
   │
   ├──► Flag.triggered_termination == true
   │     │
   │     ├──► orchestration: POST /sessions/{id}/terminate (INTERNAL_TERMINATE_TOKEN)
   │     │     ExamSession.ACTIVE → TERMINATED
   │     │     INSERT TerminationRecord (append-only; ORM + DB trigger enforce)
   │     │     LTI callback to LMS (lms_delivery_status: PENDING → SENT → ACKNOWLEDGED / FAILED)
   │     │
   │     ├──► server → client: WS message { type: "kill_switch", reason, flag_id }
   │     │     client locks UI, submits, sends ack → TerminationRecord.client_acknowledged_at set
   │     │
   │     └──► evidence flush
   │           server → client: "flush buffer" (may piggyback on kill_switch)
   │           client uploads buffered clip
   │           server writes blob to R2 at evidence/{session_id}/{flag_id}/video_clip.webm,
   │           computes checksum, then INSERT EvidenceArtifact (storage_uri, content_sha256,
   │           capture_started_at/end, retention_expires_at)
   │
   └──► retention deletion worker (scheduled, off the request path)
         rows where retention_expires_at < now() → delete blob first, then DB row
         log "evidence deleted per policy" without retaining content
```

## 4. Session lifecycle state machine

```
PENDING ──(first successful WS connect)──► ACTIVE
ACTIVE  ──(normal submit)──► COMPLETED
ACTIVE  ──(auto kill-switch OR admin /terminate)──► TERMINATED
ACTIVE  ──(ProctorReview pending on a flag)──► UNDER_REVIEW
TERMINATED ──(ProctorReview created)──► UNDER_REVIEW
```

Locked constraints:

- Only the fusion engine (or an admin manual `/terminate`) can move
  `ACTIVE → TERMINATED`. A student cannot.
- `TERMINATED → UNDER_REVIEW` is allowed (auto-terminations should be
  reviewed, not just disputed ones).
- Every other transition not listed above is **rejected** at the
  state-machine layer and tested as such.

## 5. Authorization model

- **No separate permission system.** Authorization derives from the
  LTI `roles` claim in the `id_token`.
- A learner-role launch routes to the exam-taking WebSocket flow.
- An instructor / admin role launch routes to the `/admin/*`
  surfaces (policy config, accommodation exemptions, review queue).
- **One internal exception:** the fusion engine calling
  `/sessions/{id}/terminate` uses the `INTERNAL_TERMINATE_TOKEN`
  shared secret, not an LTI-derived student or instructor token.
  This is enforced in tests, not just described in docs.

## 6. Critical cross-cutting invariants

These invariants hold across the whole architecture, regardless of
which layer is being implemented:

1. **No telemetry may be persisted before
   `ExamSession.consent_recorded_at` is set.** Enforced at the
   ingestion layer (reject or accept-but-drop incoming telemetry
   until consent is on record).
2. **`Flag` and `TerminationRecord` rows are append-only.** ORM-level
   + DB-trigger-level enforcement. Corrections are new linked rows.
3. **`PolicyConfig` is a versioned snapshot.** Sessions reference a
   specific snapshot. Changes create a new version; the old one is
   not mutated.
4. **The client rolling buffer is never transmitted during normal
   operation.** It only flushes when a flag is confirmed.
5. **Every flag carries a structural proof path.** Contributing
   `TelemetryEvent` IDs, `confidence_interval`, bounding box where
   applicable, and an immutable audit record.
6. **The internal `/terminate` credential is distinct from LTI
   auth.** A student or instructor LTI token must be rejected by
   that route in tests.
7. **Object detection is denylist, not allowlist.** Anything not in
   `cell phone | laptop | tv | book` is silently ignored. Earbuds /
   smartwatches are explicitly not in v1.
8. **Accommodation exemptions are admin-approved, not self-declared.**
   The reference table exists now; suppression logic is wired in the
   fusion engine even though v1 object detection does not yet
   cover the relevant classes.
9. **Book detection always logs.** Whether it escalates to a `Flag`
   depends on `ExamSession.allowed_reference_materials`.
10. **Identity-match decisions use a multi-frame interval, not a
    single-frame point estimate.** `confidence_interval =
    {point_estimate, lower, upper}` from a sampling window.
11. **One `EvidenceArtifact` per `Flag`** in v1. The unique
    constraint `uq_evidence_artifacts_one_per_flag` enforces this.
12. **The audit-trail guarantee is verified end-to-end against a
    real PostgreSQL engine in CI** — the `termination_record_immutable`
    and `flag_immutable` triggers reject direct `UPDATE` and
    `DELETE` SQL, not just ORM-mediated writes.

## 7. Deliberate v1 gaps

- No browser client, browser-event listener, local rolling evidence
  buffer, or fullscreen / tab enforcement.
- No authenticated WebSocket protocol, sparse-frame upload path, ack
  handling, or client kill-switch.
- No LTI 1.3 / OIDC launch validation, JWKS / key rotation, grade
  passback, or LMS termination callback.
- No face detection / mesh, face embedding, identity matching, object
  detection, VAD, audio ingestion, or async worker queue.
- No fusion & flagging engine — the per-session aggregator is the
  next atomic layer.
- No S3-compatible evidence storage adapter (R2 in production,
  MinIO locally), envelope encryption / KMS, lifecycle rule,
  background retention purge, or artifact upload verification.
- No reviewer / admin API or authorization model (beyond the
  LTI-roles-derived split and the `INTERNAL_TERMINATE_TOKEN`).
- No calibration data or false-positive evaluation for gaze,
  identity, face count, or monitor detection. Default thresholds in
  `PolicyConfig` are **not validated** as final.
- **No resume / reinstatement after termination.** Per the turn N+5
  open-decision resolution: the engine is a proctoring sidecar; the
  LMS handles attempt lifecycle through its own tools.

## 8. Iteration policy (how this file changes)

This file is updated **in the same turn** as any change to the
project flow — including:

- A previously open decision is resolved (e.g. embedding storage
  mechanism, admin identity model, accumulated-score path).
- A previously deferred item is now in scope (e.g. v1 starts
  covering earbuds / smartwatches).
- A new layer is added (e.g. an additional modality, a new
  termination path).
- A locked spec decision is revised (requires explicit user
  sign-off; update the spec doc, this file, and the relevant design
  doc in the same turn).

The "Iteration log" section at the bottom of this file is the
audit trail for those changes.

## 9. Next single atomic layer (per `SKILLS_ALIGNMENT.md` §5 and `SYSTEM_STATE.md` §12)

**Evidence & audit store** (turn N+6).  The S3-compatible
evidence-storage adapter (R2 in production, MinIO locally),
checksum verification, the `EvidenceArtifact` row wired to
`Flag`, and the retention-deletion job.  This is what backs the
"sealed evidence bundle" the fusion engine's `triggered_termination`
decision ultimately produces.  See
`docs/06-evidence-audit-store-design.md`.

## 10. Deployment topology (locked)

Per the deployment-target decision:

- **Application tier:** Kubernetes. Two deployments (`api` and
  `worker`), each with its own HPA, sticky session affinity at the
  ingress for WebSocket continuity, conservative scale-down
  (proctored exams are long-lived).
- **State:** Managed Postgres (RDS / Cloud SQL / Neon) +
  Cloudflare R2 (S3-compatible, zero egress fees for evidence clip
  traffic).
- **Local dev:** Docker Compose with Postgres + MinIO + the FastAPI
  service, hot-reload on source edits.
- **CI:** GitHub Actions with cloud runners; the integration job
  uses a `postgres:15-alpine` service container.

Full topology, sizing, and the secrets model are in
`docs/DEPLOYMENT.md`.

## 11. Iteration log

| Date | Change | By |
|---|---|---|
| 2026-07-18 | Architecture file created; project flow enumerated from design docs and current code state. | Claude |
| 2026-07-18 | Open decisions (embedding storage, admin identity, accumulated-score path) recorded as open — not resolved silently. | Claude |
| 2026-07-18 | Audit reconciliation layer landed: `SessionStatus` aligned to the spec state machine, `Flag` immutability trigger added, missing spec fields filled, `EvidenceArtifact` unique constraint added, `PolicyConfig.gaze_min_duration_within_window` constraint added. The data model is now in sync with `docs/01-data-models-design.md`. | Claude |
| 2026-07-18 | Deployment target locked: Kubernetes (api + worker tiers) + managed Postgres + Cloudflare R2. CI locked: GitHub Actions. Local dev: Docker Compose with MinIO. Full topology, sizing, and secrets model in `docs/DEPLOYMENT.md`. | Claude |
| 2026-07-18 | Integration test suite (12 cases) added; runs in CI against a real PostgreSQL 15 service container. Exercises both `flag_immutable` and `termination_record_immutable` triggers under direct `UPDATE` / `DELETE` SQL. | Claude |
| 2026-07-18 | Open decision narrowed: admin identity will be a dedicated `AdminUser` table (next atomic layer). | Claude |
| 2026-07-19 | **Admin identity resolved (part of initial schema, not a separate migration).** The `AdminUser` table, the `admin_role` enum, and the FK columns on `PolicyConfig.created_by_id`, `AccommodationExemption.approved_by_admin_id`, `ProctorReview.reviewer_admin_id` (all nullable, `ON DELETE RESTRICT`) are emitted by the initial migration's `Base.metadata.create_all` from the post-resolution ORM in `src/proctoring_engine/models.py`. The original string fields are preserved for backward compatibility. 7 new unit tests + 3 new integration tests; open decisions narrowed from 3 to 2. The original `20260719_0003_admin_user.py` migration was entirely redundant and has since been removed (see 2026-07-20 entry below). | Antigravity |
| 2026-07-20 | **Migration-chain structural regression landed.** Root cause for the repeated `DuplicateObject` / `DuplicateColumn` / `type "admin_role" already exists` CI failures: the initial migration's `Base.metadata.create_all` emits the full current schema from the ORM, so any post-initial migration that re-adds an enum value, column, table, index, or constraint already declared in the ORM would collide in CI. Structural fix: (1) the third migration `20260719_0003_admin_user.py` was entirely redundant with the initial migration and has been deleted; (2) the second migration `20260718_0002_audit_reconciliation.py` was reduced to its trigger-only payload (it had been trying to re-add 12 schema elements the initial migration already emits); (3) `tests/test_migration_chain.py` (8 regression tests) renders the initial migration's DDL via `command.upgrade(config, "20260717_0001", sql=True)` and each post-initial migration's DDL via `MigrationContext` with `Operations.context()`, then asserts no post-initial migration re-adds an enum value / column / table / index / constraint that the initial DDL already contains. The only legitimate content of a post-initial migration in this project is a DML trigger — the ORM cannot model triggers, so they have to be in a follow-up migration. 134 unit + 9 integration tests still pass. | Claude |
| 2026-07-19 | **LTI 1.3 foundation landed (turn N).** New `src/proctoring_engine/lti/` package: `LtiSettings` (env-loaded, frozen, slots), `LtiIdToken` (Pydantic v2 with strict alias-only field names and a parse boundary that applies the LTI-version and message-type checks), `AppRole` + role mapper (highest-privilege wins; admin > proctor > instructor > learner), `LaunchStateStore` (in-memory, thread-safe, monotonic-clock TTL, atomic `consume`), `issue_session_token` / `decode_session_token` (HS256, 14 400 s TTL), `OidcDiscoveryCache` (per-issuer, 5 s timeout, validates `issuer` against the fetched document), `JwksCache` (TTL-bounded, refreshes on a kid-miss after a first refresh — the key-rotation handling). 70 new unit tests pass on SQLite. `pyproject.toml` now pins `httpx>=0.27,<1`, `pyjwt[crypto]>=2.8,<3`, `pytest-asyncio>=0.24,<1`, and `pytest-httpx>=0.30,<1`. LTI 1.3 layer is split into turn N (foundation) and turn N+1 (routes + service + integration tests) per the one-atomic-layer-per-turn rule. | Claude |
| 2026-07-20 | **LTI 1.3 launch routes + service landed (turn N+1).** `process_launch(db, claims, role, *, settings, now)` upserts the `Participant`, resolves the `PolicyConfig` by `name == custom.policy_config_name` and `is_active == true`, creates the `ExamSession(PENDING)` with `consent_recorded_at = started_at = now(UTC)` (consent is the act of starting the proctored session; the `ck_exam_session_timestamp_order` and `ck_exam_session_retention_after_start` checks are self-consistent at creation time), upserts the `AdminUser` (highest-privilege tier, one-way promotion — a later lower-privilege launch does not demote), and issues the HS256 session token. The `ExamSession` row does not copy the policy's `extra_rules` JSONB — they're orthogonal. The launch is a single atomic transaction: the service does not commit; the route commits after `process_launch` returns, so a failure between "participant upserted" and "exam session created" rolls back the whole launch. FastAPI router: `GET /lti/login` validates `target_link_uri` matches the registered launch URL, generates a 32-byte URL-safe `state`/`nonce`, registers them in the `LaunchStateStore`, and 302-redirects to the platform's authorization endpoint with the OIDC third-party-initiated-login query string; `POST /lti/launch` decodes the JWT, fetches OIDC discovery + JWKS, verifies the signature + standard claims (`iss`, `aud`, `exp`, `iat`, `sub`, `nonce`), parses the `LtiIdToken` (strips the OIDC `state` claim first — it's not an LTI claim), maps the role URIs, peeks the state to validate `iss` against the state-registered issuer (rejects cross-issuer state-reuse *before* the discovery fetch), consumes the state/nonce, and dispatches to `process_launch`. A closed error-code enumeration (`signature_invalid`, `claims_invalid`, `policy_not_found`, `state_unknown`, `state_expired`, `nonce_mismatch`, `audience_invalid`, `issuer_invalid`, `discovery_error`) maps each failure to a deterministic HTTP status. `LaunchStateStore.consume` now returns a `(redirect_uri, lti_issuer)` tuple; a new `LaunchStateStore.peek` lets the route validate the `iss` claim without consuming. `pyproject.toml` adds `python-multipart>=0.0.9` for form parsing. New OIDC test double at `tests/integration/oidc_test_double.py` generates an RSA keypair, builds the discovery + JWKS payloads, and signs launch JWTs; the unit and integration tests share it. 17 new route tests + 14 service tests + 9 PostgreSQL integration tests (134 unit + 9 integration tests total) all pass. `docs/DEPLOYMENT.md` adds `EXAM_CLIENT_URL` and `ADMIN_SURFACE_URL` to the secrets model. Next layer: authenticated WebSocket protocol. | Claude |
| 2026-07-23 | **Preprocessing layer landed (turn N+3).** New `src/proctoring_engine/preprocessing/` package with four submodules: `frames.py` (JPEG/PNG/WebP decode via `cv2.imdecode`, BGR→RGB normalisation for MediaPipe, pass-through for YOLOv8, import-time OpenCV sanity check), `audio.py` (PCM-16 LE/BE base64 decode to int16 numpy, encoding-alias tolerance, linear-interpolation resampler for VAD-rate mismatch, VAD-frame splitting with zero-pad tail, RMS dBFS calculation, end-to-end `preprocess_audio_chunk` pipeline), `scheduler.py` (stateless modality scheduler: configurable every-Nth-frame cadence for head_pose_gaze / object_detection / identity_match), `rolling_buffer.py` (server-side rolling-buffer contract: `RollingBufferConfig` with spec-range validation, `RollingBufferEntry` with encoding/size validation, `RollingBuffer` runtime_checkable Protocol, `InMemoryRollingBuffer` with eviction-at-capacity, `NullRollingBuffer` no-op, `_approx_decoded_size` helper). Fixed a defect in `scheduler.py`: the `_Default` slotted-dataclass's class-level attributes are descriptors, not int values — replaced with plain `Final[int]` module constants. `pyproject.toml` adds `numpy>=1.26,<3`, `opencv-python-headless>=4.10,<5`, `Pillow>=10.4,<12`. 127 new unit tests pass (357 total). Next layer: inference modules. | Claude |
| 2026-07-24 | **Spec alignment pass (library corrections + scale escalation).** User edited `SKILLS_ALIGNMENT.md` §8, `docs/04-inference-modules-design.md`, `docs/proctoring-engine-v1-spec.md`, `docs/DEPLOYMENT.md`, and `SYSTEM_STATE.md` to correct library availability (MediaPipe Tasks API replaces `mp.solutions`; `webrtcvad-wheels` replaces `webrtcvad`) and lock scale target as "thousands of concurrent sessions." Two deployment escalations surfaced as **open decisions** per SKILLS §7: (1) WebSocket affinity/gateway architecture needed before ~10 replicas; (2) Redis-backed `LaunchStateStore` needed before multi-replica deployment. `CONTEXT.md` §9 updated to reflect actual next layer (inference modules). No code changes required — preprocessing layer uses no ML libraries; WebSocket/LTI layers are already marked complete. 357 unit tests verified passing. | Claude |
| 2026-07-24 | **Inference modules layer landed (turn N+4).** New `src/proctoring_engine/inference/` package with seven modules: `_types.py` (`ConfidenceInterval` triple validated in `[0,1]` with `lower≤score≤upper`, `BoundingBox` validated in `[0,1]`, `InferenceResult` base + six modality-specific subclasses); `face_presence.py` (MediaPipe `FaceDetector` runner, `MP_FACE_DETECTOR_BUNDLE` env var, no-face / one-face / second-person event types); `identity_match.py` (`IdentityBackend` ABC + `FaceRecognitionBackend` dlib 128-d, `compute_cosine_similarity` pure numpy, runner takes threshold as argument — never hardcoded); `head_pose_gaze.py` (`FaceLandmarker` runner, `MP_FACE_LANDMARKER_BUNDLE` env var, `compute_ear` / `compute_iris_offset` / `compute_head_pose` pure helpers using landmark indices 1/152/33/263/61/291 for solvePnP, EAR < blink-threshold suppresses off-screen); `object_detection.py` (YOLOv8, `YOLO_WEIGHTS_PATH` env var, `COCO_DENYLIST_IDS = {62: tv, 63: laptop, 67: cell phone, 73: book}`, `filter_denylist_detections` is a pure function unit-testable with mock boxes); `audio_vad.py` (`webrtcvad-wheels` `Vad` with aggressiveness 0–3 boundary, `AudioVadRunner.run` returns `silence` / `speech_detected` / `elevated_rms` based on VAD ratio + RMS dBFS vs `noise_floor_dbfs`); `browser_events.py` (deterministic passthrough, `confidence: (1.0, 1.0, 1.0)`, validates against `VALID_BROWSER_EVENTS` from the WebSocket client envelope layer). All runners are stateless per-frame classifiers; multi-frame confidence intervals are explicitly the fusion engine's job. 79 new unit tests pass on SQLite (436 total). `pyproject.toml` adds `webrtcvad-wheels>=2.0,<3` and `face-recognition>=1.3,<2` (Linux k8s; Windows tests `importorskip`-gated). Three open decisions resolved (identity-match library, MediaPipe bundle, YOLO weights); two deployment escalations remain open. Next layer: fusion & flagging engine (`docs/05-fusion-flagging-engine-design.md`). | Claude |
| 2026-07-25 | **Fusion & flagging engine landed (turn N+5).** New `src/proctoring_engine/fusion/` package with four modules: `_types.py` (`GazeAwayEvent` Stage-2 working-state aggregate, `FlagDecision` frozen output carrying `rule_code` / `severity` / `confidence` / `triggered_termination` / `suppressed_by_exemption_id` / `contributing_event_ids` / `score_delta`); `aggregator.py` (`PolicySnapshot` frozen dataclass holding every `PolicyConfig` threshold + per-rule weights, `SessionContext` denormalised session metadata + pre-loaded `ExemptionRecord`s, `SessionAggregator` per-session stateful engine with `process_face_presence` / `process_gaze` / `process_object_detection` / `process_browser_event` methods emitting `list[FlagDecision]`); `exemptions.py` (`ExemptionRecord` dataclass + pure `find_matching_exemption` lookup with `effective_at`/`expires_at` window checks + `SUPPRESSED_SEVERITY = "low"`); `book_severity.py` (pure `should_flag_book` resolution across `CLOSED_BOOK` / `OPEN_BOOK` / `SPECIFIC_LIST` policies). All three termination paths implemented in one class: Path 1 (consecutive-frame confirmation counter, noise-filter not leniency), Path 2 (consecutive off-screen frames → `GazeAwayEvent` only when `>= gaze_min_duration_ms`, rolling-window counter in `gaze_window_seconds`, warning at `gaze_warning_limit` → MEDIUM, termination at `gaze_termination_limit` → CRITICAL), Path 3 (`accumulated_medium_score` with per-rule weights from `PolicyConfig.score_weights`, threshold from `ck_policy_medium_score_threshold_nonnegative`, `0` is the documented disable sentinel). Accommodation exemption suppression: `find_matching_exemption` then downgrade to `LOW` severity + set `suppressed_by_exemption_id` — never silent drop. Book detection always logged; severity decided by `reference_material_policy` + `permitted_material_details`. Three open decisions resolved this turn: (1) **accumulated-score path is wanted** — single running accumulator across all MEDIUM flags, weights in `PolicyConfig`, threshold pre-exam; (2) **resume / reinstatement explicitly out of v1** — engine is a proctoring sidecar, LMS handles attempt lifecycle through its own tools (Canvas "Moderate Quiz", etc.); (3) **browser client capture architecture** — LTI launch opens capture client as active browser tab, LMS quiz in iframe; extension and companion-window approaches rejected. 68 new unit tests pass on SQLite (504 total). Test count: 504 unit + 16 PostgreSQL integration. Next layer: evidence & audit store (`docs/06-evidence-audit-store-design.md`). | Claude |
