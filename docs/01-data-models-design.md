# Data models — design doc

Full field-level design for the 10 entities named in the spec, plus the
relationships between them. This is the layer the actual DDL/ORM code is
written against. The reconciliation between this document and the code
landed in migration `20260718_0002`; the two are now in sync.

> **Reconciliation note (2026-07-18).** The audit-reconciliation
> migration `20260718_0002` brought the schema in line with this doc
> on the following points: `SessionStatus` was renamed `created` →
> `pending` and `cancelled` was dropped in favor of `under_review`
> to match the state machine in `docs/07-api-orchestration-design.md`
> §2; `Flag.triggered_termination`, `Flag.suppressed_by_exemption_id`,
> `ExamSession.accumulated_medium_score`, and
> `EnrollmentReference.embedding_model_version` were added per the
> spec; `EvidenceArtifact.flag_id` gained a unique constraint to
> enforce the v1 "one primary artifact per flag" model; the
> `flag_immutable` trigger was added to mirror the existing
> `termination_record_immutable` trigger.

---

## Entity list and fields

### Participant
| Field | Type | Notes |
|---|---|---|
| `id` | UUID, PK | |
| `lti_issuer` | string | The LTI platform issuer; scopes the LMS user reference. |
| `lms_user_reference` | string | The LTI-provided user identifier, unique per `(lti_issuer, lms_user_reference)` pair. |
| `display_name` | string | |
| `consent_recorded_at` | nullable timestamp | Compliance field — no telemetry may be persisted before this is set. |
| `consent_notice_version` | nullable string | The version of the consent notice the participant was shown. |
| `created_at` | timestamp | |

### EnrollmentReference
| Field | Type | Notes |
|---|---|---|
| `id` | UUID, PK | |
| `participant_id` | FK → Participant | |
| `storage_uri` | string | Object storage pointer to the enrollment photo itself. |
| `content_sha256` | string | Integrity check on the enrollment photo bytes. |
| `embedding_vector` | see open decision below | |
| `embedding_dimensions` | integer | Required so the identity module can sanity-check the vector before comparing. |
| `model_name` | string | e.g. `facenet-20180402-102059`. |
| `embedding_model_version` | string | Tracks the *version* of the embedding model — needed because a model upgrade invalidates old embeddings and forces re-enrollment. Pairs with `model_name` to form the (model, version) key the identity-match module uses. |
| `enrolled_at`, `revoked_at` | timestamp | `revoked_at` is set when the enrollment is retired (a re-enrollment supersedes it). |

**Open decision — embedding storage.** The current implementation
stores the embedding as a JSONB float array (option b). The alternative
is the `pgvector` extension. Re-evaluate when a "search across many
embeddings" use case appears.

### ExamSession
| Field | Type | Notes |
|---|---|---|
| `id` | UUID, PK | |
| `participant_id` | FK → Participant | |
| `policy_config_id` | FK → PolicyConfig | Which policy *snapshot* governs this session — see the versioning note on `PolicyConfig`. |
| `lti_issuer` | string | Redundant with `Participant.lti_issuer`; denormalized so session-level queries do not need to join. |
| `lti_context_id` | string | Course/context reference from the LTI launch. |
| `lti_resource_link_id` | string | The specific exam/assessment in the LMS. |
| `attempt_reference` | string | Unique per attempt. |
| `status` | enum: `pending`, `active`, `completed`, `terminated`, `under_review`, `reinstated` | See `docs/07` §2 for the state machine. `reinstated` is distinct from re-activating back to `active` — a session with an unusual lifecycle should be readable from its own status column without cross-referencing `ProctorReview`, per the accumulated-score fast-track-undo design in `05-fusion-flagging-engine-design.md`. |
| `allowed_reference_materials` | enum: `closed_book`, `open_book`, `specific_list` | Governs whether a detected book escalates severity. |
| `permitted_material_details` | nullable JSON | Only populated when the above is `specific_list`. |
| `consent_recorded_at` | nullable timestamp | Compliance field — no telemetry may be persisted before this is set. |
| `retention_expires_at` | nullable timestamp | Drives the deletion job in the evidence store. |
| `accumulated_medium_score` | numeric | Running tally for non-zero-tolerance signals — see fusion-engine doc. |
| `started_at`, `ended_at` | nullable timestamp | |
| `created_at` | timestamp | |

