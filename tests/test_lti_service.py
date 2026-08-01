"""Unit tests for :func:`proctoring_engine.lti.service.process_launch`.

Boundary cases (per ``docs/02-ingestion-layer-design.md`` §1 and
``docs/08-test-strategy-design.md`` §"Ingestion layer"):

* Successful learner launch creates ``Participant`` +
  ``ExamSession(PENDING)`` with ``consent_recorded_at`` set and
  the active ``PolicyConfig`` bound.
* The ``ExamSession``'s ``accumulated_medium_score`` default is
  preserved (i.e. the launch does not silently mutate the
  counter).
* An instructor-role launch upserts an ``AdminUser`` row.
* An admin-role launch sets the ``AdminUser`` role to
  ``ADMIN`` (the highest tier).
* A launch with ``custom.policy_config_name`` matching no
  active policy raises ``LtiLaunchError("policy_not_found")``.
* Two consecutive launches from the same natural key upsert
  the participant and create two ``ExamSession`` rows.
* The ``attempt_reference`` is a UUID4.
* The ``lti_context_id`` is ``<context.id>:<resource_link.id>``.
* The session token round-trips through
  :func:`decode_session_token`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from proctoring_engine.lti.claims import LtiIdToken
from proctoring_engine.lti.config import LtiSettings
from proctoring_engine.lti.roles import AppRole
from proctoring_engine.lti.service import (
    LtiLaunchError,
    LtiLaunchErrorCode,
    process_launch,
)
from proctoring_engine.lti.session_token import decode_session_token
from proctoring_engine.models import (
    AdminRole,
    AdminUser,
    ExamSession,
    Participant,
    PolicyConfig,
    SessionStatus,
)
from .integration.oidc_test_double import (
    ADMIN_URI,
    INSTRUCTOR_URI,
    LEARNER_URI,
    build_signed_launch_claims,
    make_test_oidc_setup,
)


# --- fixtures ---------------------------------------------------------


@pytest.fixture()
def settings() -> LtiSettings:
    """A test :class:`LtiSettings` with sensible defaults."""

    return LtiSettings(
        tool_client_id="proctoring-engine",
        launch_url="http://localhost:8000/lti/launch",
        session_token_secret="x" * 32,
        oidc_http_timeout_seconds=1.0,
    )


@pytest.fixture()
def active_policy(db_session) -> PolicyConfig:
    """An active policy the launch flow can resolve."""

    policy = PolicyConfig(name="cs101-default", is_active=True)
    db_session.add(policy)
    db_session.commit()
    return policy


@pytest.fixture()
def retired_policy(db_session) -> PolicyConfig:
    """A retired (is_active=False) policy the launch flow must reject."""

    policy = PolicyConfig(name="retired-policy", is_active=False)
    db_session.add(policy)
    db_session.commit()
    return policy


def _build_oidc_setup():
    return make_test_oidc_setup(issuer="https://lms.example.edu", kid="key-1")


def _build_claims(
    settings: LtiSettings,
    *,
    role_uri: str = LEARNER_URI,
    policy_config_name: str = "cs101-default",
    name: str = "Test User",
    subject: str = "user-1",
    state: Optional[str] = None,
    nonce: str = "test-nonce",
    context_id: str = "course-101",
    resource_id: str = "exam-midterm",
):
    """Build a valid ``LtiIdToken`` for the test launch.

    ``state`` defaults to ``None`` so the test payload does
    not carry the OIDC ``state`` claim (which the typed
    LtiIdToken model rejects with ``extra=forbid``). The
    service is state-agnostic; the route tests cover the
    state-handling path.
    """

    setup = _build_oidc_setup()
    payload = build_signed_launch_claims(
        issuer=setup.issuer,
        audience=settings.tool_client_id,
        target_link_uri=settings.launch_url,
        kid=setup.kid,
        nonce=nonce,
        state=state or "",
        policy_config_name=policy_config_name,
        role_uri=role_uri,
        name=name,
        subject=subject,
        extra_claims={
            "https://purl.imsglobal.org/spec/lti/claim/context": {
                "id": context_id,
                "label": "CS101",
                "title": "Intro to CS",
            },
            "https://purl.imsglobal.org/spec/lti/claim/resource_link": {
                "id": resource_id,
                "title": "Midterm",
            },
        },
    )
    # Strip ``state`` — it's a JWT claim but not an LTI
    # claim, and the typed model rejects unknown claims.
    payload.pop("state", None)
    return LtiIdToken.from_jwt_payload(payload)


# --- tests ------------------------------------------------------------


def test_learner_launch_creates_participant_and_pending_session(
    db_session, settings, active_policy
) -> None:
    """A successful learner launch creates a Participant + a
    PENDING ExamSession with the policy bound and consent recorded.
    """

    claims = _build_claims(settings)
    now = datetime.now(timezone.utc)
    result = process_launch(
        db_session, claims, AppRole.LEARNER, settings=settings, now=now
    )
    db_session.commit()

    # The Participant row exists with the right natural key.
    participant = db_session.execute(
        select(Participant).where(
            Participant.lti_issuer == claims.issuer,
            Participant.lms_user_reference == claims.subject,
        )
    ).scalar_one()
    assert participant.display_name == "Test User"

    # The ExamSession row exists, PENDING, with the policy
    # bound and consent recorded at the documented time.
    session = db_session.execute(
        select(ExamSession).where(ExamSession.id == result.exam_session.id)
    ).scalar_one()
    assert session.status == SessionStatus.PENDING
    assert session.policy_config_id == active_policy.id
    assert session.consent_recorded_at == now
    assert session.started_at == now
    assert session.accumulated_medium_score == 0

    # The combined context id is <context.id>:<resource_link.id>.
    assert session.lti_context_id == "course-101:exam-midterm"
    assert session.exam_reference == "exam-midterm"
    assert session.lti_issuer == claims.issuer


def test_exam_session_does_not_inherit_policy_extra_rules(
    db_session, settings, active_policy
) -> None:
    """The launch does not copy the policy's ``extra_rules`` into
    the session row. The two are orthogonal; copying them would
    create a hidden dependency on the policy's mutable dict.
    """

    active_policy.extra_rules = {"custom_threshold": 0.42}
    db_session.commit()

    claims = _build_claims(settings)
    process_launch(db_session, claims, AppRole.LEARNER, settings=settings)
    db_session.commit()

    session = db_session.execute(select(ExamSession)).scalar_one()
    assert session.permitted_material_details == {}
    # The policy's own extra_rules are still the policy's.
    db_session.refresh(active_policy)
    assert active_policy.extra_rules == {"custom_threshold": 0.42}


def test_instructor_launch_upserts_admin_user(db_session, settings, active_policy) -> None:
    """An instructor-role launch creates an ``AdminUser`` row."""

    claims = _build_claims(settings, role_uri=INSTRUCTOR_URI)
    result = process_launch(
        db_session, claims, AppRole.INSTRUCTOR, settings=settings
    )
    db_session.commit()

    admin = db_session.execute(
        select(AdminUser).where(
            AdminUser.lti_issuer == claims.issuer,
            AdminUser.lms_user_reference == claims.subject,
        )
    ).scalar_one()
    assert admin.role == AdminRole.INSTRUCTOR
    assert admin.display_name == "Test User"
    # The AdminUser's natural key matches the Participant's.
    participant = db_session.execute(
        select(Participant).where(Participant.id == result.participant.id)
    ).scalar_one()
    assert (admin.lti_issuer, admin.lms_user_reference) == (
        participant.lti_issuer,
        participant.lms_user_reference,
    )


def test_admin_role_promotes_admin_user_to_admin(
    db_session, settings, active_policy
) -> None:
    """An admin-role launch sets the AdminUser's role to ADMIN
    (the highest tier).
    """

    # First, a learner launch (no AdminUser yet).
    learner_claims = _build_claims(settings, role_uri=LEARNER_URI)
    process_launch(db_session, learner_claims, AppRole.LEARNER, settings=settings)
    db_session.commit()

    # Then, an admin launch from the same user.
    admin_claims = _build_claims(settings, role_uri=ADMIN_URI)
    process_launch(db_session, admin_claims, AppRole.ADMIN, settings=settings)
    db_session.commit()

    admin = db_session.execute(
        select(AdminUser).where(
            AdminUser.lti_issuer == admin_claims.issuer,
            AdminUser.lms_user_reference == admin_claims.subject,
        )
    ).scalar_one()
    assert admin.role == AdminRole.ADMIN


def test_admin_role_promotion_is_one_way(
    db_session, settings, active_policy
) -> None:
    """A later lower-privilege launch does not demote the
    AdminUser's role.
    """

    # First, an admin launch.
    admin_claims = _build_claims(settings, role_uri=ADMIN_URI)
    process_launch(db_session, admin_claims, AppRole.ADMIN, settings=settings)
    db_session.commit()

    # Then, a learner launch (which is *not* on the admin
    # path; the AdminUser row exists from the first launch).
    # Calling process_launch with INSTRUCTOR would try to
    # upsert the admin row; verify it stays at ADMIN.
    instructor_claims = _build_claims(settings, role_uri=INSTRUCTOR_URI)
    process_launch(
        db_session, instructor_claims, AppRole.INSTRUCTOR, settings=settings
    )
    db_session.commit()

    admin = db_session.execute(
        select(AdminUser).where(
            AdminUser.lti_issuer == admin_claims.issuer,
            AdminUser.lms_user_reference == admin_claims.subject,
        )
    ).scalar_one()
    assert admin.role == AdminRole.ADMIN


def test_unknown_policy_name_raises(db_session, settings) -> None:
    """A launch naming a non-existent policy raises
    ``LtiLaunchError("policy_not_found")``.
    """

    claims = _build_claims(settings, policy_config_name="never-defined")
    with pytest.raises(LtiLaunchError) as exc_info:
        process_launch(db_session, claims, AppRole.LEARNER, settings=settings)
    assert exc_info.value.code == LtiLaunchErrorCode.POLICY_NOT_FOUND


def test_retired_policy_name_raises(
    db_session, settings, retired_policy
) -> None:
    """A launch naming a retired (``is_active=False``) policy
    raises the same error — a retired policy is not the same
    as an inactive one, but neither is bindable to a new session.
    """

    claims = _build_claims(settings, policy_config_name="retired-policy")
    with pytest.raises(LtiLaunchError) as exc_info:
        process_launch(db_session, claims, AppRole.LEARNER, settings=settings)
    assert exc_info.value.code == LtiLaunchErrorCode.POLICY_NOT_FOUND


def test_two_launches_upsert_participant_and_create_two_sessions(
    db_session, settings, active_policy
) -> None:
    """Two consecutive launches from the same natural key create
    one Participant row (the upsert) and two ExamSession rows
    (each launch is a new attempt).
    """

    claims_a = _build_claims(
        settings,
        name="First Name",
        state="state-a",
        nonce="nonce-a",
    )
    process_launch(db_session, claims_a, AppRole.LEARNER, settings=settings)
    db_session.commit()

    claims_b = _build_claims(
        settings,
        name="Updated Name",
        state="state-b",
        nonce="nonce-b",
    )
    process_launch(db_session, claims_b, AppRole.LEARNER, settings=settings)
    db_session.commit()

    participants = db_session.execute(
        select(Participant).where(
            Participant.lti_issuer == claims_a.issuer,
            Participant.lms_user_reference == claims_a.subject,
        )
    ).scalars().all()
    assert len(participants) == 1
    assert participants[0].display_name == "Updated Name"

    sessions = db_session.execute(
        select(ExamSession).where(
            ExamSession.participant_id == participants[0].id
        )
    ).scalars().all()
    assert len(sessions) == 2


def test_attempt_reference_is_uuid4(db_session, settings, active_policy) -> None:
    """Each launch produces a unique ``attempt_reference`` (UUID4)."""

    claims_a = _build_claims(settings, state="s1", nonce="n1")
    result_a = process_launch(
        db_session, claims_a, AppRole.LEARNER, settings=settings
    )
    claims_b = _build_claims(settings, state="s2", nonce="n2")
    result_b = process_launch(
        db_session, claims_b, AppRole.LEARNER, settings=settings
    )
    db_session.commit()

    # Both values parse as UUID4 (i.e. version 4).
    uuid.UUID(result_a.exam_session.attempt_reference, version=4)
    uuid.UUID(result_b.exam_session.attempt_reference, version=4)
    assert result_a.exam_session.attempt_reference != result_b.exam_session.attempt_reference


def test_session_token_round_trips(db_session, settings, active_policy) -> None:
    """The session token returned by ``process_launch`` round-trips
    through ``decode_session_token`` with matching claims.
    """

    claims = _build_claims(settings)
    result = process_launch(db_session, claims, AppRole.LEARNER, settings=settings)
    db_session.commit()

    decoded = decode_session_token(result.session_token, settings=settings)
    assert decoded.subject == str(result.participant.id)
    assert decoded.session_id == str(result.exam_session.id)
    assert decoded.role == AppRole.LEARNER
    assert decoded.issuer == settings.session_token_issuer
    assert decoded.audience == settings.session_token_audience


def test_redirect_url_uses_exam_client_for_learner(
    db_session, settings, active_policy
) -> None:
    """A learner launch produces a redirect to the exam client URL."""

    claims = _build_claims(settings)
    result = process_launch(db_session, claims, AppRole.LEARNER, settings=settings)
    assert result.redirect_url.startswith(settings.exam_client_url)
    # Session token and session id travel in the URL fragment, not
    # the query string — never logged by reverse proxies, never
    # leaked via Referer headers, never persisted in browser history
    # in a way that an intermediate proxy records.
    assert "session_token=" in result.redirect_url
    assert "session_id=" in result.redirect_url
    # Defensive: query string is empty (no "?" before the fragment).
    fragment_index = result.redirect_url.index("#")
    query_part = result.redirect_url[len(settings.exam_client_url) : fragment_index]
    assert query_part == ""


def test_redirect_url_uses_admin_surface_for_instructor(
    db_session, settings, active_policy
) -> None:
    """A non-learner launch produces a redirect to the admin surface URL."""

    claims = _build_claims(settings, role_uri=INSTRUCTOR_URI)
    result = process_launch(
        db_session, claims, AppRole.INSTRUCTOR, settings=settings
    )
    assert result.redirect_url.startswith(settings.admin_surface_url)
    assert "session_token=" in result.redirect_url
    assert "session_id=" in result.redirect_url
    fragment_index = result.redirect_url.index("#")
    query_part = result.redirect_url[len(settings.admin_surface_url) : fragment_index]
    assert query_part == ""


def test_redirect_url_carries_session_id_in_fragment(
    db_session, settings, active_policy
) -> None:
    """The fragment carries the exam session id alongside the token,
    so the client can open the WebSocket against the right session
    without parsing the JWT.
    """

    claims = _build_claims(settings)
    result = process_launch(db_session, claims, AppRole.LEARNER, settings=settings)
    fragment = result.redirect_url.split("#", 1)[1]
    params = dict(p.split("=", 1) for p in fragment.split("&"))
    assert params["session_token"] == result.session_token
    assert params["session_id"] == str(result.exam_session.id)


def test_learner_launch_does_not_create_admin_user(
    db_session, settings, active_policy
) -> None:
    """A learner launch does not create an ``AdminUser`` row."""

    claims = _build_claims(settings)
    process_launch(db_session, claims, AppRole.LEARNER, settings=settings)
    db_session.commit()

    admins = db_session.execute(select(AdminUser)).scalars().all()
    assert admins == []


def test_failed_launch_rolls_back_participant(
    db_session, settings, active_policy
) -> None:
    """A launch that fails after the participant upsert rolls back
    the whole transaction — no orphan ``Participant`` row.
    """

    class _BoomPolicy:
        """A sentinel that, when bound into the session, causes
        the next flush to raise.
        """

    # Create a launch that will pass the policy lookup but
    # fail at the ExamSession insert (we use a session-scoped
    # flag to trigger a failure mid-transaction).
    claims = _build_claims(settings)

    # Patch the model class so the next ExamSession insert
    # raises — simulates a DB error mid-launch.
    from sqlalchemy.exc import SQLAlchemyError
    from proctoring_engine.models import ExamSession as _ExamSession

    original_init = _ExamSession.__init__

    def _boom_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        raise SQLAlchemyError("simulated failure during ExamSession insert")

    _ExamSession.__init__ = _boom_init  # type: ignore[assignment]
    try:
        with pytest.raises(SQLAlchemyError):
            process_launch(db_session, claims, AppRole.LEARNER, settings=settings)
    finally:
        _ExamSession.__init__ = original_init  # type: ignore[assignment]

    db_session.rollback()
    participants = db_session.execute(select(Participant)).scalars().all()
    assert participants == []
    sessions = db_session.execute(select(ExamSession)).scalars().all()
    assert sessions == []
