"""Per-session fusion aggregator.

One ``SessionAggregator`` instance per active ``ExamSession``.  It
holds whatever short-lived state each escalation rule needs (the gaze
rolling-window counter, the second-person confirmation-frame counter,
the accumulated medium score) and emits ``FlagDecision`` objects.

**This is working state only.**  Outcomes (confirmed ``Flag`` rows)
are persisted by the orchestration layer, not here.  A reasonable home
for this in production is an in-memory dict keyed by
``exam_session_id``; at scale, a Redis-backed structure.

The aggregator **never hardcodes a threshold** — every limit, window,
and weight comes from the ``PolicyConfig`` snapshot bound to the
session at launch time, passed in via the ``PolicySnapshot`` frozen
dataclass at construction.

The aggregator **never touches the database** — it receives typed
``InferenceResult`` objects and returns ``FlagDecision`` objects.
The orchestration layer is responsible for:

1. Creating immutable ``Flag`` + ``FlagTelemetryEvent`` rows.
2. Firing the kill-switch when ``FlagDecision.triggered_termination``.
3. Updating ``ExamSession.accumulated_medium_score`` by the delta.

Design doc: ``docs/05-fusion-flagging-engine-design.md``.
"""

from __future__ import annotations

import collections
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Final

from proctoring_engine.inference._types import (
    BrowserEventResult,
    ConfidenceInterval,
    FacePresenceResult,
    HeadPoseGazeResult,
    LivenessResult,
    ObjectDetectionResult,
)
from proctoring_engine.fusion._types import FlagDecision, GazeAwayEvent
from proctoring_engine.fusion.book_severity import (
    BOOK_FLAG_SEVERITY,
    BOOK_RULE_CODE,
    should_flag_book,
)
from proctoring_engine.fusion.exemptions import (
    SUPPRESSED_SEVERITY,
    ExemptionRecord,
    find_matching_exemption,
)
from proctoring_engine.models import LivenessAction, MediumScoreAction


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rule codes (matches Flag.rule_code in the ORM)
# ---------------------------------------------------------------------------

RULE_SECOND_PERSON: Final[str] = "second_person"
RULE_GAZE_AWAY_FREQUENCY: Final[str] = "gaze_away_frequency"
RULE_ACCUMULATED_SCORE: Final[str] = "accumulated_score"
RULE_OBJECT_DETECTED: Final[str] = "object_detected"
RULE_BROWSER_EVENT: Final[str] = "browser_event"
RULE_LIVENESS_CHECK_FAILED: Final[str] = "liveness_check_failed"

# Severity constants (match FlagSeverity enum values)
_CRITICAL: Final[str] = "critical"
_MEDIUM: Final[str] = "medium"

# Browser event types that contribute to the accumulated score
_BROWSER_ACCUMULATION_EVENTS: Final[frozenset[str]] = frozenset({
    "visibilitychange",
    "blur",
})


# ---------------------------------------------------------------------------
# PolicySnapshot — frozen config from the session's PolicyConfig row
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    """Immutable snapshot of the policy thresholds for one session.

    Every field maps 1:1 to a ``PolicyConfig`` column.  The
    aggregator reads these at every evaluation step; they never change
    during a session.  The constructor does not validate — the schema
    constraints on ``PolicyConfig`` have already done that.
    """

    terminate_on_second_face: bool = True
    second_face_confirmation_frames: int = 3

    gaze_min_duration_ms: int = 800
    gaze_window_seconds: int = 300
    gaze_warning_limit: int = 3
    gaze_termination_limit: int = 8

    medium_score_termination_threshold: float = 10.0
    medium_score_action: MediumScoreAction = MediumScoreAction.AUTO_TERMINATE

    liveness_check_enabled: bool = False
    liveness_check_action: LivenessAction | None = None
    liveness_score_threshold: float = 0.5

    # Per-rule-code weight for the accumulated-score path.
    # Keys are ``Flag.rule_code`` values; values are the weight
    # added to ``ExamSession.accumulated_medium_score`` when that
    # rule fires a ``MEDIUM`` flag.  Default: every MEDIUM flag
    # adds 1.0 regardless of rule code.
    score_weights: dict[str, float] = field(default_factory=dict)

    def weight_for(self, rule_code: str) -> float:
        """Return the weight for a given rule code (default 1.0)."""
        return self.score_weights.get(rule_code, 1.0)


