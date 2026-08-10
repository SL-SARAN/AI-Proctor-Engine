"""Session lifecycle state machine.

The transition table is documented in :mod:`docs/07-api-orchestration-design.md`
§2 and :mod:`docs/proctoring-engine-v1-spec.md` §"Termination policy".
The rules:

- ``pending`` → ``active`` happens on the first successful WebSocket
  handshake (already implemented in the WebSocket layer — this module
  does **not** drive that transition; the WebSocket handler does the
  PENDING→ACTIVE move directly to avoid a circular dependency).
- ``active`` → ``terminated`` is the only transition the fusion
  engine can trigger automatically; it's also the path admins use
  to end a session manually.
- ``terminated`` → ``under_review`` is allowed (auto-terminations
  should be reviewed, not just disputed ones).
- ``under_review`` is **terminal from the engine's perspective** —
  every other transition out is rejected.
- All other transitions not listed in :data:`_ALLOWED` are rejected
  at the state-machine layer and tested as such.

This module is the **single source of truth** for transitions on
``ExamSession.status`` that didn't already happen in the WebSocket
handshake (PENDING→ACTIVE).  Keeping the rules here means a future
move of the WebSocket handler can't silently widen the transition
table.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy.orm import Session

from proctoring_engine.models import ExamSession, SessionStatus


#: Closed transition table.  Keys are the *current* status; values are
#: the set of statuses the engine may move that session to.  Adding a
#: row here is a schema- and audit-meaningful change that must be
#: reflected in :mod:`docs/07-api-orchestration-design.md` §2 and the
#: integration test suite.
_ALLOWED: Final[dict[SessionStatus, frozenset[SessionStatus]]] = {
    SessionStatus.PENDING: frozenset(
        {SessionStatus.ACTIVE, SessionStatus.TERMINATED}
    ),
    SessionStatus.ACTIVE: frozenset(
        {
            SessionStatus.COMPLETED,
            SessionStatus.TERMINATED,
            SessionStatus.UNDER_REVIEW,
        }
    ),
    SessionStatus.COMPLETED: frozenset({SessionStatus.UNDER_REVIEW}),
    # TERMINATED → REINSTATED is the fast-track-undo path for the
    # accumulated-score termination (``05-fusion-flagging-engine-design.md``
    # §Path 3): when ``medium_score_action=auto_terminate`` fires, a
    # live proctor can immediately reinstate via the existing
    # ``ProctorReview`` overturn path rather than waiting for a full
    # review cycle.  This is the one new transition added in turn N+12
    # (item 4); ``REINSTATED → UNDER_REVIEW`` is also allowed so the
    # same session can still be put under review if the overturn was
    # wrong.
    SessionStatus.TERMINATED: frozenset(
        {SessionStatus.UNDER_REVIEW, SessionStatus.REINSTATED}
    ),
    SessionStatus.REINSTATED: frozenset({SessionStatus.UNDER_REVIEW}),
    # UNDER_REVIEW is terminal from the engine's perspective.
    SessionStatus.UNDER_REVIEW: frozenset(),
}


class InvalidSessionTransition(Exception):
    """Raised when a state transition is not in :data:`_ALLOWED`.

    The route handler maps this to HTTP 409 with the closed error
    code ``invalid_session_transition`` (see
    :mod:`proctoring_engine.orchestration._routes`).
    """

    def __init__(self, current: SessionStatus, target: SessionStatus) -> None:
        super().__init__(
            f"transition {current.value!r} -> {target.value!r} is not allowed"
        )
        self.current = current
        self.target = target


def can_transition(
    current: SessionStatus, target: SessionStatus
) -> bool:
    """Return ``True`` iff ``current → target`` is an allowed transition.

    Pure function over the ``SessionStatus`` enum; consults the
    :data:`_ALLOWED` table.  No side effects; safe to call from
    composition paths where the actual move is conditional.
    """

    return target in _ALLOWED.get(current, frozenset())


def assert_transition(
    current: SessionStatus, target: SessionStatus
) -> None:
    """Raise :class:`InvalidSessionTransition` if the move is not allowed.

    Use this when the caller has already decided the move is going to
    happen and needs a fail-closed guard against a stale status read.
    """

    if not can_transition(current, target):
        raise InvalidSessionTransition(current, target)


def apply_transition(
    exam_session: ExamSession,
    target: SessionStatus,
    *,
    db: Session,
) -> None:
    """Move ``exam_session.status`` to ``target`` and commit.

    Asserts the transition is allowed *before* mutating the ORM
    field, so a rejected move raises on a stale read rather than
    leaving a partial state in the session.  Commits on success;
    the caller's transaction is the unit of work.

    The validation is purely in-memory; the SQL-level ``session_status``
    enum constraint already rejects values outside the enum
    regardless of what the application does.
    """

    assert_transition(exam_session.status, target)
    exam_session.status = target
    db.commit()


def allowed_targets(current: SessionStatus) -> frozenset[SessionStatus]:
    """Return the set of statuses reachable from ``current``.

    Useful for tests, admin tooling, and observability hooks that
    want to inspect the state machine without mutating anything.
    """

    return _ALLOWED.get(current, frozenset())


__all__ = [
    "InvalidSessionTransition",
    "allowed_targets",
    "apply_transition",
    "assert_transition",
    "can_transition",
]
