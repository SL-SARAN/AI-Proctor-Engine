"""Unit tests for the fusion & flagging engine.

Tests all three termination paths, exemption suppression,
book-detection severity, and boundary cases.  No database access —
the aggregator is tested in isolation with typed inference results
and in-memory policy snapshots.

Boundary cases per SKILLS_ALIGNMENT.md §3.5:
- Frame count at exactly ``second_face_confirmation_frames``
- Gaze count at exactly ``gaze_warning_limit`` and
  ``gaze_termination_limit``
- Accumulated score at exactly ``medium_score_termination_threshold``
- Window expiry (gaze event ages out)
- Exemption match / no match / expired / not yet effective
- Book with ``CLOSED_BOOK`` / ``OPEN_BOOK`` / ``SPECIFIC_LIST``
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from proctoring_engine.inference._types import (
    AudioVadResult,
    BoundingBox,
    BrowserEventResult,
    ConfidenceInterval,
    FacePresenceResult,
    HeadPoseGazeResult,
    IdentityMatchResult,
    LivenessResult,
    ObjectDetectionResult,
)
from proctoring_engine.fusion._types import FlagDecision, GazeAwayEvent
from proctoring_engine.fusion.aggregator import (
    RULE_ACCUMULATED_SCORE,
    RULE_AUDIO_ANOMALY,
    RULE_BROWSER_EVENT,
    RULE_GAZE_AWAY_FREQUENCY,
    RULE_IDENTITY_MISMATCH,
    RULE_LIVENESS_CHECK_FAILED,
    RULE_OBJECT_DETECTED,
    RULE_SECOND_PERSON,
    PolicySnapshot,
    SessionAggregator,
    SessionContext,
)
from proctoring_engine.models import LivenessAction, MediumScoreAction
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CI = ConfidenceInterval(lower=0.9, score=0.95, upper=0.99)
_CI_POINT = ConfidenceInterval(lower=1.0, score=1.0, upper=1.0)

_NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)

_EXAM_REF = "exam-001"
_PARTICIPANT = uuid.uuid4()
_POLICY_ID = uuid.uuid4()
_SESSION_ID = uuid.uuid4()


def _default_policy(**overrides: object) -> PolicySnapshot:
    defaults: dict[str, object] = {
        "terminate_on_second_face": True,
        "second_face_confirmation_frames": 3,
        "gaze_min_duration_ms": 800,
        "gaze_window_seconds": 300,
        "gaze_warning_limit": 3,
        "gaze_termination_limit": 8,
        "medium_score_termination_threshold": 10.0,
        "medium_score_action": MediumScoreAction.AUTO_TERMINATE,
        "liveness_check_enabled": False,
        "liveness_check_action": None,
        "liveness_score_threshold": 0.5,
    }
    defaults.update(overrides)
    return PolicySnapshot(**defaults)  # type: ignore[arg-type]


def _default_context(
    *,
    exemptions: list[ExemptionRecord] | None = None,
    allowed_reference_materials: str = "closed_book",
    permitted_material_details: dict | None = None,
) -> SessionContext:
    return SessionContext(
        exam_session_id=_SESSION_ID,
        participant_id=_PARTICIPANT,
        exam_reference=_EXAM_REF,
        policy_config_id=_POLICY_ID,
        exemptions=exemptions or [],
        allowed_reference_materials=allowed_reference_materials,
        permitted_material_details=permitted_material_details or {},
    )


def _make_face_result(face_count: int = 2) -> FacePresenceResult:
    return FacePresenceResult(
        modality="face",
        event_type="second_person" if face_count >= 2 else "one_face",
        confidence=_CI,
        face_count=face_count,
    )


def _make_gaze_result(off_screen: bool | None = True) -> HeadPoseGazeResult:
    return HeadPoseGazeResult(
        modality="gaze",
        event_type="no_landmarks" if off_screen is None else ("off_screen" if off_screen else "on_screen"),
        confidence=_CI,
        off_screen=off_screen,
    )


def _make_object_result(detected_class: str = "cell phone") -> ObjectDetectionResult:
    return ObjectDetectionResult(
        modality="object",
        event_type=detected_class,
        confidence=_CI,
        bounding_boxes=[BoundingBox(x=0.1, y=0.2, w=0.3, h=0.4)],
        detected_class=detected_class,
    )


def _make_identity_result(similarity: float = 0.5) -> IdentityMatchResult:
    return IdentityMatchResult(
        modality="identity",
        event_type="identity_mismatch" if similarity < 0.6 else "identity_match",
        confidence=ConfidenceInterval(lower=similarity, score=similarity, upper=similarity),
        similarity=similarity,
    )


def _make_audio_result(event_type: str = "speech_detected") -> AudioVadResult:
    return AudioVadResult(
        modality="audio",
        event_type=event_type,
        confidence=_CI,
        speech_ratio=0.8 if event_type == "speech_detected" else 0.0,
        rms_db=-10.0,
    )


def _make_browser_result(event_type: str = "visibilitychange") -> BrowserEventResult:
    return BrowserEventResult(
        modality="browser",
        event_type=event_type,
        confidence=_CI_POINT,
        detail={"hidden": True},
    )


def _make_aggregator(
    policy: PolicySnapshot | None = None,
    context: SessionContext | None = None,
) -> SessionAggregator:
    return SessionAggregator(
        policy=policy or _default_policy(),
        context=context or _default_context(),
    )


# ===================================================================
# GazeAwayEvent
# ===================================================================


class TestGazeAwayEvent:
    """Test the GazeAwayEvent data type."""

    def test_construction(self) -> None:
        ev = GazeAwayEvent(started_at_ms=1000, ended_at_ms=2000)
        assert ev.started_at_ms == 1000
        assert ev.ended_at_ms == 2000
        assert ev.contributing_event_ids == ()

    def test_with_event_ids(self) -> None:
        ids = (uuid.uuid4(), uuid.uuid4())
        ev = GazeAwayEvent(
            started_at_ms=0,
            ended_at_ms=900,
            contributing_event_ids=ids,
        )
        assert ev.contributing_event_ids == ids

    def test_frozen(self) -> None:
        ev = GazeAwayEvent(started_at_ms=0, ended_at_ms=100)
        with pytest.raises(AttributeError):
            ev.started_at_ms = 999  # type: ignore[misc]


# ===================================================================
# FlagDecision
# ===================================================================


class TestFlagDecision:
    """Test the FlagDecision data type."""

    def test_defaults(self) -> None:
        d = FlagDecision(rule_code="test", severity="medium", confidence=_CI)
        assert d.triggered_termination is False
        assert d.suppressed_by_exemption_id is None
        assert d.contributing_event_ids == ()
        assert d.score_delta == 0.0
        assert d.detail == {}

    def test_full_construction(self) -> None:
        eid = uuid.uuid4()
        exid = uuid.uuid4()
        d = FlagDecision(
            rule_code="second_person",
            severity="critical",
            confidence=_CI,
            triggered_termination=True,
            suppressed_by_exemption_id=exid,
            detail={"key": "val"},
            contributing_event_ids=(eid,),
            score_delta=2.5,
        )
        assert d.triggered_termination is True
        assert d.suppressed_by_exemption_id == exid
        assert d.score_delta == 2.5

    def test_frozen(self) -> None:
        d = FlagDecision(rule_code="test", severity="medium", confidence=_CI)
        with pytest.raises(AttributeError):
            d.rule_code = "other"  # type: ignore[misc]


# ===================================================================
# PolicySnapshot
# ===================================================================


class TestPolicySnapshot:
    """Test the PolicySnapshot frozen dataclass."""

    def test_defaults(self) -> None:
        p = PolicySnapshot()
        assert p.terminate_on_second_face is True
        assert p.second_face_confirmation_frames == 3
        assert p.gaze_min_duration_ms == 800
        assert p.gaze_window_seconds == 300
        assert p.gaze_warning_limit == 3
        assert p.gaze_termination_limit == 8
        assert p.medium_score_termination_threshold == 10.0

    def test_weight_for_default(self) -> None:
        p = PolicySnapshot()
        assert p.weight_for("anything") == 1.0

    def test_weight_for_custom(self) -> None:
        p = PolicySnapshot(score_weights={"browser_event": 0.5, "gaze_away_frequency": 2.0})
        assert p.weight_for("browser_event") == 0.5
        assert p.weight_for("gaze_away_frequency") == 2.0
        assert p.weight_for("unknown") == 1.0


# ===================================================================
# Path 1: zero-tolerance (second person)
# ===================================================================


class TestSecondPersonPath:
    """Test the zero-tolerance second-person detection path."""

    def test_single_frame_no_flag(self) -> None:
        agg = _make_aggregator()
        result = _make_face_result(face_count=2)
        decisions = agg.process_face_presence(result, telemetry_event_id=uuid.uuid4())
        assert decisions == []
        assert agg.consecutive_second_person_frames == 1

    def test_below_threshold_no_flag(self) -> None:
        agg = _make_aggregator(policy=_default_policy(second_face_confirmation_frames=3))
        for _ in range(2):
            decisions = agg.process_face_presence(
                _make_face_result(face_count=2),
                telemetry_event_id=uuid.uuid4(),
            )
            assert decisions == []
        assert agg.consecutive_second_person_frames == 2

    def test_at_threshold_fires(self) -> None:
        """Fires when count == confirmation_frames (boundary)."""
        agg = _make_aggregator(policy=_default_policy(second_face_confirmation_frames=3))
        for i in range(3):
            decisions = agg.process_face_presence(
                _make_face_result(face_count=2),
                telemetry_event_id=uuid.uuid4(),
            )
        assert len(decisions) == 1
        d = decisions[0]
        assert d.rule_code == RULE_SECOND_PERSON
        assert d.severity == "critical"
        assert d.triggered_termination is True
        assert len(d.contributing_event_ids) == 3

    def test_single_frame_resets_counter(self) -> None:
        """A single on-screen frame resets the consecutive counter."""
        agg = _make_aggregator(policy=_default_policy(second_face_confirmation_frames=3))
        # Two second-person frames
        for _ in range(2):
            agg.process_face_presence(
                _make_face_result(face_count=2),
                telemetry_event_id=uuid.uuid4(),
            )
        assert agg.consecutive_second_person_frames == 2
        # One normal frame resets
        agg.process_face_presence(
            _make_face_result(face_count=1),
            telemetry_event_id=uuid.uuid4(),
        )
        assert agg.consecutive_second_person_frames == 0

    def test_after_reset_needs_full_count(self) -> None:
        """After a reset, a new streak must reach the full confirmation count."""
        agg = _make_aggregator(policy=_default_policy(second_face_confirmation_frames=2))
        agg.process_face_presence(
            _make_face_result(face_count=2),
            telemetry_event_id=uuid.uuid4(),
        )
        agg.process_face_presence(
            _make_face_result(face_count=1),
            telemetry_event_id=uuid.uuid4(),
        )
        # One more second-person frame is not enough (need 2)
        decisions = agg.process_face_presence(
            _make_face_result(face_count=2),
            telemetry_event_id=uuid.uuid4(),
        )
        assert decisions == []

    def test_fires_only_once(self) -> None:
        """Once fired, subsequent second-person frames produce no flags."""
        agg = _make_aggregator(policy=_default_policy(second_face_confirmation_frames=1))
        d1 = agg.process_face_presence(
            _make_face_result(face_count=2),
            telemetry_event_id=uuid.uuid4(),
        )
        assert len(d1) == 1
        d2 = agg.process_face_presence(
            _make_face_result(face_count=2),
            telemetry_event_id=uuid.uuid4(),
        )
        assert d2 == []

    def test_disabled_by_policy(self) -> None:
        """When ``terminate_on_second_face=False``, no flag fires."""
        agg = _make_aggregator(
            policy=_default_policy(
                terminate_on_second_face=False,
                second_face_confirmation_frames=1,
            )
        )
        decisions = agg.process_face_presence(
            _make_face_result(face_count=3),
            telemetry_event_id=uuid.uuid4(),
        )
        assert decisions == []

    def test_zero_faces_no_flag(self) -> None:
        agg = _make_aggregator()
        decisions = agg.process_face_presence(
            _make_face_result(face_count=0),
            telemetry_event_id=uuid.uuid4(),
        )
        assert decisions == []

    def test_confirmation_frames_1(self) -> None:
        """Fires on the very first second-person frame when threshold is 1."""
        agg = _make_aggregator(policy=_default_policy(second_face_confirmation_frames=1))
        decisions = agg.process_face_presence(
            _make_face_result(face_count=2),
            telemetry_event_id=uuid.uuid4(),
        )
        assert len(decisions) == 1
        assert decisions[0].triggered_termination is True


# ===================================================================
# Path 2: gaze-away frequency ladder
# ===================================================================


class TestGazeAwayPath:
    """Test the gaze-away frequency escalation ladder."""

    def _send_off_screen_episode(
        self,
        agg: SessionAggregator,
        start_ms: int,
        duration_ms: int,
        frames: int = 5,
    ) -> list[FlagDecision]:
        """Simulate an off-screen episode spanning ``duration_ms``."""
        all_decisions: list[FlagDecision] = []
        step = duration_ms // frames if frames > 0 else 0
        for i in range(frames):
            t = start_ms + i * step
            all_decisions.extend(
                agg.process_gaze(
                    _make_gaze_result(off_screen=True),
                    telemetry_event_id=uuid.uuid4(),
                    frame_timestamp_ms=t,
                )
            )
        # Return on-screen to close the episode
        all_decisions.extend(
            agg.process_gaze(
                _make_gaze_result(off_screen=False),
                telemetry_event_id=uuid.uuid4(),
                frame_timestamp_ms=start_ms + duration_ms,
            )
        )
        return all_decisions

    def test_short_episode_no_gaze_event(self) -> None:
        """An off-screen streak shorter than ``gaze_min_duration_ms`` is ignored."""
        agg = _make_aggregator(policy=_default_policy(gaze_min_duration_ms=800))
        self._send_off_screen_episode(agg, start_ms=0, duration_ms=500)
        assert agg.gaze_away_count_in_window == 0

    def test_long_episode_creates_gaze_event(self) -> None:
        """An episode >= ``gaze_min_duration_ms`` creates a GazeAwayEvent."""
        agg = _make_aggregator(policy=_default_policy(gaze_min_duration_ms=800))
        self._send_off_screen_episode(agg, start_ms=0, duration_ms=900)
        assert agg.gaze_away_count_in_window == 1

    def test_exact_min_duration_creates_event(self) -> None:
        """An episode of exactly ``gaze_min_duration_ms`` creates an event."""
        agg = _make_aggregator(policy=_default_policy(gaze_min_duration_ms=800))
        self._send_off_screen_episode(agg, start_ms=0, duration_ms=800)
        assert agg.gaze_away_count_in_window == 1

    def test_below_warning_limit_no_flag(self) -> None:
        """Fewer events than ``gaze_warning_limit`` → no flag."""
        agg = _make_aggregator(
            policy=_default_policy(
                gaze_min_duration_ms=100,
                gaze_warning_limit=3,
            )
        )
        for i in range(2):
            self._send_off_screen_episode(
                agg, start_ms=i * 1000, duration_ms=200,
            )
        assert agg.gaze_away_count_in_window == 2

    def test_at_warning_limit_fires_medium(self) -> None:
        """At exactly ``gaze_warning_limit`` → MEDIUM flag (boundary)."""
        agg = _make_aggregator(
            policy=_default_policy(
                gaze_min_duration_ms=100,
                gaze_warning_limit=3,
                gaze_termination_limit=8,
            )
        )
        decisions: list[FlagDecision] = []
        for i in range(3):
            decisions.extend(
                self._send_off_screen_episode(
                    agg, start_ms=i * 1000, duration_ms=200,
                )
            )
        medium_flags = [d for d in decisions if d.severity == "medium"]
        assert len(medium_flags) == 1
        assert medium_flags[0].rule_code == RULE_GAZE_AWAY_FREQUENCY
        assert medium_flags[0].triggered_termination is False

    def test_at_termination_limit_fires_critical(self) -> None:
        """At exactly ``gaze_termination_limit`` → CRITICAL + termination."""
        agg = _make_aggregator(
            policy=_default_policy(
                gaze_min_duration_ms=100,
                gaze_warning_limit=3,
                gaze_termination_limit=5,
            )
        )
        decisions: list[FlagDecision] = []
        for i in range(5):
            decisions.extend(
                self._send_off_screen_episode(
                    agg, start_ms=i * 1000, duration_ms=200,
                )
            )
        critical_flags = [d for d in decisions if d.severity == "critical"]
        assert len(critical_flags) >= 1
        termination_flag = [d for d in critical_flags if d.rule_code == RULE_GAZE_AWAY_FREQUENCY]
        assert len(termination_flag) == 1
        assert termination_flag[0].triggered_termination is True

    def test_window_expiry(self) -> None:
        """Events older than ``gaze_window_seconds`` are expired."""
        window_s = 10  # 10 seconds
        agg = _make_aggregator(
            policy=_default_policy(
                gaze_min_duration_ms=100,
                gaze_window_seconds=window_s,
                gaze_warning_limit=3,
            )
        )
        # 2 events at t=0s
        for i in range(2):
            self._send_off_screen_episode(
                agg, start_ms=i * 500, duration_ms=200,
            )
        assert agg.gaze_away_count_in_window == 2

        # Advance past the window (11s later).  Send an on-screen frame
        # to trigger expiry.
        agg.process_gaze(
            _make_gaze_result(off_screen=False),
            telemetry_event_id=uuid.uuid4(),
            frame_timestamp_ms=11_000,
        )
        assert agg.gaze_away_count_in_window == 0

    def test_after_termination_no_more_flags(self) -> None:
        """Once gaze termination fires, no more gaze flags are produced."""
        agg = _make_aggregator(
            policy=_default_policy(
                gaze_min_duration_ms=100,
                gaze_warning_limit=1,
                gaze_termination_limit=2,
            )
        )
        for i in range(2):
            self._send_off_screen_episode(
                agg, start_ms=i * 1000, duration_ms=200,
            )
        # Now send more off-screen — should produce nothing
        extra = self._send_off_screen_episode(
            agg, start_ms=5000, duration_ms=200,
        )
        assert extra == []

    def test_medium_flag_accumulates_score(self) -> None:
        """A warning-level gaze flag should add to accumulated score."""
        agg = _make_aggregator(
            policy=_default_policy(
                gaze_min_duration_ms=100,
                gaze_warning_limit=1,
                gaze_termination_limit=100,  # won't trigger
                medium_score_termination_threshold=100.0,
            )
        )
        decisions = self._send_off_screen_episode(
            agg, start_ms=0, duration_ms=200,
        )
        medium_flags = [d for d in decisions if d.severity == "medium"]
        assert len(medium_flags) == 1
        assert medium_flags[0].score_delta == 1.0  # default weight
        assert agg.accumulated_score == 1.0

    def test_no_landmarks_excluded_from_gaze(self) -> None:
        """A frame with off_screen=None (no landmarks) does not start,
        break, or continue a gaze streak.  It is ignored entirely."""
        agg = _make_aggregator(
            policy=_default_policy(gaze_min_duration_ms=100, gaze_warning_limit=1)
        )
        # Start streak
        agg.process_gaze(
            _make_gaze_result(off_screen=True),
            telemetry_event_id=uuid.uuid4(),
            frame_timestamp_ms=0,
        )
        # No-landmarks frame in the middle
        agg.process_gaze(
            _make_gaze_result(off_screen=None),
            telemetry_event_id=uuid.uuid4(),
            frame_timestamp_ms=50,
        )
        # End streak
        decisions = agg.process_gaze(
            _make_gaze_result(off_screen=False),
            telemetry_event_id=uuid.uuid4(),
            frame_timestamp_ms=200,
        )
        # Streak was 200ms -> qualifies as an event
        assert agg.gaze_away_count_in_window == 1


# ===================================================================
# Path 3: accumulated-score termination
# ===================================================================


class TestAccumulatedScorePath:
    """Test the accumulated-score termination path."""

    def test_below_threshold_no_termination(self) -> None:
        agg = _make_aggregator(
            policy=_default_policy(
                medium_score_termination_threshold=5.0,
                gaze_min_duration_ms=100,
                gaze_warning_limit=1,
                gaze_termination_limit=100,
            )
        )
        # One gaze warning -> +1.0, below threshold
        for i in range(1):
            agg.process_gaze(
                _make_gaze_result(off_screen=True),
                telemetry_event_id=uuid.uuid4(),
                frame_timestamp_ms=i * 100,
            )
        agg.process_gaze(
            _make_gaze_result(off_screen=False),
            telemetry_event_id=uuid.uuid4(),
            frame_timestamp_ms=200,
        )
        assert agg.accumulated_score == 1.0

    def test_at_threshold_fires(self) -> None:
        """Fires exactly when accumulated score crosses the threshold."""
        agg = _make_aggregator(
            policy=_default_policy(
                medium_score_termination_threshold=2.0,
                gaze_min_duration_ms=100,
                gaze_warning_limit=1,
                gaze_termination_limit=100,
            )
        )
        all_decisions: list[FlagDecision] = []
        # Two gaze warnings: each adds 1.0, total = 2.0 = threshold
        for i in range(2):
            # Off-screen episode
            agg.process_gaze(
                _make_gaze_result(off_screen=True),
                telemetry_event_id=uuid.uuid4(),
                frame_timestamp_ms=i * 1000,
            )
            decisions = agg.process_gaze(
                _make_gaze_result(off_screen=False),
                telemetry_event_id=uuid.uuid4(),
                frame_timestamp_ms=i * 1000 + 200,
            )
            all_decisions.extend(decisions)

        acc_flags = [d for d in all_decisions if d.rule_code == RULE_ACCUMULATED_SCORE]
        assert len(acc_flags) == 1
        assert acc_flags[0].triggered_termination is True
        assert acc_flags[0].severity == "critical"

    def test_threshold_zero_disables(self) -> None:
        """A threshold of 0 disables the accumulated-score path."""
        agg = _make_aggregator(
            policy=_default_policy(
                medium_score_termination_threshold=0.0,
                gaze_min_duration_ms=100,
                gaze_warning_limit=1,
                gaze_termination_limit=100,
            )
        )
        agg.process_gaze(
            _make_gaze_result(off_screen=True),
            telemetry_event_id=uuid.uuid4(),
            frame_timestamp_ms=0,
        )
        decisions = agg.process_gaze(
            _make_gaze_result(off_screen=False),
            telemetry_event_id=uuid.uuid4(),
            frame_timestamp_ms=200,
        )
        # Should have a medium flag but no accumulated-score termination
        acc_flags = [d for d in decisions if d.rule_code == RULE_ACCUMULATED_SCORE]
        assert acc_flags == []

    def test_custom_weights(self) -> None:
        """Custom weights change the delta per flag."""
        agg = _make_aggregator(
            policy=_default_policy(
                medium_score_termination_threshold=5.0,
                gaze_min_duration_ms=100,
                gaze_warning_limit=1,
                gaze_termination_limit=100,
                score_weights={"gaze_away_frequency": 3.0},
            )
        )
        # One gaze warning with weight 3.0
        agg.process_gaze(
            _make_gaze_result(off_screen=True),
            telemetry_event_id=uuid.uuid4(),
            frame_timestamp_ms=0,
        )
        agg.process_gaze(
            _make_gaze_result(off_screen=False),
            telemetry_event_id=uuid.uuid4(),
            frame_timestamp_ms=200,
        )
        assert agg.accumulated_score == 3.0

    def test_browser_events_accumulate(self) -> None:
        """Tab-blur browser events accumulate score."""
        agg = _make_aggregator(
            policy=_default_policy(medium_score_termination_threshold=3.0)
        )
        all_decisions: list[FlagDecision] = []
        for _ in range(3):
            decisions = agg.process_browser_event(
                _make_browser_result(event_type="visibilitychange"),
                telemetry_event_id=uuid.uuid4(),
            )
            all_decisions.extend(decisions)
        assert agg.accumulated_score == 3.0
        acc_flags = [d for d in all_decisions if d.rule_code == RULE_ACCUMULATED_SCORE]
        assert len(acc_flags) == 1
        assert acc_flags[0].triggered_termination is True


# ===================================================================
# Browser events
# ===================================================================


class TestBrowserEvents:
    """Test browser event processing."""

    def test_visibilitychange_medium_flag(self) -> None:
        agg = _make_aggregator()
        decisions = agg.process_browser_event(
            _make_browser_result(event_type="visibilitychange"),
            telemetry_event_id=uuid.uuid4(),
        )
        assert len(decisions) >= 1
        assert decisions[0].severity == "medium"
        assert decisions[0].rule_code == RULE_BROWSER_EVENT

    def test_blur_accumulates(self) -> None:
        agg = _make_aggregator()
        decisions = agg.process_browser_event(
            _make_browser_result(event_type="blur"),
            telemetry_event_id=uuid.uuid4(),
        )
        assert decisions[0].score_delta == 1.0
        assert agg.accumulated_score == 1.0

    def test_focus_does_not_accumulate(self) -> None:
        agg = _make_aggregator()
        decisions = agg.process_browser_event(
            _make_browser_result(event_type="focus"),
            telemetry_event_id=uuid.uuid4(),
        )
        assert decisions[0].score_delta == 0.0
        assert agg.accumulated_score == 0.0

    def test_contextmenu_does_not_accumulate(self) -> None:
        agg = _make_aggregator()
        decisions = agg.process_browser_event(
            _make_browser_result(event_type="contextmenu"),
            telemetry_event_id=uuid.uuid4(),
        )
        assert decisions[0].score_delta == 0.0

    def test_contributing_event_id_present(self) -> None:
        eid = uuid.uuid4()
        agg = _make_aggregator()
        decisions = agg.process_browser_event(
            _make_browser_result(event_type="paste"),
            telemetry_event_id=eid,
        )
        assert eid in decisions[0].contributing_event_ids


# ===================================================================
# Object detection
# ===================================================================


class TestObjectDetection:
    """Test object-detection flag processing."""

    def test_cell_phone_fires_medium(self) -> None:
        agg = _make_aggregator()
        decisions = agg.process_object_detection(
            _make_object_result("cell phone"),
            telemetry_event_id=uuid.uuid4(),
            now=_NOW,
        )
        assert len(decisions) >= 1
        assert decisions[0].severity == "medium"
        assert decisions[0].rule_code == RULE_OBJECT_DETECTED

    def test_laptop_fires_medium(self) -> None:
        agg = _make_aggregator()
        decisions = agg.process_object_detection(
            _make_object_result("laptop"),
            telemetry_event_id=uuid.uuid4(),
            now=_NOW,
        )
        assert decisions[0].severity == "medium"

    def test_object_accumulates_score(self) -> None:
        agg = _make_aggregator(
            policy=_default_policy(medium_score_termination_threshold=100.0)
        )
        agg.process_object_detection(
            _make_object_result("cell phone"),
            telemetry_event_id=uuid.uuid4(),
            now=_NOW,
        )
        assert agg.accumulated_score == 1.0

    def test_object_triggers_accumulated_termination(self) -> None:
        agg = _make_aggregator(
            policy=_default_policy(medium_score_termination_threshold=1.0)
        )
        decisions = agg.process_object_detection(
            _make_object_result("cell phone"),
            telemetry_event_id=uuid.uuid4(),
            now=_NOW,
        )
        acc_flags = [d for d in decisions if d.rule_code == RULE_ACCUMULATED_SCORE]
        assert len(acc_flags) == 1
        assert acc_flags[0].triggered_termination is True


# ===================================================================
# Book-detection severity
# ===================================================================


class TestBookSeverity:
    """Test book-detection severity resolution."""

    def test_closed_book_flags(self) -> None:
        assert should_flag_book("closed_book", {}) is True

    def test_open_book_no_flag(self) -> None:
        assert should_flag_book("open_book", {}) is False

    def test_specific_list_book_allowed(self) -> None:
        assert should_flag_book(
            "specific_list",
            {"allowed_items": ["book", "calculator"]},
        ) is False

    def test_specific_list_book_not_allowed(self) -> None:
        assert should_flag_book(
            "specific_list",
            {"allowed_items": ["calculator"]},
        ) is True

    def test_specific_list_empty(self) -> None:
        assert should_flag_book("specific_list", {}) is True

    def test_unknown_policy_flags(self) -> None:
        assert should_flag_book("something_unknown", {}) is True

    def test_book_detection_closed_book_session(self) -> None:
        """End-to-end: book detected in closed-book session → MEDIUM flag."""
        agg = _make_aggregator(
            context=_default_context(allowed_reference_materials="closed_book"),
        )
        decisions = agg.process_object_detection(
            _make_object_result("book"),
            telemetry_event_id=uuid.uuid4(),
            now=_NOW,
        )
        book_flags = [d for d in decisions if d.rule_code == BOOK_RULE_CODE]
        assert len(book_flags) == 1
        assert book_flags[0].severity == BOOK_FLAG_SEVERITY

    def test_book_detection_open_book_session(self) -> None:
        """Book detected in open-book session → no flag."""
        agg = _make_aggregator(
            context=_default_context(allowed_reference_materials="open_book"),
        )
        decisions = agg.process_object_detection(
            _make_object_result("book"),
            telemetry_event_id=uuid.uuid4(),
            now=_NOW,
        )
        assert decisions == []

    def test_book_specific_list_allowed(self) -> None:
        agg = _make_aggregator(
            context=_default_context(
                allowed_reference_materials="specific_list",
                permitted_material_details={"allowed_items": ["book"]},
            ),
        )
        decisions = agg.process_object_detection(
            _make_object_result("book"),
            telemetry_event_id=uuid.uuid4(),
            now=_NOW,
        )
        assert decisions == []

    def test_book_specific_list_not_allowed(self) -> None:
        agg = _make_aggregator(
            context=_default_context(
                allowed_reference_materials="specific_list",
                permitted_material_details={"allowed_items": ["calculator"]},
            ),
        )
        decisions = agg.process_object_detection(
            _make_object_result("book"),
            telemetry_event_id=uuid.uuid4(),
            now=_NOW,
        )
        book_flags = [d for d in decisions if d.rule_code == BOOK_RULE_CODE]
        assert len(book_flags) == 1


# ===================================================================
# Exemption suppression
# ===================================================================


class TestExemptionSuppression:
    """Test accommodation-exemption lookup and suppression."""

    def _make_exemption(
        self,
        object_class: str = "cell phone",
        **overrides: object,
    ) -> ExemptionRecord:
        defaults: dict[str, object] = {
            "id": uuid.uuid4(),
            "participant_id": _PARTICIPANT,
            "object_class": object_class,
            "exam_reference": _EXAM_REF,
            "effective_at": _NOW - timedelta(hours=1),
            "expires_at": None,
        }
        defaults.update(overrides)
        return ExemptionRecord(**defaults)  # type: ignore[arg-type]

    def test_matching_exemption(self) -> None:
        ex = self._make_exemption()
        result = find_matching_exemption(
            _PARTICIPANT, "cell phone", _EXAM_REF, _NOW, [ex],
        )
        assert result is ex

    def test_wrong_participant(self) -> None:
        ex = self._make_exemption(participant_id=uuid.uuid4())
        result = find_matching_exemption(
            _PARTICIPANT, "cell phone", _EXAM_REF, _NOW, [ex],
        )
        assert result is None

    def test_wrong_class(self) -> None:
        ex = self._make_exemption(object_class="laptop")
        result = find_matching_exemption(
            _PARTICIPANT, "cell phone", _EXAM_REF, _NOW, [ex],
        )
        assert result is None

    def test_wrong_exam(self) -> None:
        ex = self._make_exemption(exam_reference="other-exam")
        result = find_matching_exemption(
            _PARTICIPANT, "cell phone", _EXAM_REF, _NOW, [ex],
        )
        assert result is None

    def test_not_yet_effective(self) -> None:
        ex = self._make_exemption(effective_at=_NOW + timedelta(hours=1))
        result = find_matching_exemption(
            _PARTICIPANT, "cell phone", _EXAM_REF, _NOW, [ex],
        )
        assert result is None

    def test_expired(self) -> None:
        ex = self._make_exemption(
            effective_at=_NOW - timedelta(hours=2),
            expires_at=_NOW - timedelta(hours=1),
        )
        result = find_matching_exemption(
            _PARTICIPANT, "cell phone", _EXAM_REF, _NOW, [ex],
        )
        assert result is None

    def test_expires_at_boundary(self) -> None:
        """Expires exactly at ``now`` → expired (not active)."""
        ex = self._make_exemption(
            effective_at=_NOW - timedelta(hours=1),
            expires_at=_NOW,
        )
        result = find_matching_exemption(
            _PARTICIPANT, "cell phone", _EXAM_REF, _NOW, [ex],
        )
        assert result is None

    def test_no_expiry_stays_active(self) -> None:
        ex = self._make_exemption(expires_at=None)
        result = find_matching_exemption(
            _PARTICIPANT, "cell phone", _EXAM_REF, _NOW, [ex],
        )
        assert result is ex

    def test_empty_list(self) -> None:
        result = find_matching_exemption(
            _PARTICIPANT, "cell phone", _EXAM_REF, _NOW, [],
        )
        assert result is None

    def test_suppressed_flag_has_low_severity(self) -> None:
        """Object with matching exemption → severity downgraded to LOW."""
        ex = self._make_exemption(object_class="cell phone")
        agg = _make_aggregator(
            context=_default_context(exemptions=[ex]),
        )
        decisions = agg.process_object_detection(
            _make_object_result("cell phone"),
            telemetry_event_id=uuid.uuid4(),
            now=_NOW,
        )
        assert len(decisions) == 1
        assert decisions[0].severity == SUPPRESSED_SEVERITY
        assert decisions[0].suppressed_by_exemption_id == ex.id

    def test_suppressed_flag_does_not_accumulate(self) -> None:
        """A suppressed (LOW severity) flag does not add to accumulated score."""
        ex = self._make_exemption(object_class="cell phone")
        agg = _make_aggregator(
            context=_default_context(exemptions=[ex]),
        )
        agg.process_object_detection(
            _make_object_result("cell phone"),
            telemetry_event_id=uuid.uuid4(),
            now=_NOW,
        )
        assert agg.accumulated_score == 0.0

    def test_non_matching_exemption_no_suppression(self) -> None:
        """Exemption for a different class → normal MEDIUM flag."""
        ex = self._make_exemption(object_class="laptop")
        agg = _make_aggregator(
            context=_default_context(exemptions=[ex]),
        )
        decisions = agg.process_object_detection(
            _make_object_result("cell phone"),
            telemetry_event_id=uuid.uuid4(),
            now=_NOW,
        )
        assert decisions[0].severity == "medium"
        assert decisions[0].suppressed_by_exemption_id is None


# ===================================================================
# Package imports
# ===================================================================


class TestPackageExports:
    """Verify the fusion package re-exports the expected symbols."""

# ===========================================================================
# Turn 8: Identity Match
# ===========================================================================


class TestIdentityMatchPath:
    """Test the identity-mismatch path (zero-tolerance, like second-person)."""

    def test_match_breaks_streak(self) -> None:
        agg = _make_aggregator(
            policy=_default_policy(identity_confirmation_frames=3)
        )
        # 2 mismatch frames
        for _ in range(2):
            agg.process_identity_match(
                _make_identity_result(similarity=0.4),
                telemetry_event_id=uuid.uuid4(),
            )
        # 1 match frame
        agg.process_identity_match(
            _make_identity_result(similarity=0.9),
            telemetry_event_id=uuid.uuid4(),
        )
        # 1 more mismatch frame (streak broken, no fire)
        decisions = agg.process_identity_match(
            _make_identity_result(similarity=0.4),
            telemetry_event_id=uuid.uuid4(),
        )
        assert decisions == []

    def test_consecutive_mismatches_fire_critical(self) -> None:
        agg = _make_aggregator(
            policy=_default_policy(
                identity_confirmation_frames=3,
                identity_similarity_threshold=0.6,
            )
        )
        decisions: list[FlagDecision] = []
        for _ in range(3):
            decisions = agg.process_identity_match(
                _make_identity_result(similarity=0.4),
                telemetry_event_id=uuid.uuid4(),
            )
        assert len(decisions) == 1
        d = decisions[0]
        assert d.rule_code == RULE_IDENTITY_MISMATCH
        assert d.severity == "critical"
        assert d.triggered_termination is True
        assert len(d.contributing_event_ids) == 3

    def test_fires_only_once(self) -> None:
        agg = _make_aggregator(
            policy=_default_policy(identity_confirmation_frames=2)
        )
        for _ in range(2):
            agg.process_identity_match(
                _make_identity_result(similarity=0.2),
                telemetry_event_id=uuid.uuid4(),
            )
        # Next frame is also a mismatch, but we already fired
        decisions = agg.process_identity_match(
            _make_identity_result(similarity=0.2),
            telemetry_event_id=uuid.uuid4(),
        )
        assert decisions == []

    def test_interval_computation(self) -> None:
        agg = _make_aggregator(
            policy=_default_policy(identity_confirmation_frames=3)
        )
        # Scores: 0.3, 0.5, 0.4 (mean 0.4)
        scores = [0.3, 0.5, 0.4]
        decisions: list[FlagDecision] = []
        for s in scores:
            decisions = agg.process_identity_match(
                _make_identity_result(similarity=s),
                telemetry_event_id=uuid.uuid4(),
            )
        assert len(decisions) == 1
        interval = decisions[0].confidence
        assert interval.lower == 0.3
        assert interval.upper == 0.5
        assert interval.score == pytest.approx(0.4)


# ===========================================================================
# Turn 8: Audio VAD
# ===========================================================================


class TestAudioVadPath:
    """Test the audio anomaly path."""

    def test_silence_no_flag(self) -> None:
        agg = _make_aggregator(policy=_default_policy())
        decisions = agg.process_audio_vad(
            _make_audio_result(event_type="silence"),
            telemetry_event_id=uuid.uuid4(),
        )
        assert decisions == []
        assert agg.accumulated_score == 0.0

    def test_speech_detected_accumulates(self) -> None:
        agg = _make_aggregator(
            policy=_default_policy(medium_score_termination_threshold=10.0)
        )
        decisions = agg.process_audio_vad(
            _make_audio_result(event_type="speech_detected"),
            telemetry_event_id=uuid.uuid4(),
        )
        assert len(decisions) == 1
        d = decisions[0]
        assert d.rule_code == RULE_AUDIO_ANOMALY
        assert d.severity == "medium"
        assert d.triggered_termination is False
        assert d.score_delta == 1.0
        assert agg.accumulated_score == 1.0

    def test_elevated_rms_accumulates(self) -> None:
        agg = _make_aggregator(
            policy=_default_policy(
                medium_score_termination_threshold=10.0,
                score_weights={"audio_anomaly": 2.5},
            )
        )
        decisions = agg.process_audio_vad(
            _make_audio_result(event_type="elevated_rms"),
            telemetry_event_id=uuid.uuid4(),
        )
        assert len(decisions) == 1
        assert decisions[0].severity == "medium"
        assert decisions[0].score_delta == 2.5
        assert agg.accumulated_score == 2.5

    def test_audio_trips_threshold(self) -> None:
        agg = _make_aggregator(
            policy=_default_policy(
                medium_score_termination_threshold=1.5,
                medium_score_action=MediumScoreAction.AUTO_TERMINATE,
            )
        )
        decisions: list[FlagDecision] = []
        for _ in range(2):
            decisions = agg.process_audio_vad(
                _make_audio_result(event_type="speech_detected"),
                telemetry_event_id=uuid.uuid4(),
            )
        # The second call emits the medium flag AND the accumulated-score flag
        assert len(decisions) == 2
        assert decisions[0].severity == "medium"
        assert decisions[1].rule_code == RULE_ACCUMULATED_SCORE
        assert decisions[1].severity == "critical"
        assert decisions[1].triggered_termination is True
        from proctoring_engine.fusion import FlagDecision, GazeAwayEvent
        assert FlagDecision is not None
        assert GazeAwayEvent is not None

    def test_aggregator_importable(self) -> None:
        from proctoring_engine.fusion import (
            PolicySnapshot,
            SessionAggregator,
            SessionContext,
        )
        assert PolicySnapshot is not None
        assert SessionAggregator is not None
        assert SessionContext is not None

    def test_exemptions_importable(self) -> None:
        from proctoring_engine.fusion import (
            ExemptionRecord,
            find_matching_exemption,
        )
        assert ExemptionRecord is not None
        assert find_matching_exemption is not None

    def test_book_severity_importable(self) -> None:
        from proctoring_engine.fusion import (
            BOOK_FLAG_SEVERITY,
            BOOK_RULE_CODE,
            should_flag_book,
        )
        assert BOOK_FLAG_SEVERITY is not None
        assert BOOK_RULE_CODE is not None
        assert should_flag_book is not None

    def test_rule_constants_importable(self) -> None:
        from proctoring_engine.fusion import (
            RULE_ACCUMULATED_SCORE,
            RULE_AUDIO_ANOMALY,
            RULE_BROWSER_EVENT,
            RULE_GAZE_AWAY_FREQUENCY,
            RULE_IDENTITY_MISMATCH,
            RULE_LIVENESS_CHECK_FAILED,
            RULE_OBJECT_DETECTED,
            RULE_SECOND_PERSON,
        )
        assert RULE_SECOND_PERSON == "second_person"
        assert RULE_GAZE_AWAY_FREQUENCY == "gaze_away_frequency"
        assert RULE_ACCUMULATED_SCORE == "accumulated_score"
        assert RULE_OBJECT_DETECTED == "object_detected"
        assert RULE_BROWSER_EVENT == "browser_event"
        assert RULE_LIVENESS_CHECK_FAILED == "liveness_check_failed"
        assert RULE_IDENTITY_MISMATCH == "identity_mismatch"
        assert RULE_AUDIO_ANOMALY == "audio_anomaly"

# ===========================================================================
# Item 4: medium_score_action branching (auto_terminate vs flag_for_review)
# ===========================================================================


def _make_liveness_result(
    *,
    is_real: bool = False,
    confidence: float = 0.8,
) -> LivenessResult:
    return LivenessResult(
        modality="liveness",
        event_type="liveness_spoof" if not is_real else "liveness_real",
        confidence=ConfidenceInterval(
            lower=confidence, score=confidence, upper=confidence
        ),
        bounding_boxes=[],
        raw_value={},
        is_real=is_real,
    )


class TestMediumScoreActionBranching:
    """Item 4: the aggregator branches on ``medium_score_action``.

    AUTO_TERMINATE is the legacy default — the
    accumulated-score-over-threshold fires a CRITICAL flag with
    ``triggered_termination=True`` (unchanged behaviour).
    FLAG_FOR_REVIEW keeps the session live; the flag is CRITICAL but
    ``triggered_termination=False``, and a human reviewer decides.
    """

    def _trigger_accumulated_threshold(self) -> list[FlagDecision]:
        """Helper: drive an aggregator past its threshold with browser events."""
        agg = _make_aggregator(
            policy=_default_policy(
                medium_score_termination_threshold=2.0,
            )
        )
        # Two visibilitychange events at weight 1.0 each — past threshold
        decisions: list[FlagDecision] = []
        for _ in range(2):
            decisions.extend(
                agg.process_browser_event(
                    BrowserEventResult(
                        modality="browser",
                        event_type="visibilitychange",
                        confidence=_CI_POINT,
                        bounding_boxes=[],
                        raw_value={},
                        detail={},
                    ),
                    telemetry_event_id=uuid.uuid4(),
                )
            )
        return decisions

    def test_auto_terminate_still_terminates(self) -> None:
        """AUTO_TERMINATE preserves the legacy behaviour: triggered_termination=True."""
        agg = _make_aggregator(
            policy=_default_policy(
                medium_score_termination_threshold=2.0,
                medium_score_action=MediumScoreAction.AUTO_TERMINATE,
            )
        )
        all_decisions: list[FlagDecision] = []
        for _ in range(2):
            all_decisions.extend(
                agg.process_browser_event(
                    BrowserEventResult(
                        modality="browser",
                        event_type="visibilitychange",
                        confidence=_CI_POINT,
                        bounding_boxes=[],
                        raw_value={},
                        detail={},
                    ),
                    telemetry_event_id=uuid.uuid4(),
                )
            )
        acc_flags = [
            d for d in all_decisions
            if d.rule_code == RULE_ACCUMULATED_SCORE
        ]
        assert len(acc_flags) == 1
        assert acc_flags[0].triggered_termination is True
        assert acc_flags[0].severity == "critical"
        assert acc_flags[0].detail["medium_score_action"] == "auto_terminate"

    def test_flag_for_review_does_not_terminate(self) -> None:
        """FLAG_FOR_REVIEW: CRITICAL flag, triggered_termination=False."""
        agg = _make_aggregator(
            policy=_default_policy(
                medium_score_termination_threshold=2.0,
                medium_score_action=MediumScoreAction.FLAG_FOR_REVIEW,
            )
        )
        all_decisions: list[FlagDecision] = []
        for _ in range(2):
            all_decisions.extend(
                agg.process_browser_event(
                    BrowserEventResult(
                        modality="browser",
                        event_type="visibilitychange",
                        confidence=_CI_POINT,
                        bounding_boxes=[],
                        raw_value={},
                        detail={},
                    ),
                    telemetry_event_id=uuid.uuid4(),
                )
            )
        acc_flags = [
            d for d in all_decisions
            if d.rule_code == RULE_ACCUMULATED_SCORE
        ]
        assert len(acc_flags) == 1
        assert acc_flags[0].triggered_termination is False
        assert acc_flags[0].severity == "critical"
        assert acc_flags[0].detail["medium_score_action"] == "flag_for_review"

    def test_flag_for_review_session_still_processes(self) -> None:
        """After FLAG_FOR_REVIEW fires, the aggregator is NOT
        terminal — it can still process subsequent events.  (Compare
        with AUTO_TERMINATE, where ``Flag.triggered_termination=True``
        is the engine's kill-switch trigger.)
        """

        agg = _make_aggregator(
            policy=_default_policy(
                medium_score_termination_threshold=2.0,
                medium_score_action=MediumScoreAction.FLAG_FOR_REVIEW,
            )
        )
        # First two browser events trigger the flag-for-review path.
        for _ in range(2):
            agg.process_browser_event(
                BrowserEventResult(
                    modality="browser",
                    event_type="visibilitychange",
                    confidence=_CI_POINT,
                    bounding_boxes=[],
                    raw_value={},
                    detail={},
                ),
                telemetry_event_id=uuid.uuid4(),
            )

        # A subsequent event should still be processed (the
        # aggregator does not enter a terminal state on
        # FLAG_FOR_REVIEW — that's only AUTO_TERMINATE's role).
        later = agg.process_browser_event(
            BrowserEventResult(
                modality="browser",
                event_type="paste",
                confidence=_CI_POINT,
                bounding_boxes=[],
                raw_value={},
                detail={},
            ),
            telemetry_event_id=uuid.uuid4(),
        )
        assert len(later) >= 1
        # The new flag is the paste event (no accumulation)
        assert any(d.rule_code == RULE_BROWSER_EVENT for d in later)


# ===========================================================================
# Item 11: liveness / anti-spoofing modality
# ===========================================================================


class TestLivenessModality:
    """Item 11: the liveness / anti-spoofing pipeline.

    Catches print and screen-replay spoofing specifically — not
    deepfakes or 3D masks.  Branches on
    ``PolicyConfig.liveness_check_action``.
    """

    def test_disabled_by_default_returns_empty(self) -> None:
        """When liveness_check_enabled=False, no decisions are emitted
        even on spoof frames.  Defaults to disabled per the design
        doc — this is opt-in infrastructure."""
        agg = _make_aggregator(
            policy=_default_policy(liveness_check_enabled=False)
        )
        decisions = agg.process_liveness(
            _make_liveness_result(is_real=False),
            telemetry_event_id=uuid.uuid4(),
        )
        assert decisions == []

    def test_real_frame_no_flag(self) -> None:
        """A frame classified ``is_real=True`` does not raise a flag,
        regardless of action."""
        agg = _make_aggregator(
            policy=_default_policy(
                liveness_check_enabled=True,
                liveness_check_action=LivenessAction.CRITICAL_TERMINATE,
            )
        )
        decisions = agg.process_liveness(
            _make_liveness_result(is_real=True, confidence=0.95),
            telemetry_event_id=uuid.uuid4(),
        )
        assert decisions == []

    def test_critical_terminate_on_spoof_window(self) -> None:
        """``CRITICAL_TERMINATE``: spoof frames spanning the confirmation
        window raise a CRITICAL flag with ``triggered_termination=True``."""
        agg = _make_aggregator(
            policy=_default_policy(
                liveness_check_enabled=True,
                liveness_check_action=LivenessAction.CRITICAL_TERMINATE,
                liveness_confirmation_frames=3,
            )
        )
        decisions: list[FlagDecision] = []
        for _ in range(3):
            decisions = agg.process_liveness(
                _make_liveness_result(is_real=False, confidence=0.85),
                telemetry_event_id=uuid.uuid4(),
            )
        assert len(decisions) == 1
        d = decisions[0]
        assert d.rule_code == RULE_LIVENESS_CHECK_FAILED
        assert d.severity == "critical"
        assert d.triggered_termination is True
        assert d.detail["liveness_check_action"] == "critical_terminate"
        assert d.detail["window_size"] == 3
        # Degenerate confidence interval because all 3 were exactly 0.85
        assert d.confidence.score == 0.85
        assert d.confidence.lower == 0.85
        assert d.confidence.upper == 0.85

    def test_medium_accumulate_on_spoof_window(self) -> None:
        """``MEDIUM_ACCUMULATE``: spoof frames spanning the window raise
        a MEDIUM flag that contributes to the accumulated-score path."""
        agg = _make_aggregator(
            policy=_default_policy(
                liveness_check_enabled=True,
                liveness_check_action=LivenessAction.MEDIUM_ACCUMULATE,
                medium_score_termination_threshold=3.0,
                liveness_confirmation_frames=3,
            )
        )
        decisions: list[FlagDecision] = []
        for _ in range(3):
            decisions = agg.process_liveness(
                _make_liveness_result(is_real=False, confidence=0.8),
                telemetry_event_id=uuid.uuid4(),
            )
        assert len(decisions) == 1
        d = decisions[0]
        assert d.rule_code == RULE_LIVENESS_CHECK_FAILED
        assert d.severity == "medium"
        assert d.triggered_termination is False
        assert d.score_delta == 1.0
        assert d.detail["liveness_check_action"] == "medium_accumulate"

    def test_liveness_confidence_interval_spread(self) -> None:
        """The flag's confidence interval reflects the min, max, and mean
        of the raw confidence scores over the confirmation window."""
        agg = _make_aggregator(
            policy=_default_policy(
                liveness_check_enabled=True,
                liveness_check_action=LivenessAction.CRITICAL_TERMINATE,
                liveness_confirmation_frames=3,
            )
        )
        decisions: list[FlagDecision] = []
        scores = [0.8, 0.9, 0.7]
        for score in scores:
            decisions = agg.process_liveness(
                _make_liveness_result(is_real=False, confidence=score),
                telemetry_event_id=uuid.uuid4(),
            )
        assert len(decisions) == 1
        interval = decisions[0].confidence
        assert interval.lower == 0.7
        assert interval.upper == 0.9
        assert interval.score == sum(scores) / len(scores)

    def test_real_frame_resets_window(self) -> None:
        """A single ``is_real=True`` frame breaks the spoof streak; the
        window must start over."""
        agg = _make_aggregator(
            policy=_default_policy(
                liveness_check_enabled=True,
                liveness_check_action=LivenessAction.CRITICAL_TERMINATE,
                liveness_confirmation_frames=3,
            )
        )
        # 2 spoof frames
        for _ in range(2):
            agg.process_liveness(
                _make_liveness_result(is_real=False),
                telemetry_event_id=uuid.uuid4(),
            )
        # 1 real frame
        agg.process_liveness(
            _make_liveness_result(is_real=True),
            telemetry_event_id=uuid.uuid4(),
        )
        # 1 more spoof frame (should not trigger, window was broken)
        decisions = agg.process_liveness(
            _make_liveness_result(is_real=False),
            telemetry_event_id=uuid.uuid4(),
        )
        assert decisions == []

    def test_medium_accumulate_trips_threshold(self) -> None:
        """After three MEDIUM_ACCUMULATE liveness events (each at the
        default weight 1.0), the third one should also raise the
        accumulated-score CRITICAL flag — same shape as the
        browser-event / gaze-away-frequency accumulation."""
        agg = _make_aggregator(
            policy=_default_policy(
                liveness_check_enabled=True,
                liveness_check_action=LivenessAction.MEDIUM_ACCUMULATE,
                medium_score_termination_threshold=3.0,
                medium_score_action=MediumScoreAction.AUTO_TERMINATE,
                liveness_confirmation_frames=2, # make the test faster
            )
        )

        all_decisions: list[FlagDecision] = []
        # Three flag cycles (each takes 2 spoof frames)
        for _ in range(6):
            all_decisions.extend(
                agg.process_liveness(
                    _make_liveness_result(is_real=False),
                    telemetry_event_id=uuid.uuid4(),
                )
            )

        liveness_flags = [
            d for d in all_decisions
            if d.rule_code == RULE_LIVENESS_CHECK_FAILED
        ]
        assert len(liveness_flags) == 3
        acc_flags = [
            d for d in all_decisions
            if d.rule_code == RULE_ACCUMULATED_SCORE
        ]
        assert len(acc_flags) == 1
        assert acc_flags[0].triggered_termination is True

    def test_threshold_below_real_score_returns_real(self) -> None:
        """Borderline frame: high confidence but still marked spoof
        by the runner.  Aggregator treats the spoof frame the same as
        any other spoof — the threshold is applied at the runner, not
        the aggregator.  Threshold semantics are tested in
        ``test_inference.py`` for the runner side."""

        agg = _make_aggregator(
            policy=_default_policy(
                liveness_check_enabled=True,
                liveness_check_action=LivenessAction.CRITICAL_TERMINATE,
                liveness_confirmation_frames=2,
            )
        )
        # Push 2 spoof frames with low confidence; the window is full
        # of spoof frames so we should fire.
        decisions: list[FlagDecision] = []
        for _ in range(2):
            decisions = agg.process_liveness(
                _make_liveness_result(is_real=False, confidence=0.49),
                telemetry_event_id=uuid.uuid4(),
            )
        assert len(decisions) == 1
        assert decisions[0].severity == "critical"