# ---------------------------------------------------------------------------
# SessionContext — session-level metadata the aggregator needs
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SessionContext:
    """Session-level metadata the aggregator reads but never mutates.

    This is the denormalised subset of ``ExamSession`` + its relations
    that the aggregator needs.  The orchestration layer constructs it
    once at WebSocket connect and passes it into the aggregator.
    """

    exam_session_id: uuid.UUID
    participant_id: uuid.UUID
    exam_reference: str
    policy_config_id: uuid.UUID

    # From ExamSession
    allowed_reference_materials: str = "closed_book"
    permitted_material_details: dict[str, Any] = field(default_factory=dict)

    # Pre-loaded exemptions for this participant
    exemptions: list[ExemptionRecord] = field(default_factory=list)


# ---------------------------------------------------------------------------
# SessionAggregator
# ---------------------------------------------------------------------------

class SessionAggregator:
    """Per-session stateful fusion aggregator.

    Constructed once per active ``ExamSession`` and receives typed
    inference results as they arrive.  Returns zero or more
    ``FlagDecision`` objects per call.

    **Thread safety:** not thread-safe.  The WebSocket handler is
    single-reader-per-session, so this is fine.  If the architecture
    changes to multi-reader, the caller must synchronise.

    Parameters
    ----------
    policy:
        Frozen policy thresholds from ``PolicyConfig``.
    context:
        Session-level metadata (IDs, reference-material policy,
        exemptions).
    """

    def __init__(
        self,
        *,
        policy: PolicySnapshot,
        context: SessionContext,
    ) -> None:
        self._policy = policy
        self._ctx = context

        # --- Path 1: second-person confirmation counter ---
        self._consecutive_second_person_frames: int = 0
        self._second_person_event_ids: list[uuid.UUID] = []
        self._second_person_fired: bool = False

        # --- Path 2: gaze state machine ---
        # Current off-screen streak (reset on on_screen)
        self._gaze_off_start_ms: int | None = None
        self._gaze_off_event_ids: list[uuid.UUID] = []
        # Completed GazeAwayEvents within the rolling window
        self._gaze_away_events: collections.deque[GazeAwayEvent] = (
            collections.deque()
        )
        self._gaze_termination_fired: bool = False

        # --- Path 3: accumulated score ---
        self._accumulated_score: float = 0.0

    # -- Accessors for testing / observability --

    @property
    def accumulated_score(self) -> float:
        """Current accumulated medium score."""
        return self._accumulated_score

    @property
    def gaze_away_count_in_window(self) -> int:
        """Current rolling-window gaze-away event count."""
        return len(self._gaze_away_events)

    @property
    def consecutive_second_person_frames(self) -> int:
        """Current consecutive second-person frame count."""
        return self._consecutive_second_person_frames

    # -----------------------------------------------------------------
    # Path 1: zero-tolerance (second person)
    # -----------------------------------------------------------------

    def process_face_presence(
        self,
        result: FacePresenceResult,
        *,
        telemetry_event_id: uuid.UUID,
    ) -> list[FlagDecision]:
        """Process a face-presence inference result.

        When the face count is ≥ 2 for
        ``PolicyConfig.second_face_confirmation_frames`` consecutive
        frames, emits a ``CRITICAL`` flag with
        ``triggered_termination=True``.

        The 2–3-frame confirmation window is **noise filtering**, not
        leniency (per ``SKILLS_ALIGNMENT.md`` §6).
        """

        if self._second_person_fired:
            return []

        if not self._policy.terminate_on_second_face:
            return []

        if result.face_count >= 2:
            self._consecutive_second_person_frames += 1
            self._second_person_event_ids.append(telemetry_event_id)

            if (
                self._consecutive_second_person_frames
                >= self._policy.second_face_confirmation_frames
            ):
                self._second_person_fired = True
                decision = FlagDecision(
                    rule_code=RULE_SECOND_PERSON,
                    severity=_CRITICAL,
                    confidence=result.confidence,
                    triggered_termination=True,
                    detail={
                        "face_count": result.face_count,
                        "confirmation_frames": (
                            self._policy.second_face_confirmation_frames
                        ),
                    },
                    contributing_event_ids=tuple(
                        self._second_person_event_ids
                    ),
                )
                return [decision]
        else:
            # Reset: the streak was broken.
            self._consecutive_second_person_frames = 0
            self._second_person_event_ids.clear()

        return []

    # -----------------------------------------------------------------
    # Path 4: liveness / anti-spoofing
    # -----------------------------------------------------------------

    def process_liveness(
        self,
        result: LivenessResult,
        *,
        telemetry_event_id: uuid.UUID,
    ) -> list[FlagDecision]:
        """Process a liveness inference result.

        The liveness modality catches print and screen-replay spoofing
        specifically — not deepfakes or 3D masks (``docs/04-inference-modules-design.md``
        §7).  A failed check is one where ``is_real=False``: the
        model returned ``is_real=False`` or its raw confidence was
        below the policy threshold.

        Branches on ``PolicyConfig.liveness_check_action``:

        - ``CRITICAL_TERMINATE`` (default for institutions that treat
          spoofing as a hard rule): emit a ``CRITICAL`` flag with
          ``triggered_termination=True``.
        - ``MEDIUM_ACCUMULATE``: emit a ``MEDIUM`` flag with
          ``score_delta=1.0`` that contributes to the
          accumulated-score path — ``liveness_check_failed`` follows
          the same escalation ladder as ``gaze_away_frequency``,
          ``browser_event``, etc.

        Returns ``[]`` if liveness check is disabled on the policy
        or if the frame passes (``is_real=True``).
        """

        if not self._policy.liveness_check_enabled:
            return []

        if result.is_real:
            return []

        if self._policy.liveness_check_action == LivenessAction.CRITICAL_TERMINATE:
            return [
                FlagDecision(
                    rule_code=RULE_LIVENESS_CHECK_FAILED,
                    severity=_CRITICAL,
                    confidence=result.confidence,
                    triggered_termination=True,
                    detail={
                        "model_is_real": result.is_real,
                        "raw_confidence": result.confidence.score,
                        "threshold": self._policy.liveness_score_threshold,
                        "liveness_check_action": (
                            LivenessAction.CRITICAL_TERMINATE.value
                        ),
                    },
                    contributing_event_ids=(telemetry_event_id,),
                )
            ]

        # MEDIUM_ACCUMULATE: contributes to the accumulated score.
        weight = self._policy.weight_for(RULE_LIVENESS_CHECK_FAILED)
        decision = FlagDecision(
            rule_code=RULE_LIVENESS_CHECK_FAILED,
            severity=_MEDIUM,
            confidence=result.confidence,
            triggered_termination=False,
            detail={
                "model_is_real": result.is_real,
                "raw_confidence": result.confidence.score,
                "threshold": self._policy.liveness_score_threshold,
                "liveness_check_action": (
                    LivenessAction.MEDIUM_ACCUMULATE.value
                ),
            },
            contributing_event_ids=(telemetry_event_id,),
            score_delta=weight,
        )

        self._accumulated_score += weight

        decisions = [decision]
        acc_decision = self._check_accumulated_threshold(
            contributing_event_ids=(telemetry_event_id,),
        )
        if acc_decision is not None:
            decisions.append(acc_decision)
        return decisions

    # -----------------------------------------------------------------
    # Path 2: gaze-away frequency ladder
    # -----------------------------------------------------------------

    def process_gaze(
        self,
        result: HeadPoseGazeResult,
        *,
        telemetry_event_id: uuid.UUID,
        frame_timestamp_ms: int,
    ) -> list[FlagDecision]:
        """Process a head-pose/gaze inference result.

        Stage 2 of the gaze pipeline: merge consecutive
        ``off_screen`` frames into ``GazeAwayEvent`` objects once they
        exceed ``gaze_min_duration_ms``.  Then evaluate the
        rolling-window count against the warning and termination
        limits.

        Returns zero, one, or two ``FlagDecision`` objects:
        - A ``MEDIUM`` flag at the warning limit.
        - A ``CRITICAL`` flag (with termination) at the termination
          limit.
        - The accumulated-score ``CRITICAL`` flag if Path 3 trips.
        """

        if self._gaze_termination_fired:
            return []

        decisions: list[FlagDecision] = []

        if result.off_screen:
            # Continue / start an off-screen streak
            if self._gaze_off_start_ms is None:
                self._gaze_off_start_ms = frame_timestamp_ms
            self._gaze_off_event_ids.append(telemetry_event_id)
        else:
            # On-screen: close any active streak if it exceeded the
            # minimum duration
            if self._gaze_off_start_ms is not None:
                duration_ms = frame_timestamp_ms - self._gaze_off_start_ms
                if duration_ms >= self._policy.gaze_min_duration_ms:
                    event = GazeAwayEvent(
                        started_at_ms=self._gaze_off_start_ms,
                        ended_at_ms=frame_timestamp_ms,
                        contributing_event_ids=tuple(
                            self._gaze_off_event_ids
                        ),
                    )
                    self._gaze_away_events.append(event)

                # Reset streak regardless of whether it qualified
                self._gaze_off_start_ms = None
                self._gaze_off_event_ids = []

        # Expire old events outside the rolling window
        window_start_ms = (
            frame_timestamp_ms - self._policy.gaze_window_seconds * 1000
        )
        while (
            self._gaze_away_events
            and self._gaze_away_events[0].ended_at_ms < window_start_ms
        ):
            self._gaze_away_events.popleft()

        count = len(self._gaze_away_events)

        # Evaluate thresholds (termination takes priority over warning)
        if count >= self._policy.gaze_termination_limit:
            self._gaze_termination_fired = True
            # Gather all contributing event IDs from events in window
            all_ids: list[uuid.UUID] = []
            for ev in self._gaze_away_events:
                all_ids.extend(ev.contributing_event_ids)
            decisions.append(
                FlagDecision(
                    rule_code=RULE_GAZE_AWAY_FREQUENCY,
                    severity=_CRITICAL,
                    confidence=result.confidence,
                    triggered_termination=True,
                    detail={
                        "gaze_away_count": count,
                        "gaze_termination_limit": (
                            self._policy.gaze_termination_limit
                        ),
                        "window_seconds": self._policy.gaze_window_seconds,
                    },
                    contributing_event_ids=tuple(all_ids),
                )
            )
        elif count >= self._policy.gaze_warning_limit:
            # Collect contributing IDs from the most recent event only
            # (the one that tipped us over the warning threshold)
            latest = self._gaze_away_events[-1] if self._gaze_away_events else None
            contributing = (
                latest.contributing_event_ids if latest else ()
            )
            weight = self._policy.weight_for(RULE_GAZE_AWAY_FREQUENCY)

            medium_decision = FlagDecision(
                rule_code=RULE_GAZE_AWAY_FREQUENCY,
                severity=_MEDIUM,
                confidence=result.confidence,
                triggered_termination=False,
                detail={
                    "gaze_away_count": count,
                    "gaze_warning_limit": self._policy.gaze_warning_limit,
                    "window_seconds": self._policy.gaze_window_seconds,
                },
                contributing_event_ids=contributing,
                score_delta=weight,
            )
            decisions.append(medium_decision)

            # Apply the score delta to the running accumulator
            self._accumulated_score += weight
            acc_decision = self._check_accumulated_threshold(
                contributing_event_ids=contributing,
            )
            if acc_decision is not None:
                decisions.append(acc_decision)

        return decisions

    # -----------------------------------------------------------------
    # Object detection
    # -----------------------------------------------------------------

    def process_object_detection(
        self,
        result: ObjectDetectionResult,
        *,
        telemetry_event_id: uuid.UUID,
        now: Any,
    ) -> list[FlagDecision]:
        """Process an object-detection inference result.

        Parameters
        ----------
        result:
            A single denylist detection (book, cell phone, etc.).
        telemetry_event_id:
            The ``TelemetryEvent.id`` for this detection.
        now:
            Current UTC ``datetime``, used for exemption window check.

        Returns
        -------
        list[FlagDecision]
            Zero or one flag decisions.

        Book handling:
            Book detections always reach this method.  Whether they
            escalate to a flag depends on
            ``ExamSession.allowed_reference_materials``.

        Exemption handling:
            Before finalising the flag, check ``AccommodationExemption``
            for a matching ``participant_id`` + ``object_class``.
            Matching → downgrade severity to ``LOW``, set
            ``suppressed_by_exemption_id``.
        """

        detected_class = result.detected_class

        # Book-detection severity check
        if detected_class == "book":
            if not should_flag_book(
                self._ctx.allowed_reference_materials,
                self._ctx.permitted_material_details,
            ):
                return []

        # Check exemptions
        exemption = find_matching_exemption(
            participant_id=self._ctx.participant_id,
            object_class=detected_class,
            exam_reference=self._ctx.exam_reference,
            now=now,
            exemptions=self._ctx.exemptions,
        )

        if detected_class == "book":
            rule_code = BOOK_RULE_CODE
        else:
            rule_code = RULE_OBJECT_DETECTED

        base_severity = (
            BOOK_FLAG_SEVERITY if detected_class == "book" else _MEDIUM
        )

        if exemption is not None:
            severity = SUPPRESSED_SEVERITY
            suppressed_by = exemption.id
        else:
            severity = base_severity
            suppressed_by = None

        decision = FlagDecision(
            rule_code=rule_code,
            severity=severity,
            confidence=result.confidence,
            triggered_termination=False,
            suppressed_by_exemption_id=suppressed_by,
            detail={
                "detected_class": detected_class,
                "yolo_confidence": result.confidence.score,
                "suppressed": exemption is not None,
            },
            contributing_event_ids=(telemetry_event_id,),
        )

        # Non-suppressed MEDIUM detections contribute to accumulation
        if severity == _MEDIUM:
            weight = self._policy.weight_for(rule_code)
            decision = FlagDecision(
                rule_code=decision.rule_code,
                severity=decision.severity,
                confidence=decision.confidence,
                triggered_termination=decision.triggered_termination,
                suppressed_by_exemption_id=decision.suppressed_by_exemption_id,
                detail=decision.detail,
                contributing_event_ids=decision.contributing_event_ids,
                score_delta=weight,
            )
            self._accumulated_score += weight

            decisions = [decision]
            acc_decision = self._check_accumulated_threshold(
                contributing_event_ids=(telemetry_event_id,),
            )
            if acc_decision is not None:
                decisions.append(acc_decision)
            return decisions

        return [decision]

    # -----------------------------------------------------------------
    # Browser events
    # -----------------------------------------------------------------

    def process_browser_event(
        self,
        result: BrowserEventResult,
        *,
        telemetry_event_id: uuid.UUID,
    ) -> list[FlagDecision]:
        """Process a browser-event inference result.

        ``visibilitychange`` and ``blur`` events contribute to the
        accumulated-score path as ``MEDIUM`` flags.  Other browser
        events are logged as ``MEDIUM`` flags without accumulation.
        """

        accumulates = result.event_type in _BROWSER_ACCUMULATION_EVENTS
        weight = self._policy.weight_for(RULE_BROWSER_EVENT) if accumulates else 0.0

        decision = FlagDecision(
            rule_code=RULE_BROWSER_EVENT,
            severity=_MEDIUM,
            confidence=result.confidence,
            triggered_termination=False,
            detail={
                "browser_event_type": result.event_type,
                "detail": result.detail,
            },
            contributing_event_ids=(telemetry_event_id,),
            score_delta=weight,
        )

        decisions: list[FlagDecision] = [decision]

        if accumulates:
            self._accumulated_score += weight
            acc_decision = self._check_accumulated_threshold(
                contributing_event_ids=(telemetry_event_id,),
            )
            if acc_decision is not None:
                decisions.append(acc_decision)

        return decisions

    # -----------------------------------------------------------------
    # Path 3: accumulated-score threshold check
    # -----------------------------------------------------------------

    def _check_accumulated_threshold(
        self,
        *,
        contributing_event_ids: tuple[uuid.UUID, ...],
    ) -> FlagDecision | None:
        """Check whether the accumulated score has crossed the threshold.

        Branches on ``PolicyConfig.medium_score_action``:

        - ``AUTO_TERMINATE`` (legacy default): emits a ``CRITICAL`` flag
          with ``triggered_termination=True``.  Fires through the
          existing kill-switch mechanism — no new "pending termination"
          hold state.  Per the design doc §Path 3, this is overridable
          via the live-proctor fast-track-undo path
          (``TERMINATED → REINSTATED`` state-machine transition,
          triggered by an ``OVERTURNED`` ``ProctorReview`` against
          the triggering flag).
        - ``FLAG_FOR_REVIEW``: emits a ``CRITICAL`` flag with
          ``triggered_termination=False``.  The session stays live;
          a human reviewer must act.  Auto-termination is deferred
          to the reviewer's decision.

        Returns ``None`` if the threshold is not crossed, or if the
        path is disabled (``medium_score_termination_threshold = 0``).
        """

        threshold = self._policy.medium_score_termination_threshold
        if threshold <= 0:
            # Threshold of 0 means the accumulated-score path is
            # disabled (PolicyConfig.medium_score_termination_threshold
            # has a >= 0 constraint, so 0 is the only way to disable).
            return None

        if self._accumulated_score < threshold:
            return None

        if self._policy.medium_score_action == MediumScoreAction.AUTO_TERMINATE:
            return FlagDecision(
                rule_code=RULE_ACCUMULATED_SCORE,
                severity=_CRITICAL,
                confidence=ConfidenceInterval(
                    lower=1.0, score=1.0, upper=1.0,
                ),
                triggered_termination=True,
                detail={
                    "accumulated_score": self._accumulated_score,
                    "threshold": threshold,
                    "medium_score_action": MediumScoreAction.AUTO_TERMINATE.value,
                },
                contributing_event_ids=contributing_event_ids,
            )

        # FLAG_FOR_REVIEW: CRITICAL flag, no auto-termination.
        return FlagDecision(
            rule_code=RULE_ACCUMULATED_SCORE,
            severity=_CRITICAL,
            confidence=ConfidenceInterval(
                lower=1.0, score=1.0, upper=1.0,
            ),
            triggered_termination=False,
            detail={
                "accumulated_score": self._accumulated_score,
                "threshold": threshold,
                "medium_score_action": MediumScoreAction.FLAG_FOR_REVIEW.value,
            },
            contributing_event_ids=contributing_event_ids,
        )
