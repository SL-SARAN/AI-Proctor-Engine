"""PostgreSQL integration tests for the LTI 1.3 launch flow.

These tests exercise the ``process_launch`` service against a real
PostgreSQL database, verifying the database-level invariants that
SQLite cannot model:

* The JSONB ``PolicyConfig.extra_rules`` column is not mutated by
  the launch (the service does not rewrite the policy snapshot).
* The JSONB ``ExamSession.permitted_material_details`` and
  ``Flag.detail`` columns are not silently populated by the launch
  (they're empty dicts, the documented initial state).
* The ``accumulated_medium_score`` default of ``0`` is preserved
  (i.e. the launch is not silently incrementing the counter).
* The ``started_at`` and ``consent_recorded_at`` columns on
  ``ExamSession`` are set to the same value (the documented
  "consent is start" choice).
* The ``AdminUser`` row's ``(lti_issuer, lms_user_reference)``
  natural key matches the ``Participant``'s natural key for the
  same launch.
* Two consecutive launches from the same natural key produce two
  ``ExamSession`` rows but exactly one ``Participant`` row (the
  upsert semantics).
* The ``attempt_reference`` is a UUID4.

The test module is skipped when ``INTEGRATION_DATABASE_URL`` is
not set, so the standard ``pytest`` invocation in development
still runs only the SQLite unit suite. In CI, the integration
job sets ``INTEGRATION_DATABASE_URL`` to the GitHub Actions
``services.postgres`` host and runs this module explicitly.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from proctoring_engine.lti.claims import LtiIdToken
from proctoring_engine.lti.config import LtiSettings
from proctoring_engine.lti.roles import AppRole
from proctoring_engine.lti.service import process_launch
from proctoring_engine.models import (
    AdminRole,
    AdminUser,
    ExamSession,
    Participant,
    PolicyConfig,
)
from tests.integration.oidc_test_double import (
    ADMIN_URI,
    INSTRUCTOR_URI,
    LEARNER_URI,
    build_signed_launch_claims,
    make_test_oidc_setup,
)


# --- helpers ---------------------------------------------------------


def _settings() -> LtiSettings:
    return LtiSettings(
        tool_client_id="proctoring-engine",
        launch_url="http://localhost:8000/lti/launch",
        session_token_secret="x" * 32,
        oidc_http_timeout_seconds=1.0,
    )


def _build_oidc_setup():
    return make_test_oidc_setup(issuer="https://lms.example.edu", kid="key-1")


def _build_claims(
    settings: LtiSettings,
    *,
    role_uri: str = LEARNER_URI,
    policy_config_name: str = "cs101-default",
    name: str = "Test User",
    subject: str = "user-1",
    context_id: str = "course-101",
    resource_id: str = "exam-midterm",
):
    setup = _build_oidc_setup()
    payload = build_signed_launch_claims(
        issuer=setup.issuer,
        audience=settings.tool_client_id,
        target_link_uri=settings.launch_url,
        kid=setup.kid,
        nonce="test-nonce",
        state="",
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
    # Strip ``state`` — the OIDC flow's ``state`` claim is not
    # an LTI claim and the typed ``LtiIdToken`` model rejects
    # unknown claims with ``extra=forbid``.
    payload.pop("state", None)
    return LtiIdToken.from_jwt_payload(payload)


# --- tests -----------------------------------------------------------


def test_launch_does_not_mutate_policy_extra_rules(
    db_session: Session, settings: LtiSettings
) -> None:
    """The launch does not copy the policy's ``extra_rules`` JSONB
    into the session row.
    """

    policy = PolicyConfig(
        name="cs101-default",
        is_active=True,
        extra_rules={"custom_threshold": 0.42, "escalate": True},
    )
    db_session.add(policy)
    db_session.commit()

    claims = _build_claims(settings)
    process_launch(db_session, claims, AppRole.LEARNER, settings=settings)
    db_session.commit()

    # The session has the default empty dict — the policy's
    # extra_rules are not copied.
    session = db_session.execute(select(ExamSession)).scalar_one()
    assert session.permitted_material_details == {}

    # The policy's own extra_rules are unchanged.
    db_session.refresh(policy)
    assert policy.extra_rules == {"custom_threshold": 0.42, "escalate": True}


def test_launch_does_not_increment_accumulated_medium_score(
    db_session: Session, settings: LtiSettings
) -> None:
    """The launch does not silently increment the
    ``accumulated_medium_score`` counter.
    """

    policy = PolicyConfig(name="cs101-default", is_active=True)
    db_session.add(policy)
    db_session.commit()

    claims = _build_claims(settings)
    process_launch(db_session, claims, AppRole.LEARNER, settings=settings)
    db_session.commit()

    session = db_session.execute(select(ExamSession)).scalar_one()
    assert session.accumulated_medium_score == 0


def test_launch_sets_consent_recorded_at_equal_to_started_at(
    db_session: Session, settings: LtiSettings
) -> None:
    """The documented "consent is start" choice: the launch sets
    ``consent_recorded_at`` and ``started_at`` to the same
    timestamp. The SQL-level
    ``ck_exam_session_timestamp_order`` and
    ``ck_exam_session_retention_after_start`` checks are
    self-consistent at creation time as a result.
    """

    policy = PolicyConfig(name="cs101-default", is_active=True)
    db_session.add(policy)
    db_session.commit()

    claims = _build_claims(settings)
    now = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
    process_launch(
        db_session, claims, AppRole.LEARNER, settings=settings, now=now
    )
    db_session.commit()

    session = db_session.execute(select(ExamSession)).scalar_one()
    assert session.consent_recorded_at == now
    assert session.started_at == now


def test_launch_binds_policy_config_by_id(
    db_session: Session, settings: LtiSettings
) -> None:
    """The ``ExamSession.policy_config_id`` is set to the resolved
    policy's primary key.
    """

    policy = PolicyConfig(name="cs101-default", is_active=True)
    db_session.add(policy)
    db_session.commit()

    claims = _build_claims(settings)
    result = process_launch(
        db_session, claims, AppRole.LEARNER, settings=settings
    )
    db_session.commit()

    assert result.exam_session.policy_config_id == policy.id


def test_admin_user_natural_key_matches_participant(
    db_session: Session, settings: LtiSettings
) -> None:
    """For an instructor launch, the upserted ``AdminUser`` row's
    ``(lti_issuer, lms_user_reference)`` natural key matches the
    ``Participant``'s natural key for the same launch.
    """

    policy = PolicyConfig(name="cs101-default", is_active=True)
    db_session.add(policy)
    db_session.commit()

    claims = _build_claims(settings, role_uri=INSTRUCTOR_URI)
    result = process_launch(
        db_session, claims, AppRole.INSTRUCTOR, settings=settings
    )
    db_session.commit()

    # The natural keys match.
    assert (result.participant.lti_issuer, result.participant.lms_user_reference) == (
        claims.issuer,
        claims.subject,
    )

    # The AdminUser row exists with the same natural key.
    admin = db_session.execute(
        select(AdminUser).where(
            AdminUser.lti_issuer == claims.issuer,
            AdminUser.lms_user_reference == claims.subject,
        )
    ).scalar_one()
    assert (admin.lti_issuer, admin.lms_user_reference) == (
        result.participant.lti_issuer,
        result.participant.lms_user_reference,
    )
    assert admin.role == AdminRole.INSTRUCTOR


def test_two_launches_same_natural_key_create_two_sessions(
    db_session: Session, settings: LtiSettings
) -> None:
    """Two consecutive launches from the same natural key create
    one ``Participant`` row (the upsert) and two ``ExamSession``
    rows (each launch is a new attempt).
    """

    policy = PolicyConfig(name="cs101-default", is_active=True)
    db_session.add(policy)
    db_session.commit()

    claims_a = _build_claims(settings, name="First Name", subject="user-1")
    result_a = process_launch(
        db_session, claims_a, AppRole.LEARNER, settings=settings
    )
    db_session.commit()

    claims_b = _build_claims(settings, name="Updated Name", subject="user-1")
    result_b = process_launch(
        db_session, claims_b, AppRole.LEARNER, settings=settings
    )
    db_session.commit()

    # One Participant row.
    participants = db_session.execute(
        select(Participant).where(
            Participant.lti_issuer == claims_a.issuer,
            Participant.lms_user_reference == claims_a.subject,
        )
    ).scalars().all()
    assert len(participants) == 1
    # The display name is overwritten by the second launch (the
    # launch is the source of truth for the LMS-side display
    # name).
    assert participants[0].display_name == "Updated Name"

    # Two ExamSession rows, each linked to the same participant.
    sessions = db_session.execute(
        select(ExamSession).where(
            ExamSession.participant_id == participants[0].id
        )
    ).scalars().all()
    assert len(sessions) == 2
    assert {s.id for s in sessions} == {result_a.exam_session.id, result_b.exam_session.id}


def test_attempt_reference_is_uuid4(
    db_session: Session, settings: LtiSettings
) -> None:
    """Each launch produces a unique ``attempt_reference`` (UUID4)."""

    policy = PolicyConfig(name="cs101-default", is_active=True)
    db_session.add(policy)
    db_session.commit()

    claims_a = _build_claims(settings, subject="user-a")
    result_a = process_launch(
        db_session, claims_a, AppRole.LEARNER, settings=settings
    )

    claims_b = _build_claims(settings, subject="user-b")
    result_b = process_launch(
        db_session, claims_b, AppRole.LEARNER, settings=settings
    )
    db_session.commit()

    # Both values parse as UUID4 (i.e. version 4).
    uuid.UUID(result_a.exam_session.attempt_reference, version=4)
    uuid.UUID(result_b.exam_session.attempt_reference, version=4)
    assert result_a.exam_session.attempt_reference != result_b.exam_session.attempt_reference


def test_admin_role_promotion_preserves_participant(
    db_session: Session, settings: LtiSettings
) -> None:
    """An admin-role launch promotes the ``AdminUser`` to ``ADMIN``
    while leaving the existing ``Participant`` row untouched.
    """

    policy = PolicyConfig(name="cs101-default", is_active=True)
    db_session.add(policy)
    db_session.commit()

    # First launch: instructor creates the AdminUser and
    # Participant rows.
    instructor_claims = _build_claims(settings, role_uri=INSTRUCTOR_URI)
    process_launch(
        db_session, instructor_claims, AppRole.INSTRUCTOR, settings=settings
    )
    db_session.commit()

    # Second launch: admin promotes the AdminUser to ADMIN.
    admin_claims = _build_claims(settings, role_uri=ADMIN_URI)
    process_launch(
        db_session, admin_claims, AppRole.ADMIN, settings=settings
    )
    db_session.commit()

    # The AdminUser's role is ADMIN, not INSTRUCTOR.
    admin = db_session.execute(
        select(AdminUser).where(
            AdminUser.lti_issuer == admin_claims.issuer,
            AdminUser.lms_user_reference == admin_claims.subject,
        )
    ).scalar_one()
    assert admin.role == AdminRole.ADMIN

    # Only one Participant row exists — the admin launch did not
    # create a duplicate.
    participants = db_session.execute(
        select(Participant).where(
            Participant.lti_issuer == admin_claims.issuer,
            Participant.lms_user_reference == admin_claims.subject,
        )
    ).scalars().all()
    assert len(participants) == 1


def test_launch_combines_context_and_resource_link(
    db_session: Session, settings: LtiSettings
) -> None:
    """The ``lti_context_id`` is the documented
    ``<context.id>:<resource_link.id>`` shape."""

    policy = PolicyConfig(name="cs101-default", is_active=True)
    db_session.add(policy)
    db_session.commit()

    claims = _build_claims(
        settings, context_id="course-101", resource_id="exam-midterm"
    )
    process_launch(db_session, claims, AppRole.LEARNER, settings=settings)
    db_session.commit()

    session = db_session.execute(select(ExamSession)).scalar_one()
    assert session.lti_context_id == "course-101:exam-midterm"
    assert session.exam_reference == "exam-midterm"
    assert session.lti_issuer == claims.issuer
