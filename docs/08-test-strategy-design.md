# Test strategy — design doc

What actually needs testing per layer, with a focus on boundary values
— since most of the interesting bugs in a system like this live at the
edges of a threshold, not in the average case.

## Data models

- Confidence score boundaries: exactly `0.0`, exactly `1.0`, and just
  outside the valid range (`-0.01`, `1.01`) should be rejected at the
  constraint level, not just by convention.
- Timestamp ordering: `TelemetryEvent.occurred_at` after `received_at`
  (clock skew or a malformed client timestamp) shouldn't silently
  pass — this is exactly the kind of thing that quietly corrupts an
  audit trail if unguarded.
- Foreign key integrity: a `Flag` referencing a `TelemetryEvent` that
  belongs to a *different* `ExamSession` than the flag itself —
  should be constrained at the schema level, not just assumed
  correct by application logic.
- `PolicyConfig` versioning: two versions with overlapping
  `effective_from` / `effective_until` ranges for the same institution
  — should this be possible? Worth deciding and then testing against
  that decision, not leaving ambiguous.
- `Flag` confidence interval containment:
  `confidence_lower <= confidence_score <= confidence_upper` must
  hold for every row.
- `EvidenceArtifact.flag_id` uniqueness: a second artifact on the
  same flag must be rejected at the SQL level (the v1 invariant).
- `PolicyConfig.gaze_min_duration_ms` must not exceed
  `gaze_window_seconds * 1000` (a 30s minimum against a 5s window is
  meaningless).
- `ExamSession.accumulated_medium_score` must be non-negative.

## Ingestion layer

- LTI JWT validation: expired token, wrong signature, missing
  required claims, mismatched `nonce` — each should fail closed, not
  fail open.
- WebSocket reconnect: a client reconnecting with a valid session
  token for an already-`terminated` session should be rejected, not
  silently resume.
- Malformed envelope: a `telemetry_heavy_frame` message missing the
  `frame` field, or with an unrecognized `type` value — should be
  rejected without crashing the connection for the rest of the
  session.

## Preprocessing layer

- `webrtcvad` boundary: an audio chunk at an *unsupported* sample
  rate (e.g. 44100 Hz) — should fail the resampling step loudly, not
  silently pass a valid-looking VAD call with garbage internal
  behavior.
- Rolling buffer boundary: a flag firing at the exact moment the
  buffer is still filling (e.g. 2 seconds into a session, before the
  full 10–15 second buffer exists) — the flush should send whatever's
  actually buffered, not fail or send an empty clip.

## Inference modules

- Face count boundary: exactly 1 face vs. exactly 2 faces vs. 0 faces
  — three distinct code paths, each needs its own test, not just
  "not 1."
- Gaze EAR boundary: a frame at exactly the blink-vs-open threshold
  — should be deterministic, not flip-flop between runs on the same
  input.
- Identity match: a similarity score exactly at the configured
  threshold — needs an explicit decision on which side of the
  boundary counts as a match, then a test locking that in.
- Object detection: a detection at exactly the confidence threshold
  for denylisting — same boundary-inclusion question as above.

## Fusion & flagging engine

- Second-person confirmation window: exactly 2 consecutive confirming
  frames vs. exactly 1 (should not fire) vs. exactly 3 (definitely
  should).
- Gaze-away escalation: `GazeAwayEvent` count exactly at
  `gaze_warning_limit` and exactly at `gaze_termination_limit` —
  off-by-one errors here are the most likely bug class in this whole
  system, since the spec's escalation ladder is entirely
  boundary-driven.
- Accumulated-score path: score exactly at
  `medium_score_termination_threshold` — same off-by-one risk.
- Exemption suppression: a `Flag` for an object class with a matching
  `AccommodationExemption` scoped to a *different* `exam_session_id`
  than the one in progress — should not suppress, since the
  exemption doesn't apply here.

## Evidence & audit store

- Retention boundary: an `EvidenceArtifact` with
  `retention_expires_at` exactly equal to the deletion job's current
  run time — needs an explicit decision on whether "exactly now"
  counts as expired, then a test for it.
- Flush-order integrity: simulate the object-storage write succeeding
  but the subsequent DB insert failing — the blob should end up
  orphaned (acceptable) rather than a DB row pointing at a
  nonexistent blob (not acceptable).
- Immutability: an attempted `UPDATE` on a `TerminationRecord` or
  `Flag` row should fail at the database level, not just be
  discouraged at the application level — test this directly against
  the DB, not just through the ORM. The integration suite in
  `tests/integration/` does this.

## API / orchestration

- Session state transitions: every transition not explicitly listed
  in the state machine (e.g. `completed → active`) should be
  rejected.
- Authorization: a learner-role LTI launch attempting to hit an
  `/admin/*` route should be rejected, tested with an actual
  learner-role token, not mocked away.
- The internal `/sessions/{id}/terminate` credential: a request using
  a student or instructor LTI token (rather than the internal
  service credential) should be rejected — this route should never
  be reachable via ordinary user authentication.

## Integration scenarios (full-flow, not unit-level)

- **End-to-end zero-tolerance termination:** simulate a second face
  appearing for the confirmation window, confirm a `CRITICAL` `Flag`
  is raised, the kill-switch message is sent, the client's
  (simulated) buffer flush produces an `EvidenceArtifact`, and the
  `TerminationRecord` is created with the correct `triggering_flag_id`.
- **Gaze-away escalation over simulated time:** feed a sequence of
  `GazeAwayEvent`s timed to cross `gaze_warning_limit` first and
  `gaze_termination_limit` later within the same rolling window,
  confirm the `MEDIUM` flag fires first and the `CRITICAL` flag fires
  only once the second threshold is actually crossed, not before.
- **Accommodation exemption suppressing a flag correctly:** once
  earbud / smartwatch detection exists (v2), an approved exemption
  should downgrade the flag and set `suppressed_by_exemption_id`,
  while the underlying detection event still appears in the audit
  log.
- **`PolicyConfig` version change mid-session:** a session that
  started under one policy version shouldn't have its behavior
  silently altered by a new version published while it's still
  active — confirms the versioning / snapshot design from the
  data-models doc actually holds under a live change, not just in
  the schema.

## Integration suite (PostgreSQL)

The test files under `tests/integration/` are skipped when
`INTEGRATION_DATABASE_URL` is not set, so the SQLite unit suite runs
unchanged. When the env var is set (the GitHub Actions
`integration` job, or a developer with a local Postgres), the
integration suite:

- Applies the full Alembic migration chain against a real PostgreSQL
  15+ database.
- Verifies the `session_status` enum contains `pending`, `active`,
  `completed`, `terminated`, `under_review` — and does **not**
  contain the legacy `created` or `cancelled` values.
- Verifies the `flag_status` enum contains `overturned`.
- Verifies the `review_decision` enum contains `needs_more_info`.
- Exercises the `flag_immutable` and `termination_record_immutable`
  triggers under direct `UPDATE` and `DELETE` SQL — the database
  raises an exception, not the ORM.
- Enforces the new `gaze_min_duration_within_window` check
  constraint.
- Enforces the new `one_evidence_artifact_per_flag` unique
  constraint.
- Round-trips the new `accumulated_medium_score`,
  `embedding_model_version`, and `triggered_termination` columns.
- Verifies the FK `flags.policy_config_id` is enforced at the SQL
  level.
- Verifies that `flags.policy_config_id`'s `ON DELETE RESTRICT`
  blocks the cascade when a termination record depends on the flag.

The integration test module is the gate that proves the
audit-reconciliation migration is correct. The CI workflow's
`integration` job is mandatory; passing it is required to merge.
