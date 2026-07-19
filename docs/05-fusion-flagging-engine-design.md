# Fusion & flagging engine — design doc

This is the layer that turns `TelemetryEvent`s into `Flag`s, and decides when a flag becomes a termination. It's the one place in the system where all the modality-specific rules from the spec actually get enforced together, since a real session can trigger more than one signal at once.

---

## Core structure: a per-session aggregator

One logical aggregator instance per active `ExamSession`, consuming `TelemetryEvent`s as they arrive from the inference modules and holding whatever short-lived state each rule needs (the gaze rolling-window counter, the second-person confirmation-frame counter, the accumulated medium score). This state doesn't need to live in Postgres in real time — it's working state for an active session, only the *outcomes* (confirmed `Flag`s) get persisted. A reasonable home for this is an in-memory or Redis-backed structure keyed by `exam_session_id`, not a database table that gets written to every frame.

---

## Path 1: zero-tolerance (second person)

Straight from the spec, restated as the actual control flow:

1. Face-presence module reports face count ≥ 2.
2. Aggregator checks: has this been true for 2–3 consecutive confirming frames (noise filter, not leniency)?
3. If yes → emit `Flag(severity=CRITICAL, flag_type=second_person, triggered_termination=true)`.
4. Orchestration layer picks this up and fires the kill-switch (see that doc).

No accumulation, no window, no exemption check applies here — the spec was explicit that this is not something a per-participant exemption should ever soften.

---

## Path 2: gaze-away frequency ladder

Also from the spec (§3.1), restated as control flow:

1. Gaze module emits per-frame `off_screen` classifications.
2. Aggregator runs Stage 2: merges consecutive `off_screen` frames into a `GazeAwayEvent` once they persist past `gaze_min_duration_ms` (default 800ms).
3. Aggregator maintains a rolling count of `GazeAwayEvent`s within `gaze_window_seconds` (default 5 min).
4. Below `gaze_warning_limit` → log only, no `Flag`.
5. At/above `gaze_warning_limit` → emit `Flag(severity=MEDIUM, flag_type=gaze_away_frequency)`, and increment `ExamSession.accumulated_medium_score` (see Path 3).
6. At/above `gaze_termination_limit` → emit `Flag(severity=CRITICAL, flag_type=gaze_away_frequency, triggered_termination=true)` → kill-switch.

---

## Path 3: accumulated-score termination — proposed here, not previously confirmed

The spec established that `MEDIUM` signals (tab-blur, gaze-warning-level) "accumulate a score rather than auto-terminating on their own" — but never specified what happens once that score gets large. Leaving it as pure accumulation with no ceiling means a student who trips a lot of low-level signals over a long exam can never reach termination through that path, no matter how many `MEDIUM` flags stack up, which seems unlikely to be the actual intent.

**Proposed mechanism:** each `MEDIUM` `Flag` adds a weighted increment to `ExamSession.accumulated_medium_score` (weight per `flag_type`, configurable in `PolicyConfig` — a tab-blur might weigh less than a gaze-warning, for instance). If that running total crosses `PolicyConfig.medium_score_termination_threshold`, the aggregator emits a `Flag(severity=CRITICAL, flag_type=accumulated_score, triggered_termination=true)` — same kill-switch flow as the other two paths.

This is a genuinely new design decision introduced while writing this doc, not something you'd confirmed earlier — flagging it explicitly rather than treating it as settled. Worth deciding: do you want this third termination path at all, or should `MEDIUM` signals stay purely advisory for human review with no automatic ceiling?

---

## Exemption suppression (applies before any of the three paths finalize a flag)

Before a `Flag` involving an object class is written, the aggregator checks `AccommodationExemption` for a matching `participant_id` + `object_class` (and, if scoped, matching `exam_session_id`). If found: either suppress the flag entirely or downgrade its severity (implementation choice — suppressing loses the audit trail of the detection ever happening, downgrading keeps it logged but non-escalating; the spec's object-detection section already says the detection event should always be logged regardless, which argues for downgrade-and-log over silent suppression). Set `Flag.suppressed_by_exemption_id` either way, so the record shows an exemption was checked and applied, not that detection simply failed to fire.

This currently has no live effect in v1, since earbuds/smartwatches aren't detected at all yet — but the logic is designed in now so it's a config change, not a rewrite, the moment that gap gets revisited.

---

## Book-detection severity check (object detection path)

Object detection always logs a book detection regardless of exam type (per the inference-modules doc). The severity decision happens here: the aggregator checks `ExamSession.allowed_reference_materials` — `closed_book` escalates to a `Flag`; `open_book` or `specific_list` (if the detected item matches) does not escalate, but the underlying `TelemetryEvent` remains in the audit trail either way.

---

## What the aggregator explicitly does not decide

It doesn't decide *how* the kill-switch gets delivered (WebSocket message format, LTI callback) — that's the orchestration layer's job, triggered by `Flag.triggered_termination = true`. Keeping "decide severity" and "act on severity" as separate layers is what lets the termination-vs-flag-for-review behavior stay a `PolicyConfig` change rather than a code change, per the spec's original configurability requirement.
