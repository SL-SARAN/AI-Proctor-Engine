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
| 10 | `AdminUser` table | **Next** | `docs/01` open decision | — |
| 11 | LTI 1.3 launch + session creation + consent capture | Pending | `docs/02-ingestion-layer-design.md` §1 | — |
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
  detection, VAD, audio ingestion, gaze aggregation, or async worker
  queue.
- No S3-compatible evidence storage adapter (R2 in production,
  MinIO locally), envelope encryption / KMS, lifecycle rule,
  background retention purge, or artifact upload verification.
- No reviewer / admin API or authorization model (beyond the
  LTI-roles-derived split and the `INTERNAL_TERMINATE_TOKEN`).
- No `AdminUser` table — the admin-identity open decision is the
  next atomic layer.
- No calibration data or false-positive evaluation for gaze,
  identity, face count, or monitor detection. Default thresholds in
  `PolicyConfig` are **not validated** as final.

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

**`AdminUser` table + admin-identity resolution.** The current schema
stores `AccommodationExemption.approved_by`,
`PolicyConfig.created_by`, and `ProctorReview.reviewer_reference` as
free-form strings. The next layer adds an `AdminUser` table with
`(lti_issuer, lms_user_reference, role, created_at, retired_at)`,
migrates the three string references to FKs, and adds a test that the
admin route authorization (a future layer) can rely on the FK
constraint. This closes the open decision called out in
`docs/01-data-models-design.md` and unblocks the LTI 1.3 launch
implementation, which needs an admin identity to attribute the
`created_by` field on `PolicyConfig`.

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
