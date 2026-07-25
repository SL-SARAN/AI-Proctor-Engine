"""Fusion & flagging engine package.

Turns typed ``InferenceResult`` objects from the inference layer into
``FlagDecision`` objects that the orchestration layer persists as
immutable ``Flag`` rows.

The ``SessionAggregator`` is the core: one instance per active
``ExamSession``, holding rolling-window counters and escalation
state.  It implements the three termination paths from
``docs/05-fusion-flagging-engine-design.md``:

- **Path 1 — zero-tolerance:** second-person confirmation counter.
- **Path 2 — gaze-away ladder:** Stage-2 event merging + rolling
  window + warning/termination limits.
- **Path 3 — accumulated score:** weighted MEDIUM increments
  against a configurable threshold.

Plus: accommodation-exemption suppression and book-detection
severity resolution.

Modules:

- :mod:`~proctoring_engine.fusion._types` — ``GazeAwayEvent``,
  ``FlagDecision``.
- :mod:`~proctoring_engine.fusion.aggregator` — ``SessionAggregator``,
  ``PolicySnapshot``, ``SessionContext``.
- :mod:`~proctoring_engine.fusion.exemptions` — ``ExemptionRecord``,
  ``find_matching_exemption``.
- :mod:`~proctoring_engine.fusion.book_severity` — ``should_flag_book``.
"""

# --- Shared types ---
from proctoring_engine.fusion._types import (
    FlagDecision,
    GazeAwayEvent,
)

# --- Aggregator ---
from proctoring_engine.fusion.aggregator import (
    RULE_ACCUMULATED_SCORE,
    RULE_BROWSER_EVENT,
    RULE_GAZE_AWAY_FREQUENCY,
    RULE_OBJECT_DETECTED,
    RULE_SECOND_PERSON,
    PolicySnapshot,
    SessionAggregator,
    SessionContext,
)

# --- Exemptions ---
from proctoring_engine.fusion.exemptions import (
    SUPPRESSED_SEVERITY,
    ExemptionRecord,
    find_matching_exemption,
)

# --- Book severity ---
from proctoring_engine.fusion.book_severity import (
    BOOK_FLAG_SEVERITY,
    BOOK_RULE_CODE,
    should_flag_book,
)

__all__ = [
    # Types
    "FlagDecision",
    "GazeAwayEvent",
    # Aggregator
    "RULE_ACCUMULATED_SCORE",
    "RULE_BROWSER_EVENT",
    "RULE_GAZE_AWAY_FREQUENCY",
    "RULE_OBJECT_DETECTED",
    "RULE_SECOND_PERSON",
    "PolicySnapshot",
    "SessionAggregator",
    "SessionContext",
    # Exemptions
    "SUPPRESSED_SEVERITY",
    "ExemptionRecord",
    "find_matching_exemption",
    # Book severity
    "BOOK_FLAG_SEVERITY",
    "BOOK_RULE_CODE",
    "should_flag_book",
]
