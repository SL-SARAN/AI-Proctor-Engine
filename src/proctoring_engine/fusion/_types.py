"""Shared types for the fusion & flagging engine.

The fusion engine sits between the inference layer (stateless per-frame
classifiers) and the persistence layer (immutable ``Flag`` rows in
Postgres).  These types are the internal vocabulary used by the
``SessionAggregator`` — they are *not* ORM models.

``FlagDecision`` is the primary output: it carries all the fields
needed to construct a ``Flag`` row *plus* the list of contributing
``TelemetryEvent`` IDs (for the ``FlagTelemetryEvent`` join table) and
the delta to apply to ``ExamSession.accumulated_medium_score`` (for
Path 3).  The orchestration layer is responsible for turning a
``FlagDecision`` into actual DB rows and, if
``triggered_termination=True``, firing the kill-switch.

``GazeAwayEvent`` is the Stage-2 aggregate produced when consecutive
``off_screen`` frames persist past ``gaze_min_duration_ms``
(``docs/proctoring-engine-v1-spec.md`` §3.1).  It is never persisted
directly; it exists as working state inside the ``SessionAggregator``
and participates in the rolling-window count that drives the gaze
escalation ladder.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from proctoring_engine.inference._types import ConfidenceInterval


# ---------------------------------------------------------------------------
# GazeAwayEvent — Stage-2 aggregate (not persisted)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class GazeAwayEvent:
    """A single uninterrupted off-screen episode.

    Built by the gaze state machine inside the ``SessionAggregator``
    from consecutive ``HeadPoseGazeResult(off_screen=True)`` frames
    whose total duration exceeds ``gaze_min_duration_ms``.

    Attributes
    ----------
    started_at_ms:
        Monotonic timestamp (in milliseconds) of the first
        ``off_screen`` frame in this episode.
    ended_at_ms:
        Monotonic timestamp of the last ``off_screen`` frame before
        the student returned on-screen (or the most recent frame if
        the episode is still active at the time of creation).
    contributing_event_ids:
        ``TelemetryEvent.id`` values that contributed to this episode.
    """

    started_at_ms: int
    ended_at_ms: int
    contributing_event_ids: tuple[uuid.UUID, ...] = ()


# ---------------------------------------------------------------------------
# FlagDecision — the aggregator's output
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FlagDecision:
    """A fully resolved flag decision emitted by the aggregator.

    The orchestration layer reads these and:

    1. Creates a ``Flag`` row (append-only, immutable after creation).
    2. Creates ``FlagTelemetryEvent`` rows for each contributing event.
    3. If ``triggered_termination``, fires the kill-switch.
    4. If ``score_delta > 0``, adds ``score_delta`` to
       ``ExamSession.accumulated_medium_score``.

    All threshold references are baked in by the aggregator from the
    session's ``PolicyConfig`` snapshot — the orchestration layer does
    not re-evaluate policy.
    """

    # --- Flag row fields ---
    rule_code: str
    severity: str  # matches ``FlagSeverity`` enum value
    confidence: ConfidenceInterval
    triggered_termination: bool = False
    suppressed_by_exemption_id: uuid.UUID | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    # --- Evidence trail ---
    contributing_event_ids: tuple[uuid.UUID, ...] = ()

    # --- Path 3 side-effect ---
    score_delta: float = 0.0