### AccommodationExemption
| Field | Type | Notes |
|---|---|---|
| `id` | UUID, PK | |
| `participant_id` | FK → Participant | |
| `exam_reference` | string | The exam this exemption is scoped to (or all exams, in the future). |
| `object_class` | string | e.g. `earbud`, `smartwatch`, `hearing_aid` — forward-looking, since v1 object detection doesn't cover ear/wrist devices yet, but the schema supports it now. |
| `approved_by` | string | Admin reference; the `AdminUser` table lands in the next layer. |
| `approved_at` | timestamp | |
| `approval_reason` | text | Free-text reason for the exemption. |
| `effective_at`, `expires_at` | timestamp | `expires_at` is nullable for standing exemptions. |
| `documentation_ref` | nullable string | A *reference* to supporting documentation, not the document itself — avoid storing medical records directly in this table. |

### TelemetryEvent
| Field | Type | Notes |
|---|---|---|
| `id` | UUID, PK | |
| `exam_session_id` | FK → ExamSession | |
| `modality` | enum: `face`, `gaze`, `identity`, `object`, `audio`, `browser`, `system` | |
| `event_type` | string | Modality-specific event subtype. |
| `occurred_at` | timestamp | Client-side capture time. |
| `received_at` | timestamp | Server receipt time — kept distinct from `occurred_at` for latency/clock-skew auditing. |
| `confidence` | numeric (0–1) | |
| `raw_value` | JSONB | Shape varies per modality. |
| `bounding_boxes` | JSONB | List of bbox dicts, when applicable. |
| `correlation_id` | nullable UUID | For tracing related events across a session. |

**Persistence discipline.** Not every per-frame reading becomes a
`TelemetryEvent` row. Per the design doc, raw per-frame classifications
live transiently (in the fusion engine's rolling window or a short-TTL
cache); only *aggregated, meaningful* readings — a confirmed
`GazeAwayEvent` after Stage 2, a confirmed object detection above
threshold, a confirmed second-face reading — become persistent
`TelemetryEvent` rows. This keeps write volume proportional to actual
events, not camera frame rate.

### Flag
| Field | Type | Notes |
|---|---|---|
| `id` | UUID, PK | |
| `exam_session_id` | FK → ExamSession | |
| `policy_config_id` | FK → PolicyConfig | The policy snapshot the flag was raised under. |
| `rule_code` | string | E.g. `SECOND_FACE_CONFIRMED`, `OBJECT_CELL_PHONE`, `GAZE_TERMINATION`. |
| `severity` | enum: `low`, `medium`, `high`, `critical` | |
| `status` | enum: `raised`, `confirmed`, `dismissed`, `overturned` | Append-only at the row level; "transitions" are represented by a new `ProctorReview` row, not by updating `status`. |
| `confidence_score`, `confidence_lower`, `confidence_upper` | numeric (0–1) | The statistical interval, computed from multiple samples in a window. The DB constraint `ck_flag_confidence_interval_contains_score` enforces that the score is inside the interval. |
| `triggered_termination` | boolean | True iff this flag fired the kill-switch. The orchestration layer reads this, not `severity == CRITICAL`, because policy may set a non-CRITICAL severity as the termination trigger in the future. |
| `suppressed_by_exemption_id` | nullable FK → AccommodationExemption | Set when an exemption downgraded or suppressed this flag — never silently dropped. |
| `detail` | JSONB | Per-rule diagnostic payload. |
| `created_at`, `resolved_at` | timestamp | |

**Immutability.** `Flag` rows are append-only. The
`flag_immutable` PostgreSQL trigger (added in migration
`20260718_0002`) raises an exception on any `UPDATE` or `DELETE`. An
ORM-level mirror lives in `src/proctoring_engine/models.py`. The
correct mechanism for "this flag was wrong" is a `ProctorReview` row
that sits alongside the original flag.

