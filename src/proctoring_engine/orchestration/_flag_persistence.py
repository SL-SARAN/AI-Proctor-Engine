"""Flag row persistence — translate a :class:`FlagDecision` into immutable rows.

The fusion engine (:mod:`proctoring_engine.fusion`) is a stateless
per-session state machine that emits
:class:`proctoring_engine.fusion._types.FlagDecision` objects.  This
module is the **only** place in the codebase that turns those
decisions into persisted ``Flag`` + ``FlagTelemetryEvent`` rows.

The hard rules this function preserves (per
:mod:`CLAUDE.md` §"Hard rules" and
:mod:`SKILLS_ALIGNMENT.md` §3.4):

* ``Flag`` rows are **append-only** — the ``flag_immutable`` PostgreSQL
  trigger (added by the audit-reconciliation migration
  ``20260718_0002``) and the ORM listener
  (:func:`proctoring_engine.models.reject_flag_update`) both reject
  ``UPDATE`` and ``DELETE`` on the row.  This function INSERTs once,
  commits once, and never touches the row again.
* Contributing ``TelemetryEvent`` IDs are preserved via the
  ``FlagTelemetryEvent`` join table; the composite primary key
  ``(flag_id, telemetry_event_id)`` enforces the no-duplicate-link
  invariant.
* ``ExamSession.accumulated_medium_score`` is incremented by exactly
  ``decision.score_delta``; the ORM validator
  (:func:`proctoring_engine.models.validate_accumulated_medium_score`)
  enforces the non-negative invariant.
* If the function fails between "Flag inserted" and "commit," the
  transaction rolls back — the fusion engine's ``FlagDecision`` is the
  only recoverable state.

This function does **not** fire the kill-switch.  The kill-switch is
the route handler's job (``POST /sessions/{id}/terminate`` is the
*only* way to terminate a session, per
:mod:`docs/07-api-orchestration-design.md` §"What this layer
deliberately doesn't own").
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from proctoring_engine.fusion._types import FlagDecision
from proctoring_engine.inference._types import ConfidenceInterval
from proctoring_engine.models import (
    ExamSession,
    Flag,
    FlagSeverity,
    FlagTelemetryEvent,
)


class FlagPersistenceError(Exception):
    """Raised when a :class:`FlagDecision` cannot be persisted.

    Carries the underlying :class:`IntegrityError` so the route
    handler can map the failure to the right HTTP status (a duplicate
    link is 409, not 500).
    """

    def __init__(
        self, message: str, *, original: Exception | None = None
    ) -> None:
        super().__init__(message)
        self.original = original


#: Sentinel score-delta at-or-below which we don't touch the running
#: ``accumulated_medium_score`` accumulator.  Avoids float-precision
#: drift on no-op decisions (e.g. a CRITICAL flag with
#: ``score_delta == 0.0``).
_NO_SCORE_DELTA: Final[float] = 0.0


def _to_severity(value: str) -> FlagSeverity:
    """Resolve a string severity (per :class:`FlagSeverity` enum) to the
    enum member.

    Raises :class:`FlagPersistenceError` if ``value`` is not a valid
    :class:`FlagSeverity`.  The string is what
    :attr:`FlagDecision.severity` carries — fusion never holds an ORM
    enum, only its string value.
    """

    try:
        return FlagSeverity(value)
    except ValueError as exc:
        raise FlagPersistenceError(
            f"unknown severity {value!r} on FlagDecision"
        ) from exc


def _confidence_valid(interval: ConfidenceInterval) -> None:
    """Defensive assert that a :class:`ConfidenceInterval` is in range.

    The fusion engine already enforces the ``[0,1]`` bounds, but the
    defense here is the audit trail — a corrupted
    :class:`ConfidenceInterval` should *not* reach the database even
    if the upstream invariant were silently broken.
    """

    if not 0.0 <= interval.lower <= interval.score <= interval.upper <= 1.0:
        raise FlagPersistenceError(
            "confidence interval out of bounds: "
            f"lower={interval.lower}, score={interval.score}, "
            f"upper={interval.upper}"
        )


def persist_flag_decision(
    db: Session,
    decision: FlagDecision,
    *,
    exam_session: ExamSession,
    rule_code: str | None = None,
) -> Flag:
    """Insert a :class:`Flag` row from a :class:`FlagDecision`.

    Parameters
    ----------
    db:
        The SQLAlchemy session for the active transaction.
    decision:
        The fused decision emitted by
        :class:`proctoring_engine.fusion.aggregator.SessionAggregator`.
    exam_session:
        The :class:`ExamSession` the flag belongs to.  Must be the
        same session the aggregator's :class:`SessionContext` was
        built against — cross-session flag links are an audit bug.
    rule_code:
        Optional override for ``decision.rule_code``.  Used by the
        admin review path to attach a flag row to an existing
        :class:`FlagTelemetryEvent` cluster.

    Returns
    -------
    Flag:
        The freshly-INSERTed :class:`Flag` instance (append-only
        after this point).

    Raises
    ------
    FlagPersistenceError:
        The decision could not be persisted (unknown severity,
        out-of-bounds confidence, integrity violation, etc.).
    """

    severity = _to_severity(decision.severity)
    _confidence_valid(decision.confidence)
    code = rule_code or decision.rule_code

    flag = Flag(
        exam_session_id=exam_session.id,
        policy_config_id=exam_session.policy_config_id,
        rule_code=code,
        severity=severity,
        # Default status is RAISED; downstream services may push a
        # CONFIRMED flag through a separate decision row, never by
        # mutating this column.  The immutability triggers would
        # reject the UPDATE anyway.
        confidence_score=decision.confidence.score,
        confidence_lower=decision.confidence.lower,
        confidence_upper=decision.confidence.upper,
        triggered_termination=decision.triggered_termination,
        suppressed_by_exemption_id=decision.suppressed_by_exemption_id,
        detail=dict(decision.detail),
    )
    db.add(flag)
    try:
        db.flush()  # populate flag.id before the FlagTelemetryEvent links
    except IntegrityError as exc:
        db.rollback()
        raise FlagPersistenceError(
            "Flag insert violated an integrity constraint", original=exc
        ) from exc

    # Ordered links preserve the temporal sequence the fusion engine
    # recorded.  The composite PK guards against duplicates.
    for position, telemetry_id in enumerate(decision.contributing_event_ids):
        link = FlagTelemetryEvent(
            flag_id=flag.id,
            telemetry_event_id=telemetry_id,
            position=position,
        )
        db.add(link)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise FlagPersistenceError(
            "FlagTelemetryEvent link violated a uniqueness / FK constraint",
            original=exc,
        ) from exc

    # Path-3 accumulator.  The SQL ``Numeric(10,4)`` column carries
    # the running total; the ORM validator enforces non-negative.
    if decision.score_delta > _NO_SCORE_DELTA:
        exam_session.accumulated_medium_score = (
            float(exam_session.accumulated_medium_score or 0.0)
            + float(decision.score_delta)
        )

    db.commit()
    db.refresh(flag)
    return flag


def assert_flag_present(db: Session, flag_id: uuid.UUID) -> Flag:
    """Return the :class:`Flag` row with ``id == flag_id`` or raise.

    ``Flag`` rows are append-only, so loading one is always safe.
    Used by the admin review path to confirm the flag exists *before*
    inserting the :class:`ProctorReview` row.

    Raises
    ------
    FlagPersistenceError:
        The flag does not exist (a 404 at the route layer).
    """

    flag = db.get(Flag, flag_id)
    if flag is None:
        raise FlagPersistenceError(f"Flag {flag_id!s} does not exist")
    return flag


__all__ = [
    "FlagPersistenceError",
    "assert_flag_present",
    "persist_flag_decision",
]
