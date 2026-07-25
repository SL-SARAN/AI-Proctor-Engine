# CLAUDE

Navigation aid for new Claude sessions on the AI Proctoring Engine.
Read this first, then read the four cross-cutting files it points to,
then read the design doc for the layer you are about to touch.

---

## Read in this order, every session

1. **`SYSTEM_STATE.md`** — what is built, what is verified, what is
   open, and the System State Status checklist at the end. The
   checklist is the single source of truth for "what's next."
2. **`SKILLS_ALIGNMENT.md`** — the role contract, the ReAct loop,
   the guardrails (zero-hallucination, no placeholders, objectivity,
   explainability, test-driven, asynchronous non-blocking, halting
   clause), the incremental-lifecycle rule, and the open-decision
   policy.
3. **`ARCHITECTURE.md`** — the project flow, the layer-by-layer
   sequence, the end-to-end data flow per session, the cross-cutting
   invariants, the deployment topology, and the iteration log.
4. **`CONTEXT.md`** — the cross-session mental model: locked
   decisions, open decisions, the file map, the cross-cutting rules,
   and the shape of the next session.

Then read the layer-specific design doc in `docs/0X-…-design.md` and
the locked spec at `docs/proctoring-engine-v1-spec.md` before
touching code.

---

## The hard rules (do not violate)

- One atomic system layer per turn. The next layer is the first
  unchecked item on the `SYSTEM_STATE.md` §12 checklist.
- No placeholders, no truncation, no `TODO`, no `…`, no `pass  #
  implement`. Files are complete end-to-end or the turn halts.
- No invented APIs, methods, libraries, configurations, or numbers.
  Cite a verified source (`docs/`, the actual source tree,
  `pyproject.toml`) or halt and ask.
- Every flag must carry a structural proof path: contributing
  `TelemetryEvent` IDs, `confidence_interval`, bounding box where
  applicable, immutable audit record.
- `Flag` and `TerminationRecord` rows are append-only. `ProctorReview`
  is the mechanism for corrections — it sits alongside, never
  replaces. Both the ORM listener and the PostgreSQL trigger enforce
  this.
- `ExamSession.consent_recorded_at` must be set before any telemetry
  / evidence is persisted. Enforce at the ingestion layer.
- The client rolling buffer is **never** transmitted during normal
  operation. Flush only on a confirmed flag.
- The internal `/sessions/{id}/terminate` route uses a distinct
  internal service-to-service credential
  (`INTERNAL_TERMINATE_TOKEN`), not an LTI-derived student or
  instructor token. This is enforced in tests.
- Thresholds live in `PolicyConfig`, not in conditionals.
- Schema changes are verified by both unit (SQLite) and integration
  (PostgreSQL) tests in the same turn. The immutability triggers
  and the new enum values added by
  `20260718_0002_audit_reconciliation.py` are exactly the kind of
  invariants SQLite cannot model.
- The two remaining open decisions (accumulated-score path,
  embedding storage) are **not** to be silently resolved. Surface
  them and ask.

---

## What is built today

- Python 3.11+ project structure (`pyproject.toml`, package layout).
- PostgreSQL ORM schema for all 11 entities (`src/proctoring_engine/models.py`),
  including `AdminUser` for structured admin/proctor/instructor identity.
- Data integrity constraints (confidence `[0,1]`, timestamp ordering,
  retention, FK, uniqueness, policy gaze ordering, accumulated-score
  non-negative, embedding validation, `Flag` confidence interval
  containment).
- Audit relations (`FlagTelemetryEvent`, `EvidenceArtifact`,
  `TerminationRecord`, `ProctorReview`).
- `Flag` and `TerminationRecord` immutability — both at the ORM
  event listener and at PostgreSQL triggers.
- Two Alembic migrations: the initial revision
  (`20260717_0001_initial_proctoring_schema.py`) and the audit
  reconciliation (`20260718_0002_audit_reconciliation.py`). The
  initial migration creates the entire current schema via
  `Base.metadata.create_all`; the audit-reconciliation migration
  installs only the `flag_immutable` PostgreSQL trigger
  (the schema-shape operations it once carried are emitted by the
  initial migration). `tests/test_migration_chain.py` enforces the
  invariant that no post-initial migration may re-add an enum value,
  column, table, index, or constraint that the initial migration
  already emits — the only legitimate content of a post-initial
  migration is a DML trigger.
- Admin identity resolution: `PolicyConfig.created_by_id`,
  `AccommodationExemption.approved_by_admin_id`, and
  `ProctorReview.reviewer_admin_id` are FK-backed columns pointing
  to `admin_users`. Original string fields are preserved for
  backward compatibility.
- 35 unit boundary / integrity tests passing on SQLite
  (`tests/test_models.py`).
- 16 PostgreSQL integration tests passing on the real engine
  (`tests/integration/test_postgres_immutability.py`).