### EvidenceArtifact
| Field | Type | Notes |
|---|---|---|
| `id` | UUID, PK | |
| `flag_id` | FK → Flag, **unique** | One primary artifact per flag in v1. The `uq_evidence_artifacts_one_per_flag` constraint enforces this. |
| `kind` | enum: `frame`, `clip`, `audio`, `event_export` | |
| `storage_uri` | string | Object storage path. |
| `content_sha256` | string | Integrity check. |
| `media_type` | string | MIME type. |
| `byte_size` | integer | For cost / quota tracking. |
| `capture_started_at`, `capture_ended_at` | timestamp | The lead-in context window, per the rolling-buffer design. |
| `retention_expires_at` | timestamp | Inherited from the session's policy but stored independently so the deletion job can act on evidence without joining back to `ExamSession`. |
| `encryption_key_reference` | nullable string | KMS key identifier (or empty if no envelope encryption). |
| `sealed_at`, `created_at` | timestamp | `sealed_at` is set when the artifact is finalized for retention. |

### PolicyConfig
| Field | Type | Notes |
|---|---|---|
| `id` | UUID, PK | |
| `name` | string | E.g. `default_v1`, or an institution-specific variant. |
| `version` | integer | Increments on every "change" — see the versioning note below. |
| `is_active` | boolean | Soft-delete flag; the active row is the default for new sessions. |
| `termination_severity` | enum: `low`, `medium`, `high`, `critical` | The minimum severity that triggers an auto-termination. |
| `terminate_on_second_face` | boolean | Whether the second-person path auto-terminates or only flags. |
| `second_face_confirmation_frames` | integer | The 2–3-frame sensor-noise filter. |
| `gaze_min_duration_ms` | integer | Minimum on-screen-off duration to count as a `GazeAwayEvent`. |
| `gaze_window_seconds` | integer | The rolling window for gaze-away event counting. |
| `gaze_warning_limit` | integer | MEDIUM flag threshold. |
| `gaze_termination_limit` | integer | CRITICAL flag threshold. |
| `medium_score_termination_threshold` | numeric | The accumulated-score path's threshold. |
| `medium_score_action` | enum: `auto_terminate`, `flag_for_review` | Admin-configured default for what happens when the threshold is crossed. Fires immediately through the existing kill-switch — no new "pending termination" hold state. A live proctor can fast-track an undo via the existing `ProctorReview` overturn path (see `05-fusion-flagging-engine-design.md`), which supersedes turn N+5's separate "termination is final" call. |
| `liveness_check_enabled` | boolean | Whether the liveness / anti-spoofing modality runs for sessions under this policy. Default `false` — opt-in. See `05-fusion-flagging-engine-design.md` §Path 4 and `04-inference-modules-design.md` §7. |
| `liveness_check_action` | nullable enum: `critical_terminate`, `medium_accumulate` | What happens on a failed liveness check. SQL constraint `ck_policy_liveness_action_when_enabled` requires this to be non-null when `liveness_check_enabled=true`. |
| `liveness_score_threshold` | numeric (0–1) | The minimum `real_score` for a frame to be classified `is_real=true`. Default `0.5` — flagged for calibration the same way the gaze thresholds are. |
| `extra_rules` | JSONB | Forward-compatible extension point. |
| `created_at`, `retired_at` | timestamp | `retired_at` is set when a new version supersedes this one. |
| `created_by` | string | Admin reference; the `AdminUser` table lands in the next layer. |

**Versioning.** `ExamSession.policy_config_id` references a specific
*snapshot*, not a mutable row. A policy change creates a new version
rather than mutating the old one, so a session that already ran can
always be interpreted under the rules that were in effect when it
happened.

