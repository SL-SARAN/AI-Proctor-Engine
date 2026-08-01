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

## Path 3: accumulated-score termination — resolved

**Resolved: this path is wanted.** Single running accumulator across
all MEDIUM-severity signals (not a separate counter per modality) —
`ExamSession.accumulated_medium_score`. Each `MEDIUM` `Flag` adds a
weighted increment (weight per `flag_type`, configurable in
`PolicyConfig`). `PolicyConfig.medium_score_termination_threshold` is
set by whoever holds the proctor/admin role for that policy, ahead of
the exam, as part of the versioned snapshot — not adjustable
mid-session.

**What happens on crossing the threshold — `PolicyConfig.medium_score_action`:**
an admin-configured default, `auto_terminate` or `flag_for_review`.
On crossing, the configured default fires **immediately** through the
existing kill-switch mechanism — no new "pending termination" hold
state. A live proctor can fast-track an **undo** via the existing
`ProctorReview` overturn path (a new decision consequence: reinstate a
session, not a new mechanism). `ExamSession.status` gains `reinstated`
for this — deliberately not just reset to `active`, so a session with
an unusual lifecycle is readable from its own status column.

> **Correction — this overrides a conflicting decision made
> independently at turn N+5.** That turn recorded "Resume/reinstatement:
> explicitly out of v1. Termination is final from the engine's
> perspective; the LMS handles attempt lifecycle through its own
> tools" (`SYSTEM_STATE.md` §2). That conclusion was reached without
> visibility into the fuller design worked through separately: the
> live-proctor-override requirement specifically needs an undo
> mechanism to fast-track, or "act immediately per default" has no
> undo to fast-track at all. Explicitly confirmed to stand as the
> resolution: **the "termination is final" call is overridden for
> this specific path.** It does *not* touch the separate,
> genuinely-still-open question of whether the *LMS's own* attempt
> lifecycle (grades, reopening a native-quiz attempt) can be resumed
> — that depends on the still-unresolved own-client-vs.-embedded-quiz
> architectural question, and is unaffected by this engine-side
> reinstatement.
>
> `TerminationRecord` gains no new column for this — "was this
> reinstated" stays a derived fact (a `ProctorReview` with
> `decision=overturned` against the triggering flag exists), per the
> already-locked append-only rule.

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
