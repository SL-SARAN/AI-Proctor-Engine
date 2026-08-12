"""Modality-keyed sampling scheduler for the preprocessing layer.

The ingestion layer feeds heavy frames and audio chunks into the
queue at fixed cadences (heavy every 2-3 s, audio on its own stream).
Three video *inference* modalities need different rates relative to
that incoming heavy-frame cadence:

- **Head pose / gaze** runs on *every* heavy frame — needs enough
  samples for the 800 ms minimum-duration aggregation
  (``docs/proctoring-engine-v1-spec.md`` §3.1) to have any chance
  of catching a sustained off-screen gaze.
- **Object detection** runs on every heavy frame too — a phone that
  appears for less than 2-3 s would be missed at a lower rate.
- **Identity match** is the heaviest per-call cost and *identity
  doesn't change frame-to-frame*, so it runs only every N heavy
  frames (``identity_match_period``).

Browser events and lightweight face-presence have no server-side
scheduler because they're client-driven, not server-invoked.

This module is intentionally **stateless**: it is given an incoming
sequence number and returns the decision for that step, without
internal mutable state.  Callers that want different cadence shapes
per session construct a :class:`ModalityScheduler` per-session.
The scheduler only knows about counts; it doesn't track wall-clock
time.  Wall-clock-aware scheduling belongs to the higher-level
orchestration loop (which already has policy-config-driven
heterogeneity, see ``PolicyConfig``).

**Calibration note** (from design doc §2): "this is a real tuning
question, not a fixed constant."  The defaults below are sane
starting points; they are deliberately exposed through constructor
arguments so a deployment can tune them against real latency
measurements without subclassing the scheduler.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Final, Literal, Optional


# ---------------------------------------------------------------------------
# Modalities the scheduler cares about
# ---------------------------------------------------------------------------

class InferenceModality(str, enum.Enum):
    """The server-side inference modalities driven by heavy frames."""

    HEAD_POSE_GAZE = "head_pose_gaze"
    OBJECT_DETECTION = "object_detection"
    IDENTITY_MATCH = "identity_match"
    LIVENESS = "liveness"


class ScheduleDecision(str, enum.Enum):
    """Whether to invoke an inference module on this frame."""

    RUN = "run"
    SKIP = "skip"


# ---------------------------------------------------------------------------
# Decision container
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ModalityDecision:
    """A single (modality, decision) entry."""

    modality: InferenceModality
    decision: ScheduleDecision
    reason: str


@dataclass(frozen=True, slots=True)
class InferenceDecision:
    """The full per-frame decision: which modalities to run."""

    frame_seq: int
    decisions: list[ModalityDecision] = field(default_factory=list)

    def modalities_to_run(self) -> list[InferenceModality]:
        """Convenience accessor — only the modalities that should run."""
        return [
            d.modality for d in self.decisions
            if d.decision is ScheduleDecision.RUN
        ]

    def modalities_to_skip(self) -> list[InferenceModality]:
        """Modalities explicitly skipped on this frame."""
        return [
            d.modality for d in self.decisions
            if d.decision is ScheduleDecision.SKIP
        ]

    def should_run(self, modality: InferenceModality) -> bool:
        """True if the given modality should be invoked this frame."""
        for d in self.decisions:
            if d.modality is modality:
                return d.decision is ScheduleDecision.RUN
        return False


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

# Default cadence multipliers — plain module constants so they are
# usable as function-signature defaults (slotted-dataclass class
# attributes are descriptors, not values).
_DEFAULT_HEAD_POSE_PERIOD: Final[int] = 1        # Every heavy frame.
_DEFAULT_OBJECT_DETECTION_PERIOD: Final[int] = 1  # Every heavy frame.
_DEFAULT_IDENTITY_MATCH_PERIOD: Final[int] = 5    # Every 5th heavy frame.
_DEFAULT_LIVENESS_PERIOD: Final[int] = 5          # Same as identity match.


class ModalityScheduler:
    """Stateless per-session scheduler for heavy-frame inference ordering.

    The scheduler wraps independent "every Nth-frame" counters
    (one per modality).  It is constructed once per session with
    the desired N values, then used by calling
    :meth:`decide_for_frame` on each arriving heavy-frame sequence
    number.

    Parameters
    ----------
    head_pose_period:
        Run head-pose / gaze every ``head_pose_period`` heavy frames.
        Default 1 (every frame).
    object_detection_period:
        Run object detection every ``object_detection_period`` heavy
        frames.  Default 1 (every frame).
    identity_match_period:
        Run identity match every ``identity_match_period`` heavy frames.
        Default 5 — five frames at the 2-3 s cadence is roughly 10-15 s
        between identity checks, which is plenty for detecting an
        identity swap.
    liveness_period:
        Run liveness anti-spoofing check.  Default matches identity check
        since it uses the same face crop.
    """

    def __init__(
        self,
        *,
        head_pose_period: int = _DEFAULT_HEAD_POSE_PERIOD,
        object_detection_period: int = _DEFAULT_OBJECT_DETECTION_PERIOD,
        identity_match_period: int = _DEFAULT_IDENTITY_MATCH_PERIOD,
        liveness_period: int = _DEFAULT_LIVENESS_PERIOD,
    ) -> None:
        if head_pose_period <= 0:
            raise ValueError("head_pose_period must be a positive integer.")
        if object_detection_period <= 0:
            raise ValueError("object_detection_period must be a positive integer.")
        if identity_match_period <= 0:
            raise ValueError("identity_match_period must be a positive integer.")
        if liveness_period <= 0:
            raise ValueError("liveness_period must be a positive integer.")

        self._periods = {
            InferenceModality.HEAD_POSE_GAZE: head_pose_period,
            InferenceModality.OBJECT_DETECTION: object_detection_period,
            InferenceModality.IDENTITY_MATCH: identity_match_period,
            InferenceModality.LIVENESS: liveness_period,
        }

    @property
    def periods(self) -> dict[InferenceModality, int]:
        """The configured period for each modality (read-only snapshot)."""
        # Return a fresh dict so callers can't mutate internals.
        return dict(self._periods)

    def period_for(self, modality: InferenceModality) -> int:
        """Return the cadence period for a single modality."""
        return self._periods[modality]

    def decide_for_frame(self, frame_seq: int) -> InferenceDecision:
        """Return the per-frame decision for the given heavy-frame sequence.

        ``frame_seq`` is the 0-based index of the heavy frame within
        the session.  A frame period of 1 means "every frame" — the
        scheduler returns ``RUN`` for every seq.  A period of 5 means
        "run on frame seq 0, 5, 10, ...".
        """

        if frame_seq < 0:
            raise ValueError("frame_seq must be non-negative.")

        decisions: list[ModalityDecision] = []
        for modality in (
            InferenceModality.HEAD_POSE_GAZE,
            InferenceModality.OBJECT_DETECTION,
            InferenceModality.IDENTITY_MATCH,
            InferenceModality.LIVENESS,
        ):
            period = self._periods[modality]
            if frame_seq % period == 0:
                decisions.append(
                    ModalityDecision(
                        modality=modality,
                        decision=ScheduleDecision.RUN,
                        reason=f"frame_seq {frame_seq} is a multiple of "
                        f"period {period}",
                    )
                )
            else:
                decisions.append(
                    ModalityDecision(
                        modality=modality,
                        decision=ScheduleDecision.SKIP,
                        reason=f"frame_seq {frame_seq} is not a multiple "
                        f"of period {period}",
                    )
                )
        return InferenceDecision(frame_seq=frame_seq, decisions=decisions)
