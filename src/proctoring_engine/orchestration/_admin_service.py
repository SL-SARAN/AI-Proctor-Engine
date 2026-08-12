"""Admin service functions — pure over a :class:`Session` + typed inputs.

Four operations, one module:

* :func:`create_policy_version` — INSERT a new ``PolicyConfig`` row,
  optionally retire the previous active version of the same ``name``
  in the same transaction.  Implements the "versioned snapshot"
  contract from :mod:`docs/01-data-models-design.md`:
  ``PolicyConfig.name`` is the natural key, ``version`` increments
  monotonically, the active version is selected by
  ``name + is_active=True + retired_at IS NULL``.  A mid-semester
  policy change therefore cannot retroactively alter the rules under
  which an already-completed session is interpreted.
* :func:`create_exemption` — INSERT a new
  ``AccommodationExemption`` row; ``approved_by_admin_id`` is set
  from the calling :class:`AdminUser`.
* :func:`list_flags_for_session` — SELECT every ``Flag`` for a
  session, joined with their ``EvidenceArtifact`` and ``ProctorReview``
  rows so the reviewer sees whether a flag has already been
  reviewed.  Optionally excludes suppressed flags.
* :func:`record_proctor_review` — INSERT a ``ProctorReview`` row
  (append-only; the existing ``Flag`` row is never mutated).  If
  ``decision == UPHELD`` and the session is in ``ACTIVE`` or
  ``TERMINATED``, transitions the session to ``UNDER_REVIEW``.

The services are **pure over a :class:`Session`** — they neither
commit nor close the session (the route handler is the unit of
work).  SQL-level constraints catch the impossible cases
(``gaze_warning_limit <= gaze_termination_limit``, the
``ck_policy_gaze_min_duration_within_window`` check, etc.); the
service-layer validation here mirrors those checks so the route
returns a useful 422 *before* the constraint violation bubbles up as
an :class:`IntegrityError`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from proctoring_engine.models import (
    AccommodationExemption,
    AdminUser,
    ExamSession,
    Flag,
    PolicyConfig,
    ProctorReview,
    SessionStatus,
)
from proctoring_engine.orchestration._schemas import (
    AccommodationExemptionResponse,
    CreateExemptionRequest,
    CreatePolicyConfigRequest,
    FlagReviewResponse,
    PolicyConfigResponse,
)
from proctoring_engine.orchestration._state_machine import (
    InvalidSessionTransition,
    assert_transition,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AdminServiceError(Exception):
    """Base class for the admin service error surface.

    The route handler maps each subclass to a deterministic HTTP
    status (see :mod:`proctoring_engine.orchestration._routes`).
    """


class PolicyVersioningError(AdminServiceError):
    """Raised when a policy row would violate the version invariants."""


class ExemptionValidationError(AdminServiceError):
    """Raised when an exemption row would violate the schema invariants."""


class FlagNotFoundError(AdminServiceError):
    """Raised when the referenced :class:`Flag` row does not exist."""


class ReviewTransitionError(AdminServiceError):
    """Raised when a review cannot be applied (state-machine reject)."""


# ---------------------------------------------------------------------------
# Internal validation helpers
# ---------------------------------------------------------------------------


def _validate_policy_request(request: CreatePolicyConfigRequest) -> None:
    """Raise :class:`PolicyVersioningError` on a request that would
    fail a SQL-level constraint.

    The SQL constraints on ``PolicyConfig`` already enforce these
    invariants; the validation here surfaces a 422 in the API
    response, not an :class:`IntegrityError` from the persistence
    layer.
    """

    if request.gaze_warning_limit > request.gaze_termination_limit:
        raise PolicyVersioningError(
            "gaze_warning_limit must be <= gaze_termination_limit"
        )
    if (
        request.gaze_min_duration_ms
        > request.gaze_window_seconds * 1000
    ):
        raise PolicyVersioningError(
            "gaze_min_duration_ms must be <= gaze_window_seconds * 1000"
        )
    if request.medium_score_termination_threshold < 0:
        raise PolicyVersioningError(
            "medium_score_termination_threshold must be >= 0"
        )
    if (
        request.liveness_check_enabled
        and request.liveness_check_action is None
    ):
        # Mirror of the SQL-level
        # ``ck_policy_liveness_action_when_enabled`` constraint.
        raise PolicyVersioningError(
            "liveness_check_action must be set when "
            "liveness_check_enabled is true"
        )
    if not (
        0.0 <= request.liveness_score_threshold <= 1.0
    ):
        raise PolicyVersioningError(
            "liveness_score_threshold must be in [0, 1]"
        )
    if not (
        0.0 <= request.identity_similarity_threshold <= 1.0
    ):
        raise PolicyVersioningError(
            "identity_similarity_threshold must be in [0, 1]"
        )
    if not (
        0.0 <= request.audio_speech_ratio_threshold <= 1.0
    ):
        raise PolicyVersioningError(
            "audio_speech_ratio_threshold must be in [0, 1]"
        )


def _validate_exemption_request(
    request: CreateExemptionRequest,
) -> None:
    """Raise :class:`ExemptionValidationError` on a request that would
    fail a SQL-level constraint."""

    if request.expires_at is not None and request.expires_at <= request.effective_at:
        raise ExemptionValidationError(
            "expires_at must be strictly after effective_at"
        )


# ---------------------------------------------------------------------------
# /admin/policy-config
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PolicyVersionResult:
    """Return shape for :func:`create_policy_version`.

    ``superseded_previous_id`` is ``None`` when ``retire_previous``
    was not set or no prior active row existed for the name.
    """

    new_policy: PolicyConfig
    superseded_previous_id: uuid.UUID | None


def _resolve_previous_active(
    db: Session, name: str
) -> PolicyConfig | None:
    """Return the *currently active* policy row for ``name``, or
    ``None`` if none exists.

    Active = ``is_active=True`` AND ``retired_at IS NULL``.  This is
    the same predicate :func:`proctoring_engine.lti.service.process_launch`
    uses to resolve the policy at launch time.
    """

    return db.execute(
        select(PolicyConfig).where(
            PolicyConfig.name == name,
            PolicyConfig.is_active.is_(True),
            PolicyConfig.retired_at.is_(None),
        )
    ).scalar_one_or_none()


def _resolve_max_version(db: Session, name: str) -> int:
    """Return the highest ``version`` already in the table for
    ``name``, or ``0`` if no rows exist."""

    rows: Sequence[int] = (
        db.execute(
            select(PolicyConfig.version)
            .where(PolicyConfig.name == name)
            .order_by(PolicyConfig.version.desc())
        )
        .scalars()
        .all()
    )
    return rows[0] if rows else 0


def create_policy_version(
    db: Session,
    *,
    request: CreatePolicyConfigRequest,
    created_by: AdminUser,
    now: datetime,
) -> PolicyVersionResult:
    """Insert a new ``PolicyConfig`` row.

    If ``request.retire_previous`` is ``True`` AND a prior active row
    exists for ``request.name``, that row is marked ``retired_at = now``
    in the same transaction.  The returned
    :class:`PolicyVersionResult` carries the new row and the
    (now-retired) previous row's id for the route handler to log.

    The ``name`` column is unique across rows (per
    :mod:`proctoring_engine.models.PolicyConfig`), so creating two
    versions of the same policy requires retiring the old one in the
    same transaction — otherwise the ``uq_policy_configs_name``
    unique constraint raises.
    """

    _validate_policy_request(request)

    previous = _resolve_previous_active(db, request.name)
    superseded_id: uuid.UUID | None = None

    if previous is not None:
        if request.retire_previous:
            previous.retired_at = now
            superseded_id = previous.id
        else:
            # Without retire_previous, the INSERT below would violate
            # the ``uq_policy_configs_name`` constraint because the
            # ``name`` column is unique.  Two retirements of the same
            # name are an explicit, intentional operation.
            raise PolicyVersioningError(
                f"a policy named {request.name!r} is already active; "
                "pass retire_previous=true to supersede it"
            )

    next_version = _resolve_max_version(db, request.name) + 1

    new_row = PolicyConfig(
        name=request.name,
        version=next_version,
        is_active=request.is_active,
        termination_severity=request.termination_severity,
        terminate_on_second_face=request.terminate_on_second_face,
        second_face_confirmation_frames=(
            request.second_face_confirmation_frames
        ),
        gaze_min_duration_ms=request.gaze_min_duration_ms,
        gaze_window_seconds=request.gaze_window_seconds,
        gaze_warning_limit=request.gaze_warning_limit,
        gaze_termination_limit=request.gaze_termination_limit,
        medium_score_termination_threshold=(
            request.medium_score_termination_threshold
        ),
        medium_score_action=request.medium_score_action,
        liveness_check_enabled=request.liveness_check_enabled,
        liveness_check_action=request.liveness_check_action,
        liveness_score_threshold=request.liveness_score_threshold,
        liveness_confirmation_frames=request.liveness_confirmation_frames,
        identity_similarity_threshold=request.identity_similarity_threshold,
        identity_confirmation_frames=request.identity_confirmation_frames,
        audio_noise_floor_dbfs=request.audio_noise_floor_dbfs,
        audio_speech_ratio_threshold=request.audio_speech_ratio_threshold,
        extra_rules=dict(request.extra_rules),
        created_by_id=created_by.id,
    )
    db.add(new_row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise PolicyVersioningError(
            f"PolicyConfig insert violated a constraint: {exc.orig}"
        ) from exc
    db.refresh(new_row)
    return PolicyVersionResult(
        new_policy=new_row, superseded_previous_id=superseded_id
    )


def list_policy_configs(
    db: Session,
    *,
    is_active: bool | None = None,
) -> list[PolicyConfig]:
    """List ``PolicyConfig`` rows, newest first.

    ``is_active`` filters to ``True`` / ``False`` only; passing
    ``None`` returns every row (the admin view of the version
    history).  ``PolicyConfig.name`` is unique across rows, so
    "every active policy" is by-name; "every row" is the
    version-history view.
    """

    stmt = select(PolicyConfig).order_by(
        PolicyConfig.name.asc(),
        PolicyConfig.version.desc(),
    )
    if is_active is not None:
        stmt = stmt.where(PolicyConfig.is_active.is_(is_active))
    return list(db.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# /admin/accommodation-exemptions
# ---------------------------------------------------------------------------


def create_exemption(
    db: Session,
    *,
    request: CreateExemptionRequest,
    approver: AdminUser,
    now: datetime,
) -> AccommodationExemption:
    """INSERT a new ``AccommodationExemption`` row.

    ``approved_by_admin_id`` is the calling :class:`AdminUser`'s id;
    ``approved_by`` (the legacy string column) is the admin's
    ``lms_user_reference`` — the same value the LTI session token's
    ``sub`` claim carries.  The two columns stay in sync.
    """

    _validate_exemption_request(request)

    exemption = AccommodationExemption(
        participant_id=request.participant_id,
        exam_reference=request.exam_reference,
        object_class=request.object_class,
        approved_by=approver.lms_user_reference,
        approved_by_admin_id=approver.id,
        approval_reason=request.approval_reason,
        effective_at=request.effective_at,
        expires_at=request.expires_at,
    )
    db.add(exemption)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ExemptionValidationError(
            f"AccommodationExemption insert violated a constraint: {exc.orig}"
        ) from exc
    db.refresh(exemption)
    return exemption


def list_exemptions(
    db: Session,
    *,
    participant_id: uuid.UUID | None = None,
    object_class: str | None = None,
) -> list[AccommodationExemption]:
    """List ``AccommodationExemption`` rows, newest first.

    Both filters are optional and stack.  An unfiltered call returns
    every row, which is the admin's view of the exemption registry.
    """

    stmt = select(AccommodationExemption).order_by(
        AccommodationExemption.created_at.desc()
    )
    if participant_id is not None:
        stmt = stmt.where(
            AccommodationExemption.participant_id == participant_id
        )
    if object_class is not None:
        stmt = stmt.where(
            AccommodationExemption.object_class == object_class
        )
    return list(db.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# /admin/flags/{session_id}
# ---------------------------------------------------------------------------


def list_flags_for_session(
    db: Session,
    *,
    session_id: uuid.UUID,
    include_suppressed: bool = True,
) -> tuple[list[Flag], int]:
    """SELECT every :class:`Flag` row for the session.

    Returns ``(flags, omitted_suppressed_count)``.  The
    ``omitted_suppressed_count`` is only nonzero when
    ``include_suppressed=False``; the route handler surfaces it to
    the reviewer so they know there are suppressed flags they can't
    see.

    Each returned :class:`Flag` carries its eager-loaded
    ``evidence_artifacts`` and ``reviews`` relationships so the
    response carries the full audit trail the reviewer needs.
    """

    # ``selectinload`` keeps the N+1 problem off the reviewer's hot
    # path: each Flag's evidence_artifacts and reviews are loaded in
    # a single follow-up query.
    from sqlalchemy.orm import selectinload

    rows: Sequence[Flag] = (
        db.execute(
            select(Flag)
            .where(Flag.exam_session_id == session_id)
            .options(
                selectinload(Flag.evidence_artifacts),
                selectinload(Flag.reviews),
            )
            .order_by(Flag.created_at.asc())
        )
        .scalars()
        .all()
    )

    visible: list[Flag] = []
    omitted = 0
    for flag in rows:
        suppressed = flag.suppressed_by_exemption_id is not None
        if suppressed and not include_suppressed:
            omitted += 1
            continue
        visible.append(flag)
    return visible, omitted


def assert_session_exists(
    db: Session, session_id: uuid.UUID
) -> ExamSession:
    """Return the :class:`ExamSession` row or raise."""

    session = db.get(ExamSession, session_id)
    if session is None:
        raise FlagNotFoundError(f"session {session_id!s} does not exist")
    return session


# ---------------------------------------------------------------------------
# /admin/flags/{flag_id}/review
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReviewResult:
    """Return shape for :func:`record_proctor_review`."""

    review: ProctorReview
    session_status: SessionStatus


def record_proctor_review(
    db: Session,
    *,
    flag_id: uuid.UUID,
    decision_value: str,
    notes: str | None,
    reviewer: AdminUser,
    now: datetime,
) -> ReviewResult:
    """INSERT a :class:`ProctorReview` row (append-only).

    Side effects:

    * If ``decision_value == "upheld"`` AND the parent
      :class:`ExamSession` is in ``ACTIVE`` or ``TERMINATED``,
      transitions the session to ``UNDER_REVIEW``.  ``COMPLETED →
      UNDER_REVIEW`` is also allowed by the state machine, but
      recorded here only when ``decision_value == UPHELD`` and the
      reviewer's call.
    * Does **not** mutate the :class:`Flag` row — corrections are new
      linked rows, not edits, per :mod:`CLAUDE.md` §"Hard rules".

    Raises
    ------
    FlagNotFoundError:
        ``flag_id`` does not exist.
    ReviewTransitionError:
        The state-machine guard rejects the move (the only way to
        reach this from a valid call is if the session is in
        ``UNDER_REVIEW`` and a reviewer's upheld decision tries to
        re-apply the same transition).
    """

    flag = db.get(Flag, flag_id)
    if flag is None:
        raise FlagNotFoundError(f"flag {flag_id!s} does not exist")

    review = ProctorReview(
        flag_id=flag_id,
        reviewer_reference=reviewer.lms_user_reference,
        reviewer_admin_id=reviewer.id,
        decision=decision_value,  # ORM validates via enum_value coercion
        notes=notes,
    )
    db.add(review)
    db.flush()  # populate review.id; not committed yet

    session = db.get(ExamSession, flag.exam_session_id)
    if session is None:
        # FK is ON DELETE CASCADE for Flag → ExamSession, so this
        # branch is unreachable in normal operation.  Defensive.
        raise FlagNotFoundError(
            f"session {flag.exam_session_id!s} does not exist"
        )

    new_status = session.status
    if decision_value == "upheld" and session.status in (
        SessionStatus.ACTIVE,
        SessionStatus.TERMINATED,
        SessionStatus.COMPLETED,
    ):
        try:
            assert_transition(session.status, SessionStatus.UNDER_REVIEW)
        except InvalidSessionTransition as exc:
            raise ReviewTransitionError(str(exc)) from exc
        session.status = SessionStatus.UNDER_REVIEW
        new_status = session.status

    db.commit()
    db.refresh(review)
    return ReviewResult(review=review, session_status=new_status)


__all__ = [
    "AdminServiceError",
    "ExemptionValidationError",
    "FlagNotFoundError",
    "PolicyVersioningError",
    "PolicyVersionResult",
    "ReviewResult",
    "ReviewTransitionError",
    "create_exemption",
    "create_policy_version",
    "list_exemptions",
    "list_flags_for_session",
    "list_policy_configs",
    "record_proctor_review",
    "assert_session_exists",
]