### TerminationRecord
| Field | Type | Notes |
|---|---|---|
| `id` | UUID, PK | |
| `exam_session_id` | FK → ExamSession, unique | 1:1. |
| `triggering_flag_id` | FK → Flag | The flag that caused the termination. |
| `reason` | string | E.g. `second_person_confirmed`, `gaze_termination_exceeded`, `accumulated_score_exceeded`. |
| `client_delivery_status` | enum: `pending`, `sent`, `acknowledged`, `failed` | Kill-switch delivery state. |
| `client_command_sent_at`, `client_acknowledged_at` | nullable timestamp | |
| `lms_delivery_status` | enum: `pending`, `sent`, `acknowledged`, `failed` | LMS callback state. |
| `lms_callback_sent_at`, `lms_callback_completed_at` | nullable timestamp | |
| `evidence_sealed_at` | nullable timestamp | When the evidence bundle was finalized. |
| `created_at` | timestamp | |

**Immutability.** `TerminationRecord` is append-only at both the ORM
level and the PostgreSQL trigger level. The initial migration installs
the `termination_record_immutable` trigger; the ORM-level listener in
`src/proctoring_engine/models.py` mirrors it.

### ProctorReview
| Field | Type | Notes |
|---|---|---|
| `id` | UUID, PK | |
| `flag_id` | FK → Flag | |
| `reviewer_reference` | string | The admin who reviewed — a string reference until the `AdminUser` table lands. |
| `decision` | enum: `upheld`, `overturned`, `annotated`, `needs_more_info` | |
| `notes` | nullable text | |
| `created_at` | timestamp | |

---

## Resolved: admin / reviewer identity

**Resolved.** `AdminUser` is part of the initial schema (see
`SYSTEM_STATE.md` §1). `AccommodationExemption.approved_by`,
`PolicyConfig.created_by`, and `ProctorReview.reviewer_reference`
remain as string fields for backward compatibility, alongside the
structured FK columns (`approved_by_admin_id`, `created_by_id`,
`reviewer_admin_id`).

## New entity: IdentityVerificationOverrideRequest (designed, not yet built)

Needed for the identity-match fail-closed default (see
`04-inference-modules-design.md` §2): if the identity-match backend
can't construct, the exam session blocks entirely by default. The
only escape hatch is a scoped, time-bounded, two-person-approved
admin override, resolved ahead of the exam (not a live/real-time
flow).

| Field | Type | Notes |
|---|---|---|
| `id` | UUID, PK | |
| `exam_session_id` | FK → ExamSession | |
| `requested_by_admin_id` | FK → AdminUser | The professor/admin requesting the override. |
| `department` | string | See `AdminUser.department` below. |
| `reason` | text, required | Why the override is being requested. |
| `status` | enum: `pending`, `approved`, `rejected`, `expired` | |
| `approved_by_admin_id` | nullable FK → AdminUser | Must hold the `HEAD` role for `department`; enforced `!= requested_by_admin_id`. |
| `valid_from`, `valid_until` | timestamp | The time-bounded window the approval applies to. |
| `decided_at` | nullable timestamp | |
| `created_at` | timestamp | |

Unapproved requests should auto-expire rather than sit in the queue
indefinitely. The override lifts the session block only — it never
suppresses `ExamSession.identity_verification_status` or the
mandatory-review `Flag` that still gets raised.

**`AdminUser` gains two fields for this:** a `department` string
(kept deliberately light — not a full `Department` entity with its
own lifecycle, since nothing else in the system needs that structure
yet), and a new `HEAD` value in the admin-role enum, alongside the
existing `ADMIN`/`INSTRUCTOR`/`PROCTOR` tiers.

**`ExamSession` gains:** `identity_verification_status` — enum
(`verified`, `unavailable`, `failed_to_match`), deliberately distinct
from ordinary `Flag` severity, so "we couldn't check" never reads as
a variant of "this looks suspicious."

---

## Relationship summary

```
Participant 1──* ExamSession
Participant 1──* EnrollmentReference
Participant 1──* AccommodationExemption
ExamSession *──1 PolicyConfig (versioned snapshot)
ExamSession 1──* TelemetryEvent
ExamSession 1──* Flag
ExamSession 1──0/1 TerminationRecord
Flag *──* TelemetryEvent (contributing evidence, via FlagTelemetryEvent)
Flag 1──0/1 EvidenceArtifact (unique)
Flag 1──* ProctorReview
Flag *──0/1 AccommodationExemption (suppression reference)
```