- GitHub Actions CI: unit + integration (with `postgres:15-alpine`
  service container) + Docker build smoke test
  (`.github/workflows/ci.yml`).
- Docker image and `docker-compose.yml` for local dev.
- Kubernetes manifest set for production deployment
  (`k8s/00-…` through `k8s/10-…`).
- `docs/DEPLOYMENT.md` documenting the deployment topology, sizing,
  secrets model, and scaling story.
- Design doc suite (`docs/00`–`08`, plus the spec and the handoff) —
  `docs/` is now committed to git.
- FastAPI app shell with `/healthz` only.
- LTI 1.3 launch + session creation + consent capture (turns N + N+1):
  `LtiSettings`, claim parsing, role mapping, the in-memory
  `LaunchStateStore`, the HS256 session token, OIDC discovery, JWKS
  fetchers, the `process_launch` service, the FastAPI router
  (`GET /lti/login`, `POST /lti/launch`), and the OIDC test double.
  134 unit tests pass on SQLite; 9 PostgreSQL integration tests cover
  the JSONB-column preservation, the `consent_recorded_at` /
  `started_at` invariants, and the upsert semantics.
- Authenticated WebSocket protocol (turn N+2): envelope types,
  discriminated-union dispatch, sparse-frame ingestion, kill-switch
  delivery with ack/retry grace, telemetry event buffer, FastAPI
  `/ws` endpoint. 88 unit tests pass.
- Preprocessing layer (turn N+3): frame decode (`cv2.imdecode` +
  BGR→RGB for MediaPipe, pass-through for YOLOv8), audio decode
  (PCM-16 LE/BE → int16 numpy, resample, VAD-frame split, RMS
  dBFS), stateless modality scheduler, server-side rolling-buffer
  contract. 127 unit tests pass. Dependencies:
  `opencv-python-headless`, `numpy`, `Pillow`.

## What is **not** built today (the unchecked items on the checklist)

- Inference modules (6 modalities).
- Fusion & flagging engine (3 paths + exemption suppression).
- Evidence store (S3-compatible adapter, checksums, retention
  deletion job).
- API & orchestration (full route surface, state machine,
  authorization).
- Browser client + capture.
- Live cluster provisioning + end-to-end smoke test on a real
  Kubernetes cluster.

## Sandbox constraints (not the same as production constraints)

- No PostgreSQL server in this workspace — but the integration test
  suite is designed to run against one. The CI workflow's
  `integration` job is the source of truth; the local developer
  runs the same suite against a local Postgres or a
  `docker compose up postgres` instance.
- MediaPipe Tasks API is not used (its model bundle host is outside
  the sandbox allowlist). The locked design uses `mp.solutions`
  instead.
- `webrtcvad`, YOLOv8 (Ultralytics), and the older MediaPipe
  `mp.solutions` weights are all verifiable here.
- `pyannote.audio` and the full multi-speaker diarization path are
  **explicitly out of v1**, not just sandbox-deferred.

## How a turn is structured

1. **THOUGHT** — what the user asked, what the locked spec requires,
   what the boundary cases are.
2. **ACTION** — the architectural pattern, the files to emit, the
   tests to supply.
3. **OBSERVATION** — self-audit against `SKILLS_ALIGNMENT.md` §3.
4. **Output** — the code, fully realized, plus the tests, plus the
   System State Status update, plus the next-layer pointer.
5. **Halting clause** — if any step fails the guardrail audit, the
   turn halts and emits a structured markdown table of the choices
   needed to proceed.

If a turn has to halt, do not start writing code. State the
ambiguity precisely and ask.

## At the end of every turn

- Update `SYSTEM_STATE.md` §12 to mark the atomic layer complete and
  to identify the next one.
- If architecture changed, update `ARCHITECTURE.md` in the same turn
  (and add an entry to its iteration log).
- If an open decision was resolved, update `CONTEXT.md` §4, the
  relevant design doc, and `ARCHITECTURE.md`.
- If a new open decision was surfaced, surface it in the turn's
  output — do not silently resolve.

---

The four cross-cutting files (this one, `SYSTEM_STATE.md`,
`SKILLS_ALIGNMENT.md`, `ARCHITECTURE.md`, `CONTEXT.md`) are the
single source of truth for "where the project is and how to work on
it." The design docs in `docs/0X-…-design.md` are the single source
of truth for each layer's contract. The locked spec at
`docs/proctoring-engine-v1-spec.md` is the source of truth for
requirements. `docs/DEPLOYMENT.md` is the source of truth for the
deployment topology, sizing, and secrets model.

The next atomic layer is the **inference modules** — the six
server-side modalities (face presence, identity match, head
pose / gaze, object detection, audio VAD, browser events) that
consume the decoded, normalised output of the preprocessing
layer.  See `docs/04-inference-modules-design.md`.
