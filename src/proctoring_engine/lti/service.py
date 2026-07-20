"""``process_launch`` — the business logic of an LTI 1.3 launch.

This module is the single place that turns a *validated*
:class:`proctoring_engine.lti.claims.LtiIdToken` into persisted
rows. The route handler does the OIDC / JWT / state / nonce
validation; the service does the data side:

1. Upsert the :class:`proctoring_engine.models.Participant` row
   on the natural key ``(lti_issuer, lms_user_reference)``.
2. Resolve the :class:`proctoring_engine.models.PolicyConfig`
   by name + ``is_active=True``.
3. Create the :class:`proctoring_engine.models.ExamSession` row
   with ``status=PENDING``, the policy snapshot bound, and
   ``consent_recorded_at = started_at = now(UTC)``.
4. Upsert an :class:`proctoring_engine.models.AdminUser` row
   when the role maps to the admin surface.
5. Issue the HS256 session token.
6. Return a :class:`LaunchResult` with the resolved redirect URL.

The service is a **pure function** over a SQLAlchemy
:class:`~sqlalchemy.orm.Session` and the launch inputs. It does
not commit; the route handler commits after the service returns
so the whole launch is a single atomic write. A failure between
"participant upserted" and "exam session created" rolls back the
whole launch — no orphan ``Participant`` rows from a partial
launch.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from proctoring_engine.lti.claims import (
    LtiIdToken,
    combined_context_id,
    require_policy_config_name,
)
from proctoring_engine.lti.config import LtiSettings
from proctoring_engine.lti.roles import AppRole, is_admin_route
from proctoring_engine.lti.session_token import issue_session_token
from proctoring_engine.models import (
    AdminRole,
    AdminUser,
    ExamSession,
    Participant,
    PolicyConfig,
    SessionStatus,
)


class LtiLaunchErrorCode(str, enum.Enum):
    """The closed enumeration of launch-failure error codes.

    The route handler maps each code to an HTTP status:

    * ``policy_not_found`` — 400. The platform's LTI claim
      named a policy that does not exist (or is not active).
    * ``discovery_error`` — 502. The platform's OIDC discovery
      document could not be fetched (this surfaces from the
      route, not the service).

    The closed set means the integration test can assert on
    the error code in the response body, not just the status
    code. Free-form error messages are deliberately not used
    here: they are a place where a JWT-internal detail could
    leak into a response.
    """

    POLICY_NOT_FOUND = "policy_not_found"


class LtiLaunchError(Exception):
    """Raised when a launch cannot be processed.

    Carries a :class:`LtiLaunchErrorCode` so the route handler
    can map the failure to the right HTTP status without
    relying on the exception message.
    """

    def __init__(self, code: LtiLaunchErrorCode, message: str = "") -> None:
        super().__init__(message or code.value)
        self.code = code


@dataclass(frozen=True, slots=True)
class LaunchResult:
    """The output of a successful :func:`process_launch` call.

    The :attr:`redirect_url` is the URL the route handler 302s
    the browser to. It carries the session token as a query
    parameter so the exam client (or admin surface, in v2) can
    open the WebSocket with the token as the auth credential.
    """

    participant: Participant
    exam_session: ExamSession
    session_token: str
    role: AppRole
    redirect_url: str


def process_launch(
    db: Session,
    claims: LtiIdToken,
    role: AppRole,
    *,
    settings: LtiSettings,
    now: Optional[datetime] = None,
) -> LaunchResult:
    """Turn a validated LTI 1.3 launch into persisted rows and a
    session token.

    See the module docstring for the step-by-step contract. The
    caller is responsible for committing the transaction after
    this function returns successfully.
    """

    # 1. The policy name is required at the launch handler level
    #    (the route returns 400 with policy_not_found for the
    #    missing-claim case before the service is called), but
    #    re-checking here makes the service self-contained for
    #    the unit tests that call it directly.
    policy_name = require_policy_config_name(claims)

    # 2. Resolve the active policy. ``is_active=True`` AND
    #    ``retired_at IS NULL`` — both conditions because a
    #    retired policy is not the same as an inactive one in
    #    the audit story (a retired policy is preserved for
    #    audit but should not be assigned to new sessions).
    policy = db.execute(
        select(PolicyConfig).where(
            PolicyConfig.name == policy_name,
            PolicyConfig.is_active.is_(True),
            PolicyConfig.retired_at.is_(None),
        )
    ).scalar_one_or_none()
    if policy is None:
        raise LtiLaunchError(
            LtiLaunchErrorCode.POLICY_NOT_FOUND,
            f"no active policy named {policy_name!r}",
        )

    # 3. Upsert the participant on the natural key. The
    #    display_name is updated if the launch provides one —
    #    the LMS is the source of truth for the display name.
    participant = db.execute(
        select(Participant).where(
            Participant.lti_issuer == claims.issuer,
            Participant.lms_user_reference == claims.subject,
        )
    ).scalar_one_or_none()
    if participant is None:
        participant = Participant(
            lti_issuer=claims.issuer,
            lms_user_reference=claims.subject,
            display_name=claims.name,
        )
        db.add(participant)
        db.flush()  # populate participant.id before the session FK
    elif claims.name and claims.name != participant.display_name:
        participant.display_name = claims.name

    # 4. Create the exam session. ``consent_recorded_at`` and
    #    ``started_at`` are the same value: consent is the act
    #    of starting the proctored session, the WS handshake
    #    is transport, not consent. The SQL-level checks
    #    ``ck_exam_session_timestamp_order`` and
    #    ``ck_exam_session_retention_after_start`` are
    #    self-consistent at this point because both
    #    timestamps are equal.
    consent_at = now or datetime.now(timezone.utc)
    if consent_at.tzinfo is None:
        # Naive datetimes are an error: the column is
        # timezone-aware and Postgres will reject them.
        consent_at = consent_at.replace(tzinfo=timezone.utc)

    exam_session = ExamSession(
        participant_id=participant.id,
        policy_config_id=policy.id,
        lti_issuer=claims.issuer,
        lti_context_id=combined_context_id(claims),
        exam_reference=claims.resource_link.id,
        attempt_reference=str(uuid.uuid4()),
        status=SessionStatus.PENDING,
        consent_recorded_at=consent_at,
        started_at=consent_at,
    )
    db.add(exam_session)
    db.flush()  # populate exam_session.id before the session token

    # 5. Upsert the admin user when the role maps to the
    #    admin surface. The natural key is
    #    ``(lti_issuer, lms_user_reference)`` — the same
    #    shape used for ``Participant``, scoped so the same
    #    instructor across two LMS platforms remains
    #    distinct. The role stored is the *highest*
    #    applicable tier (AppRole.ADMIN > PROCTOR >
    #    INSTRUCTOR) so a later lower-privilege launch does
    #    not demote the existing row.
    if is_admin_route(role):
        admin_role = _admin_role_for(role)
        admin_user = db.execute(
            select(AdminUser).where(
                AdminUser.lti_issuer == claims.issuer,
                AdminUser.lms_user_reference == claims.subject,
            )
        ).scalar_one_or_none()
        if admin_user is None:
            admin_user = AdminUser(
                lti_issuer=claims.issuer,
                lms_user_reference=claims.subject,
                display_name=claims.name,
                role=admin_role,
            )
            db.add(admin_user)
        else:
            # Promote the role if the new launch is a higher
            # privilege. Demotions are a no-op so an
            # instructor-role launch from a different
            # platform cannot lower the stored role.
            if _admin_role_privilege(admin_role) > _admin_role_privilege(admin_user.role):
                admin_user.role = admin_role
            if claims.name and claims.name != admin_user.display_name:
                admin_user.display_name = claims.name

    # 6. Issue the session token. The token's ``exp`` is
    #    ``consent_at + settings.session_token_ttl_seconds``
    #    (the session token is bound to the exam window, not
    #    the current wall clock).
    session_token = issue_session_token(
        participant_id=participant.id,
        exam_session_id=exam_session.id,
        role=role,
        settings=settings,
        now=consent_at,
    )

    # 7. Build the redirect URL. The token travels as a query
    #    parameter so the exam client (or admin surface, in
    #    v2) can open the WebSocket with it. The route
    #    handler 302s to this URL on success.
    if role == AppRole.LEARNER:
        redirect_url = f"{settings.exam_client_url}?session_token={session_token}"
    else:
        redirect_url = (
            f"{settings.admin_surface_url}?session_token={session_token}"
        )

    return LaunchResult(
        participant=participant,
        exam_session=exam_session,
        session_token=session_token,
        role=role,
        redirect_url=redirect_url,
    )


# --- helpers ----------------------------------------------------------


_ADMIN_ROLE_PRIVILEGE = {
    AdminRole.INSTRUCTOR: 1,
    AdminRole.PROCTOR: 2,
    AdminRole.ADMIN: 3,
}


def _admin_role_for(role: AppRole) -> AdminRole:
    """Map the application-level role to the persisted
    :class:`AdminRole`.

    Only the three admin-route roles reach this helper; the
    caller is :func:`process_launch` which already guards
    on :func:`is_admin_route`.
    """

    if role == AppRole.ADMIN:
        return AdminRole.ADMIN
    if role == AppRole.PROCTOR:
        return AdminRole.PROCTOR
    return AdminRole.INSTRUCTOR


def _admin_role_privilege(role: AdminRole) -> int:
    """Return the privilege rank of an :class:`AdminRole`."""

    return _ADMIN_ROLE_PRIVILEGE[role]
