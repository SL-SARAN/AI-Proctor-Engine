# Claude Handoff

## What is present

The repository contains a clean Python 3.11+ package for the **data-model stage** of the AI Proctoring Engine v1 specification. Start with these files:

- `src/proctoring_engine/models.py` - ORM entities, enums, validation, constraints, relationships, and ORM-level append-only termination records.
- `migrations/versions/20260717_0001_initial_proctoring_schema.py` - first versioned schema revision; PostgreSQL trigger for immutable terminations.
- `tests/test_models.py` - contract tests for the boundary values required by the specification.
- `docs/COMPLETION_STATUS.md` - precise completion boundaries.
- `docs/KNOWN_ISSUES.md` - encountered environment errors, production risks, and intentionally deferred work.
- `docs/VERIFICATION_LOG.md` - exact verification expectations and results.

## First actions for the next agent

1. Read the original `proctoring-engine-v1-spec.md` alongside `models.py`; this code implements its explicit next data-model layer, not every architectural layer described in the document.
2. Install `.[dev]`, run `pytest`, and replace the pending verification entry with exact pass/fail output.
3. Run `alembic upgrade head` against a disposable PostgreSQL 15+ database and inspect the generated schema, especially enum types and `termination_record_immutable`.
4. Do not weaken or remove `PolicyConfig`, evidence retention metadata, `FlagTelemetryEvent`, or termination immutability to make later API work easier. They encode locked v1 audit requirements.

## Recommended implementation order after schema verification

1. Add Alembic/SQLAlchemy integration tests against PostgreSQL in CI.
2. Build authenticated LTI 1.3 launch/session creation with consent capture and a policy snapshot.
3. Design the authenticated WebSocket event schema, sparse-frame protocol, evidence-buffer upload, and kill-switch acknowledgement flow.
4. Add object-storage abstraction with checksums, encryption metadata, retention deletion worker, and test doubles.
5. Add the async inference job queue and define versioned telemetry payload contracts for each modality.
6. Add the browser client and client-side event capture. Only then connect face/gaze/object/audio models.
7. Add proctor/admin review endpoints, authorization, audit exports, operational metrics, and privacy/security review.

## Important constraints

- Do not treat webcam gaze heuristics or COCO `tv` detections as calibrated truth. The supplied spec explicitly requires real-session validation.
- Do not enable face-recognition embeddings in production until model source, consent, encryption, threshold calibration, and retention handling are finalized.
- Never persist the normal rolling browser-frame buffer; persist it only after a flag, as an `EvidenceArtifact`.
- Keep client lightweight checks result-only under normal conditions. Sparse server frames and evidence uploads are different paths.

