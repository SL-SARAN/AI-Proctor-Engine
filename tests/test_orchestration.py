"""Unit tests for the API / orchestration layer.

The tests are organised into one class per source module, mirroring
the way :mod:`proctoring_engine.orchestration` is split.  Each
class exercises one contract: settings loading, state-machine
transitions, the four auth/authorization decisions, the admin
service operations, the evidence-flush service, and the route
surface via :class:`fastapi.testclient.TestClient`.

Every test runs against an in-memory SQLite engine with FK
constraints enabled (per ``tests/conftest.py``) and the
:class:`InMemoryEvidenceStore` for the storage layer.  Tests that
need an admin / instructor / proctor session token build one with
:func:`issue_session_token` and the matching :class:`LtiSettings`;
tests that need the *internal* terminate credential just set the
``Authorization: Bearer`` header.

All assertions on the route surface use the closed error-code
envelope (``detail.code`` and ``detail.message``), matching the
LTI layer's contract.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Generator
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from proctoring_engine.evidence import InMemoryEvidenceStore
from proctoring_engine.lti.config import LtiSettings
from proctoring_engine.lti.roles import AppRole
from proctoring_engine.lti.session_token import issue_session_token
from proctoring_engine.models import (
    AccommodationExemption,
    AdminRole,
    AdminUser,
    ExamSession,
    Flag,
    FlagSeverity,
    FlagTelemetryEvent,
    Participant,
    PolicyConfig,
    ProctorReview,
    ReviewDecision,
    SessionStatus,
    TelemetryEvent,
    TelemetryModality,
)
from proctoring_engine.orchestration import (
    OrchestrationSettings,
    build_orchestration_router,
    get_orchestration_settings,
    reset_orchestration_settings,
    set_orchestration_settings,
)
from proctoring_engine.orchestration._routes import _OrchestrationDeps
from proctoring_engine.orchestration._state_machine import (
    InvalidSessionTransition,
    allowed_targets,
    apply_transition,
    assert_transition,
    can_transition,
)
from proctoring_engine.orchestration._auth import (
    parse_bearer,
    require_admin_role,
    require_internal_terminate_token,
    require_session_owner_or_admin,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def internal_token() -> str:
    """The internal terminate token used in tests."""

    return "x" * 48


@pytest.fixture()
def orchestration_settings(internal_token: str) -> OrchestrationSettings:
    return OrchestrationSettings(
        internal_terminate_token=internal_token,
        retention_default_seconds=86_400,
    )


@pytest.fixture()
def lti_settings() -> LtiSettings:
    return LtiSettings(
        tool_client_id="proctoring-engine",
        launch_url="http://localhost:8000/lti/launch",
        session_token_secret="x" * 32,
        oidc_http_timeout_seconds=1.0,
    )


@pytest.fixture()
def test_db() -> Generator[Session, None, None]:
    """A SQLite in-memory engine with ``StaticPool`` so all
    sessions across threads share the same connection.

    Mirrors the ``test_db`` fixture in ``tests/test_lti_routes.py``:
    ``StaticPool`` keeps one connection for the engine's lifetime,
    which is the right shape for a test-only in-memory database
    where FastAPI runs the route handler in a worker thread.
    """

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from proctoring_engine.models import Base

    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture()
def evidence_store() -> InMemoryEvidenceStore:
    return InMemoryEvidenceStore()


@pytest.fixture()
def app_factory(
    test_db: Session,
    lti_settings: LtiSettings,
    orchestration_settings: OrchestrationSettings,
    evidence_store: InMemoryEvidenceStore,
) -> Callable[[], tuple[FastAPI, Session]]:
    """Build a fresh FastAPI app per call, wired with the
    orchestration router against the per-call dependencies."""

    def _build() -> tuple[FastAPI, Session]:
        app = FastAPI()
        deps = _OrchestrationDeps(
            settings=orchestration_settings,
            lti_settings=lti_settings,
            get_db=lambda: test_db,
            evidence_store=evidence_store,
        )
        app.include_router(build_orchestration_router(deps))
        return app, test_db

    return _build


@pytest.fixture()
def active_policy(test_db: Session) -> PolicyConfig:
    policy = PolicyConfig(name="cs101-default", is_active=True)
    test_db.add(policy)
    test_db.commit()
    test_db.refresh(policy)
    return policy


def _make_participant(session: Session, *, lms_user_reference: str) -> Participant:
    p = Participant(
        lti_issuer="https://lms.example.edu",
        lms_user_reference=lms_user_reference,
    )
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _make_exam_session(
    session: Session,
    *,
    participant: Participant,
    policy: PolicyConfig,
    status: SessionStatus = SessionStatus.PENDING,
) -> ExamSession:
    s = ExamSession(
        participant_id=participant.id,
        policy_config_id=policy.id,
        lti_issuer="https://lms.example.edu",
        lti_context_id="ctx-1",
        exam_reference="exam-1",
        attempt_reference=str(uuid.uuid4()),
        status=status,
        consent_recorded_at=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
    )
    session.add(s)
    session.commit()
    session.refresh(s)
    return s


def _make_admin(
    session: Session,
    *,
    role: AdminRole,
    lms_user_reference: str,
) -> AdminUser:
    """Create the :class:`AdminUser` row only.

    The session-token ``sub`` claim is a participant id (UUID), not
    the LMS subject.  Tests that need to mint an admin session token
    must therefore create the matching :class:`Participant` row
    (with the same ``lms_user_reference``) and pass it to
    :func:`_admin_token`.  Use :func:`_make_admin_participant_pair`
    for the typical case.
    """

    admin = AdminUser(
        lti_issuer="https://lms.example.edu",
        lms_user_reference=lms_user_reference,
        display_name=lms_user_reference,
        role=role,
    )
    session.add(admin)
    session.commit()
    session.refresh(admin)
    return admin


def _make_admin_participant_pair(
    session: Session,
    *,
    role: AdminRole,
    lms_user_reference: str,
) -> tuple[Participant, AdminUser]:
    """Create both the :class:`Participant` and :class:`AdminUser`
    rows with the same ``lms_user_reference``.

    Mirrors the production shape created by
    :func:`proctoring_engine.lti.service.process_launch`.  Returns
    ``(participant, admin)``; pass the ``participant`` to
    :func:`_admin_token` so the session-token's ``sub`` claim can
    be bridged to the admin's ``lms_user_reference``.
    """

    participant = Participant(
        lti_issuer="https://lms.example.edu",
        lms_user_reference=lms_user_reference,
    )
    session.add(participant)
    session.commit()
    session.refresh(participant)
    admin = _make_admin(
        session, role=role, lms_user_reference=lms_user_reference
    )
    return participant, admin


def _make_flag(
    session: Session,
    *,
    exam_session: ExamSession,
    policy: PolicyConfig,
    severity: FlagSeverity = FlagSeverity.MEDIUM,
) -> Flag:
    flag = Flag(
        exam_session_id=exam_session.id,
        policy_config_id=policy.id,
        rule_code="gaze_away_frequency",
        severity=severity,
        confidence_score=0.9,
        confidence_lower=0.7,
        confidence_upper=0.95,
        detail={"gaze_away_count": 4, "gaze_warning_limit": 3},
    )
    session.add(flag)
    session.commit()
    session.refresh(flag)
    return flag


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestOrchestrationSettings:
    def test_minimum_token_length_accepted(self) -> None:
        # 32 bytes is the documented minimum; verify the boundary.
        s = OrchestrationSettings(internal_terminate_token="a" * 32)
        assert s.internal_terminate_token == "a" * 32

    def test_short_token_rejected(self) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            OrchestrationSettings(internal_terminate_token="a" * 31)

    def test_zero_retention_rejected(self) -> None:
        with pytest.raises(ValueError, match="retention_default_seconds"):
            OrchestrationSettings(
                internal_terminate_token="a" * 32,
                retention_default_seconds=0,
            )

    def test_negative_retention_rejected(self) -> None:
        with pytest.raises(ValueError, match="retention_default_seconds"):
            OrchestrationSettings(
                internal_terminate_token="a" * 32,
                retention_default_seconds=-1,
            )

    def test_set_and_reset_round_trip(
        self,
        internal_token: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("INTERNAL_TERMINATE_TOKEN", "env-default-token-1234567890abcdef")
        reset_orchestration_settings()
        original = get_orchestration_settings()
        custom = OrchestrationSettings(
            internal_terminate_token=internal_token,
            retention_default_seconds=12345,
        )
        set_orchestration_settings(custom)
        assert get_orchestration_settings() == custom
        reset_orchestration_settings()
        assert get_orchestration_settings() == original
        reset_orchestration_settings()

    def test_missing_env_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("INTERNAL_TERMINATE_TOKEN", raising=False)
        reset_orchestration_settings()
        with pytest.raises(ValueError, match="INTERNAL_TERMINATE_TOKEN"):
            get_orchestration_settings()
        reset_orchestration_settings()


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class TestStateMachine:
    @pytest.mark.parametrize(
        "current,target",
        [
            (SessionStatus.PENDING, SessionStatus.ACTIVE),
            (SessionStatus.PENDING, SessionStatus.TERMINATED),
            (SessionStatus.ACTIVE, SessionStatus.COMPLETED),
            (SessionStatus.ACTIVE, SessionStatus.TERMINATED),
            (SessionStatus.ACTIVE, SessionStatus.UNDER_REVIEW),
            (SessionStatus.COMPLETED, SessionStatus.UNDER_REVIEW),
            (SessionStatus.TERMINATED, SessionStatus.UNDER_REVIEW),
        ],
    )
    def test_allowed_transitions(
        self, current: SessionStatus, target: SessionStatus
    ) -> None:
        assert can_transition(current, target)
        assert_transition(current, target)  # does not raise

    @pytest.mark.parametrize(
        "current,target",
        [
            (SessionStatus.PENDING, SessionStatus.COMPLETED),
            (SessionStatus.PENDING, SessionStatus.UNDER_REVIEW),
            (SessionStatus.ACTIVE, SessionStatus.PENDING),
            (SessionStatus.COMPLETED, SessionStatus.ACTIVE),
            (SessionStatus.COMPLETED, SessionStatus.TERMINATED),
            (SessionStatus.TERMINATED, SessionStatus.ACTIVE),
            (SessionStatus.TERMINATED, SessionStatus.COMPLETED),
            (SessionStatus.TERMINATED, SessionStatus.TERMINATED),
            (SessionStatus.UNDER_REVIEW, SessionStatus.ACTIVE),
            (SessionStatus.UNDER_REVIEW, SessionStatus.COMPLETED),
            (SessionStatus.UNDER_REVIEW, SessionStatus.TERMINATED),
            (SessionStatus.UNDER_REVIEW, SessionStatus.UNDER_REVIEW),
        ],
    )
    def test_rejected_transitions(
        self, current: SessionStatus, target: SessionStatus
    ) -> None:
        assert not can_transition(current, target)
        with pytest.raises(InvalidSessionTransition):
            assert_transition(current, target)

    def test_invalid_transition_carries_status(
        self,
    ) -> None:
        with pytest.raises(InvalidSessionTransition) as ei:
            assert_transition(
                SessionStatus.UNDER_REVIEW, SessionStatus.ACTIVE
            )
        assert ei.value.current == SessionStatus.UNDER_REVIEW
        assert ei.value.target == SessionStatus.ACTIVE

    def test_apply_transition_persists(
        self,
        test_db: Session,
        active_policy: PolicyConfig,
    ) -> None:
        participant = _make_participant(test_db, lms_user_reference="u_unknown")
        session = _make_exam_session(
            test_db,
            participant=participant,
            policy=active_policy,
            status=SessionStatus.PENDING,
        )
        apply_transition(session, SessionStatus.ACTIVE, db=test_db)
        test_db.refresh(session)
        assert session.status == SessionStatus.ACTIVE

    def test_apply_rejected_transition_does_not_mutate(
        self,
        test_db: Session,
        active_policy: PolicyConfig,
    ) -> None:
        participant = _make_participant(test_db, lms_user_reference="u_unknown")
        session = _make_exam_session(
            test_db,
            participant=participant,
            policy=active_policy,
            status=SessionStatus.UNDER_REVIEW,
        )
        with pytest.raises(InvalidSessionTransition):
            apply_transition(
                session, SessionStatus.ACTIVE, db=test_db
            )
        # No mutation, no commit
        test_db.refresh(session)
        assert session.status == SessionStatus.UNDER_REVIEW

    def test_allowed_targets_matches_table(
        self,
    ) -> None:
        assert SessionStatus.UNDER_REVIEW not in allowed_targets(
            SessionStatus.UNDER_REVIEW
        )
        assert SessionStatus.UNDER_REVIEW in allowed_targets(
            SessionStatus.ACTIVE
        )


# ---------------------------------------------------------------------------
# Auth — Bearer parsing
# ---------------------------------------------------------------------------


class TestParseBearer:
    def test_well_formed(self) -> None:
        assert parse_bearer("Bearer abc123") == "abc123"

    def test_lowercase_scheme_accepted(self) -> None:
        assert parse_bearer("bearer abc123") == "abc123"

    def test_missing_returns_none(self) -> None:
        assert parse_bearer(None) is None

    def test_wrong_scheme(self) -> None:
        assert parse_bearer("Basic abc123") is None

    def test_only_scheme(self) -> None:
        assert parse_bearer("Bearer ") is None

    def test_extra_spaces_kept(self) -> None:
        # Tokens may contain characters that survive URL-safe encoding.
        # We require exactly one space between scheme and token.
        assert parse_bearer("Bearer abc 123") == "abc 123"


# ---------------------------------------------------------------------------
# Auth — internal terminate
# ---------------------------------------------------------------------------


class TestInternalTerminateAuth:
    def test_correct_token_passes(
        self, orchestration_settings: OrchestrationSettings
    ) -> None:
        # No exception is the success signal.
        require_internal_terminate_token(
            settings=orchestration_settings,
            authorization="Bearer " + orchestration_settings.internal_terminate_token,
        )

    def test_missing_header_raises(
        self, orchestration_settings: OrchestrationSettings
    ) -> None:
        with pytest.raises(Exception) as ei:  # HTTPException
            require_internal_terminate_token(
                settings=orchestration_settings, authorization=None
            )
        assert "internal_token_required" in str(ei.value)

    def test_wrong_scheme_raises(
        self, orchestration_settings: OrchestrationSettings
    ) -> None:
        with pytest.raises(Exception) as ei:
            require_internal_terminate_token(
                settings=orchestration_settings,
                authorization="Basic " + "x" * 32,
            )
        assert "internal_token_required" in str(ei.value)

    def test_wrong_token_raises(
        self, orchestration_settings: OrchestrationSettings
    ) -> None:
        with pytest.raises(Exception) as ei:
            require_internal_terminate_token(
                settings=orchestration_settings, authorization="Bearer wrong"
            )
        assert "internal_token_invalid" in str(ei.value)

    def test_empty_token_raises(
        self, orchestration_settings: OrchestrationSettings
    ) -> None:
        with pytest.raises(Exception) as ei:
            require_internal_terminate_token(
                settings=orchestration_settings, authorization="Bearer"
            )
        assert "internal_token_required" in str(ei.value)


# ---------------------------------------------------------------------------
# Routes — internal terminate
# ---------------------------------------------------------------------------


class TestInternalTerminateRoute:
    def _post(
        self,
        client: TestClient,
        session_id: str,
        flag_id: uuid.UUID,
        *,
        authorization: str | None = None,
    ):
        return client.post(
            f"/sessions/{session_id}/terminate",
            json={
                "triggering_flag_id": str(flag_id),
                "reason": "auto second-person",
            },
            headers={"Authorization": authorization} if authorization else {},
        )

    def test_correct_internal_token_terminates(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        active_policy: PolicyConfig,
        internal_token: str,
    ) -> None:
        app, db = app_factory()
        participant = _make_participant(db, lms_user_reference="u_unknown")
        session = _make_exam_session(
            db, participant=participant, policy=active_policy
        )
        flag = _make_flag(db, exam_session=session, policy=active_policy)
        client = TestClient(app)
        r = self._post(
            client, str(session.id), flag.id,
            authorization="Bearer " + internal_token,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["new_status"] == "terminated"
        assert body["triggering_flag_id"] == str(flag.id)
        # Status updated, TerminationRecord inserted.
        db.refresh(session)
        assert session.status == SessionStatus.TERMINATED
        from proctoring_engine.models import TerminationRecord
        records = db.execute(
            select(TerminationRecord).where(
                TerminationRecord.exam_session_id == session.id
            )
        ).scalars().all()
        assert len(records) == 1
        assert records[0].triggering_flag_id == flag.id

    def test_missing_internal_token_returns_401(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        active_policy: PolicyConfig,
    ) -> None:
        app, db = app_factory()
        participant = _make_participant(db, lms_user_reference="u_unknown")
        session = _make_exam_session(
            db, participant=participant, policy=active_policy
        )
        flag = _make_flag(db, exam_session=session, policy=active_policy)
        client = TestClient(app)
        r = self._post(client, str(session.id), flag.id)
        assert r.status_code == 401
        assert r.json()["detail"]["code"] == "internal_token_required"

    def test_wrong_internal_token_returns_403(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        active_policy: PolicyConfig,
    ) -> None:
        app, db = app_factory()
        participant = _make_participant(db, lms_user_reference="u_unknown")
        session = _make_exam_session(
            db, participant=participant, policy=active_policy
        )
        flag = _make_flag(db, exam_session=session, policy=active_policy)
        client = TestClient(app)
        r = self._post(
            client, str(session.id), flag.id,
            authorization="Bearer " + "wrong" * 8,
        )
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "internal_token_invalid"

    def test_learner_session_token_to_internal_route_returns_403(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        active_policy: PolicyConfig,
        lti_settings: LtiSettings,
    ) -> None:
        app, db = app_factory()
        participant = _make_participant(db, lms_user_reference="u_unknown")
        session = _make_exam_session(
            db, participant=participant, policy=active_policy
        )
        flag = _make_flag(db, exam_session=session, policy=active_policy)
        token = issue_session_token(
            participant_id=participant.id,
            exam_session_id=session.id,
            role=AppRole.LEARNER,
            settings=lti_settings,
        )
        client = TestClient(app)
        r = self._post(
            client, str(session.id), flag.id,
            authorization="Bearer " + token,
        )
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "internal_token_invalid"

    def test_instructor_session_token_to_internal_route_returns_403(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        active_policy: PolicyConfig,
        lti_settings: LtiSettings,
    ) -> None:
        app, db = app_factory()
        # Create both an admin user (for the session_token 'sub') and
        # the exam session.
        participant, admin = _make_admin_participant_pair(
            db, role=AdminRole.INSTRUCTOR, lms_user_reference="instr1"
        )
        session = _make_exam_session(
            db, participant=participant, policy=active_policy
        )
        flag = _make_flag(db, exam_session=session, policy=active_policy)
        token = issue_session_token(
            participant_id=admin.id,  # only matters for token shape
            exam_session_id=session.id,
            role=AppRole.INSTRUCTOR,
            settings=lti_settings,
        )
        client = TestClient(app)
        r = self._post(
            client, str(session.id), flag.id,
            authorization="Bearer " + token,
        )
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "internal_token_invalid"

    def test_invalid_session_transition_returns_409(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        active_policy: PolicyConfig,
        internal_token: str,
    ) -> None:
        app, db = app_factory()
        participant = _make_participant(db, lms_user_reference="u_unknown")
        session = _make_exam_session(
            db,
            participant=participant,
            policy=active_policy,
            status=SessionStatus.COMPLETED,
        )
        flag = _make_flag(db, exam_session=session, policy=active_policy)
        client = TestClient(app)
        r = self._post(
            client, str(session.id), flag.id,
            authorization="Bearer " + internal_token,
        )
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "invalid_session_transition"

    def test_unknown_flag_returns_404(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        active_policy: PolicyConfig,
        internal_token: str,
    ) -> None:
        app, db = app_factory()
        participant = _make_participant(db, lms_user_reference="u_unknown")
        session = _make_exam_session(
            db, participant=participant, policy=active_policy
        )
        client = TestClient(app)
        r = self._post(
            client, str(session.id), uuid.uuid4(),
            authorization="Bearer " + internal_token,
        )
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "flag_not_found"

    def test_unknown_session_returns_404(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        active_policy: PolicyConfig,
        internal_token: str,
    ) -> None:
        app, db = app_factory()
        client = TestClient(app)
        r = self._post(
            client, str(uuid.uuid4()), uuid.uuid4(),
            authorization="Bearer " + internal_token,
        )
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "session_not_found"

    def test_invalid_uuid_returns_404(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        internal_token: str,
    ) -> None:
        app, _ = app_factory()
        client = TestClient(app)
        r = self._post(
            client, "not-a-uuid", uuid.uuid4(),
            authorization="Bearer " + internal_token,
        )
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "session_not_found"

    def test_already_terminated_returns_409(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        active_policy: PolicyConfig,
        internal_token: str,
    ) -> None:
        app, db = app_factory()
        participant = _make_participant(db, lms_user_reference="u_unknown")
        session = _make_exam_session(
            db,
            participant=participant,
            policy=active_policy,
            status=SessionStatus.TERMINATED,
        )
        flag = _make_flag(db, exam_session=session, policy=active_policy)
        client = TestClient(app)
        r = self._post(
            client, str(session.id), flag.id,
            authorization="Bearer " + internal_token,
        )
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "invalid_session_transition"


# ---------------------------------------------------------------------------
# Admin service — policy config
# ---------------------------------------------------------------------------


def _post_policy(
    client: TestClient,
    *,
    body: dict,
    headers: dict | None = None,
) -> "object":  # Response
    return client.post("/admin/policy-config", json=body, headers=headers or {})


def _get_policy(client: TestClient, *, headers: dict | None = None, **params) -> "object":
    return client.get("/admin/policy-config", params=params, headers=headers or {})


def _admin_token(
    lti_settings: LtiSettings,
    *,
    admin_participant: Participant,
    session_id: uuid.UUID,
    role: AppRole,
) -> str:
    """Issue a session token for an admin.

    The :func:`proctoring_engine.orchestration._auth._load_admin_user`
    dependency joins ``claims.subject`` (a participant id) →
    :class:`Participant.lms_user_reference` → :class:`AdminUser.lms_user_reference`.
    :func:`proctoring_engine.lti.service.process_launch` upserts
    both rows on every launch; tests must mirror that shape with a
    paired ``Participant`` + ``AdminUser`` (see
    :func:`_make_admin_with_participant`).
    """

    return issue_session_token(
        participant_id=admin_participant.id,
        exam_session_id=session_id,
        role=role,
        settings=lti_settings,
    )


def _make_admin_with_participant(
    session: Session,
    *,
    role: AdminRole,
    lms_user_reference: str,
) -> tuple[Participant, AdminUser]:
    """DEPRECATED — use :func:`_make_admin_participant_pair`.

    Kept here so older imports keep working while the test
    sweep migrates to the renamed helper.
    """

    return _make_admin_participant_pair(
        session,
        role=role,
        lms_user_reference=lms_user_reference,
    )


class TestPolicyConfigEndpoints:
    def test_post_creates_new_row(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        lti_settings: LtiSettings,
    ) -> None:
        app, db = app_factory()
        participant, admin = _make_admin_participant_pair(
            db, role=AdminRole.INSTRUCTOR, lms_user_reference="admin1"
        )
        session = _make_exam_session(
            db, participant=participant, policy=_make_minimal_policy(db)
        )
        token = _admin_token(
            lti_settings, admin_participant=participant, session_id=session.id,
            role=AppRole.INSTRUCTOR,
        )
        client = TestClient(app)
        r = _post_policy(
            client,
            body={
                "name": "cs101-default",
                "second_face_confirmation_frames": 3,
                "gaze_min_duration_ms": 800,
                "gaze_window_seconds": 300,
                "gaze_warning_limit": 3,
                "gaze_termination_limit": 8,
                "medium_score_termination_threshold": 10.0,
            },
            headers={"Authorization": "Bearer " + token},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["name"] == "cs101-default"
        assert body["created_by_id"] == str(admin.id)

    def test_learner_token_to_admin_route_rejected(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        lti_settings: LtiSettings,
    ) -> None:
        app, db = app_factory()
        participant = _make_participant(db, lms_user_reference="u_unknown")
        session = _make_exam_session(
            db, participant=participant, policy=_make_minimal_policy(db)
        )
        token = issue_session_token(
            participant_id=participant.id,
            exam_session_id=session.id,
            role=AppRole.LEARNER,
            settings=lti_settings,
        )
        client = TestClient(app)
        r = _post_policy(
            client,
            body={
                "name": "x",
                "second_face_confirmation_frames": 3,
                "gaze_min_duration_ms": 800,
                "gaze_window_seconds": 300,
                "gaze_warning_limit": 3,
                "gaze_termination_limit": 8,
            },
            headers={"Authorization": "Bearer " + token},
        )
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "not_authorized"

    def test_post_without_admin_user_record_returns_403(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        lti_settings: LtiSettings,
    ) -> None:
        # The token has role=INSTRUCTOR but no AdminUser row exists
        # for that subject — the dependency 403s.
        app, db = app_factory()
        participant = _make_participant(db, lms_user_reference="u_unknown")
        session = _make_exam_session(
            db, participant=participant, policy=_make_minimal_policy(db)
        )
        # Issue a token whose ``sub`` points to a participant_id that
        # has NO matching AdminUser row — the auth dependency joins
        # through the participant to find the admin row, and the
        # absence of one is the 403 we want to verify.
        orphan_participant = Participant(
            lti_issuer="https://lms.example.edu",
            lms_user_reference="orphan-without-admin-row",
        )
        db.add(orphan_participant)
        db.commit()
        db.refresh(orphan_participant)
        token = _admin_token(
            lti_settings,
            admin_participant=orphan_participant,
            session_id=session.id,
            role=AppRole.INSTRUCTOR,
        )
        client = TestClient(app)
        r = _post_policy(
            client,
            body={
                "name": "y",
                "second_face_confirmation_frames": 3,
                "gaze_min_duration_ms": 800,
                "gaze_window_seconds": 300,
                "gaze_warning_limit": 3,
                "gaze_termination_limit": 8,
            },
            headers={"Authorization": "Bearer " + token},
        )
        assert r.status_code == 403

    def test_post_invalid_gaze_window_rejected(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        lti_settings: LtiSettings,
    ) -> None:
        app, db = app_factory()
        participant, admin = _make_admin_participant_pair(
            db, role=AdminRole.ADMIN, lms_user_reference="admin2"
        )
        session = _make_exam_session(
            db, participant=participant, policy=_make_minimal_policy(db)
        )
        token = _admin_token(
            lti_settings, admin_participant=participant, session_id=session.id,
            role=AppRole.ADMIN,
        )
        client = TestClient(app)
        r = _post_policy(
            client,
            body={
                "name": "bad",
                # gaze_min_duration_ms > gaze_window_seconds * 1000
                "second_face_confirmation_frames": 3,
                "gaze_min_duration_ms": 30000,
                "gaze_window_seconds": 5,
                "gaze_warning_limit": 3,
                "gaze_termination_limit": 8,
            },
            headers={"Authorization": "Bearer " + token},
        )
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "policy_versioning_error"

    def test_post_warning_greater_than_termination_rejected(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        lti_settings: LtiSettings,
    ) -> None:
        app, db = app_factory()
        participant, admin = _make_admin_participant_pair(
            db, role=AdminRole.ADMIN, lms_user_reference="admin3"
        )
        session = _make_exam_session(
            db, participant=participant, policy=_make_minimal_policy(db)
        )
        token = _admin_token(
            lti_settings, admin_participant=participant, session_id=session.id,
            role=AppRole.ADMIN,
        )
        client = TestClient(app)
        r = _post_policy(
            client,
            body={
                "name": "bad2",
                "second_face_confirmation_frames": 3,
                "gaze_min_duration_ms": 800,
                "gaze_window_seconds": 300,
                "gaze_warning_limit": 10,
                "gaze_termination_limit": 5,  # smaller than warning
            },
            headers={"Authorization": "Bearer " + token},
        )
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "policy_versioning_error"

    def test_post_retire_previous_supersedes(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        lti_settings: LtiSettings,
    ) -> None:
        """``retire_previous=True`` must produce a unique-name conflict
        under the v1 schema's ``uq_policy_configs_name`` constraint.

        The v1 schema enforces ``PolicyConfig.name`` as a column-level
        ``unique=True`` (see ``src/proctoring_engine/models.py``); the
        v1 design doc promises "name uniquely identifies a family of
        versions" but the schema does not yet model that.  This test
        documents the current behaviour: the ``retire_previous=True``
        request returns 422 because the second INSERT would collide
        with the just-retired row's name.

        Surfaced as an open decision in :mod:`SYSTEM_STATE.md` §5
        and :mod:`CONTEXT.md` §4 — the fix is a schema change
        (drop ``unique=True`` on ``PolicyConfig.name`` and replace
        it with a unique constraint on
        ``(name, is_active, retired_at IS NULL)``).
        """

        app, db = app_factory()
        participant, admin = _make_admin_participant_pair(
            db, role=AdminRole.ADMIN, lms_user_reference="admin4"
        )
        session = _make_exam_session(
            db, participant=participant, policy=_make_minimal_policy(db)
        )
        token = _admin_token(
            lti_settings, admin_participant=participant, session_id=session.id,
            role=AppRole.ADMIN,
        )
        client = TestClient(app)
        # Initial POST — succeeds.
        r1 = _post_policy(
            client,
            body={"name": "cs101-default"},
            headers={"Authorization": "Bearer " + token},
        )
        assert r1.status_code == 201
        first_id = r1.json()["id"]
        # Second POST with retire_previous — currently 422 because
        # the schema's name-uniqueness constraint blocks the new row.
        r3 = _post_policy(
            client,
            body={"name": "cs101-default", "retire_previous": True},
            headers={"Authorization": "Bearer " + token},
        )
        assert r3.status_code == 422
        # Old row is unchanged (service never got to commit the retire).
        db.expire_all()
        old = db.get(PolicyConfig, uuid.UUID(first_id))
        assert old is not None
        assert old.retired_at is None

    def test_get_lists_policies(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        lti_settings: LtiSettings,
    ) -> None:
        app, db = app_factory()
        participant, admin = _make_admin_participant_pair(
            db, role=AdminRole.ADMIN, lms_user_reference="admin5"
        )
        session = _make_exam_session(
            db, participant=participant, policy=_make_minimal_policy(db)
        )
        token = _admin_token(
            lti_settings, admin_participant=participant, session_id=session.id,
            role=AppRole.ADMIN,
        )
        client = TestClient(app)
        for n in ("p1", "p2"):
            _post_policy(
                client,
                body={
                    "name": n,
                    "second_face_confirmation_frames": 3,
                    "gaze_min_duration_ms": 800,
                    "gaze_window_seconds": 300,
                    "gaze_warning_limit": 3,
                    "gaze_termination_limit": 8,
                },
                headers={"Authorization": "Bearer " + token},
            )
        r = _get_policy(client, headers={"Authorization": "Bearer " + token})
        assert r.status_code == 200
        names = sorted(p["name"] for p in r.json())
        assert "p1" in names and "p2" in names


def _make_minimal_policy(session: Session) -> PolicyConfig:
    """Create a minimal valid :class:`PolicyConfig` row to satisfy the
    :class:`ExamSession` FK.
    """

    p = PolicyConfig(name="default-min", is_active=True)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


# ---------------------------------------------------------------------------
# Admin service — accommodation exemptions
# ---------------------------------------------------------------------------


class TestAccommodationExemptionEndpoints:
    def test_post_creates_row_with_approver(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        lti_settings: LtiSettings,
    ) -> None:
        app, db = app_factory()
        participant, admin = _make_admin_participant_pair(
            db, role=AdminRole.ADMIN, lms_user_reference="admin_e1"
        )
        session = _make_exam_session(
            db, participant=participant, policy=_make_minimal_policy(db)
        )
        token = _admin_token(
            lti_settings, admin_participant=participant, session_id=session.id,
            role=AppRole.ADMIN,
        )
        client = TestClient(app)
        r = client.post(
            "/admin/accommodation-exemptions",
            json={
                "participant_id": str(participant.id),
                "exam_reference": "exam-1",
                "object_class": "smartwatch",
                "approval_reason": "medical accommodation",
                "effective_at": datetime.now(timezone.utc).isoformat(),
            },
            headers={"Authorization": "Bearer " + token},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["approved_by_admin_id"] == str(admin.id)
        assert body["approved_by"] == admin.lms_user_reference

    def test_post_invalid_window_rejected(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        lti_settings: LtiSettings,
    ) -> None:
        app, db = app_factory()
        participant, admin = _make_admin_participant_pair(
            db, role=AdminRole.ADMIN, lms_user_reference="admin_e2"
        )
        session = _make_exam_session(
            db, participant=participant, policy=_make_minimal_policy(db)
        )
        token = _admin_token(
            lti_settings, admin_participant=participant, session_id=session.id,
            role=AppRole.ADMIN,
        )
        client = TestClient(app)
        now = datetime.now(timezone.utc)
        r = client.post(
            "/admin/accommodation-exemptions",
            json={
                "participant_id": str(participant.id),
                "exam_reference": "exam-1",
                "object_class": "smartwatch",
                "approval_reason": "x",
                "effective_at": now.isoformat(),
                "expires_at": (now - timedelta(seconds=10)).isoformat(),
            },
            headers={"Authorization": "Bearer " + token},
        )
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "exemption_validation_error"

    def test_get_lists_exemptions(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        lti_settings: LtiSettings,
    ) -> None:
        app, db = app_factory()
        participant, admin = _make_admin_participant_pair(
            db, role=AdminRole.ADMIN, lms_user_reference="admin_e3"
        )
        session = _make_exam_session(
            db, participant=participant, policy=_make_minimal_policy(db)
        )
        token = _admin_token(
            lti_settings, admin_participant=participant, session_id=session.id,
            role=AppRole.ADMIN,
        )
        client = TestClient(app)
        r = client.post(
            "/admin/accommodation-exemptions",
            json={
                "participant_id": str(participant.id),
                "exam_reference": "exam-1",
                "object_class": "smartwatch",
                "approval_reason": "x",
                "effective_at": datetime.now(timezone.utc).isoformat(),
            },
            headers={"Authorization": "Bearer " + token},
        )
        assert r.status_code == 201
        r2 = client.get(
            "/admin/accommodation-exemptions",
            headers={"Authorization": "Bearer " + token},
        )
        assert r2.status_code == 200
        rows = r2.json()
        assert len(rows) == 1
        assert rows[0]["object_class"] == "smartwatch"


# ---------------------------------------------------------------------------
# Routes — admin flag list
# ---------------------------------------------------------------------------


class TestAdminFlagsListEndpoint:
    def test_empty_session_returns_empty_list(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        lti_settings: LtiSettings,
    ) -> None:
        app, db = app_factory()
        participant, admin = _make_admin_participant_pair(
            db, role=AdminRole.PROCTOR, lms_user_reference="proctor1"
        )
        session = _make_exam_session(
            db, participant=participant, policy=_make_minimal_policy(db)
        )
        token = _admin_token(
            lti_settings, admin_participant=participant, session_id=session.id,
            role=AppRole.PROCTOR,
        )
        client = TestClient(app)
        r = client.get(
            f"/admin/flags/{session.id}",
            headers={"Authorization": "Bearer " + token},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["flags"] == []
        assert body["omitted_suppressed_count"] == 0

    def test_unknown_session_returns_404(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        lti_settings: LtiSettings,
    ) -> None:
        app, db = app_factory()
        participant, admin = _make_admin_participant_pair(
            db, role=AdminRole.PROCTOR, lms_user_reference="proctor2"
        )
        session = _make_exam_session(
            db, participant=participant, policy=_make_minimal_policy(db)
        )
        token = _admin_token(
            lti_settings, admin_participant=participant, session_id=session.id,
            role=AppRole.PROCTOR,
        )
        client = TestClient(app)
        r = client.get(
            f"/admin/flags/{uuid.uuid4()}",
            headers={"Authorization": "Bearer " + token},
        )
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "session_not_found"

    def test_session_with_flags(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        lti_settings: LtiSettings,
    ) -> None:
        app, db = app_factory()
        participant, admin = _make_admin_participant_pair(
            db, role=AdminRole.PROCTOR, lms_user_reference="proctor3"
        )
        session = _make_exam_session(
            db, participant=participant, policy=_make_minimal_policy(db)
        )
        _make_flag(
            db, exam_session=session, policy=session.policy_config,
            severity=FlagSeverity.MEDIUM,
        )
        _make_flag(
            db, exam_session=session, policy=session.policy_config,
            severity=FlagSeverity.CRITICAL,
        )
        token = _admin_token(
            lti_settings, admin_participant=participant, session_id=session.id,
            role=AppRole.PROCTOR,
        )
        client = TestClient(app)
        r = client.get(
            f"/admin/flags/{session.id}",
            headers={"Authorization": "Bearer " + token},
        )
        assert r.status_code == 200
        body = r.json()
        assert len(body["flags"]) == 2


# ---------------------------------------------------------------------------
# Routes — proctor review
# ---------------------------------------------------------------------------


class TestProctorReviewEndpoint:
    def test_upheld_review_transitions_active_to_under_review(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        lti_settings: LtiSettings,
    ) -> None:
        app, db = app_factory()
        participant, admin = _make_admin_participant_pair(
            db, role=AdminRole.PROCTOR, lms_user_reference="pr1"
        )
        session = _make_exam_session(
            db,
            participant=participant,
            policy=_make_minimal_policy(db),
            status=SessionStatus.ACTIVE,
        )
        flag = _make_flag(
            db, exam_session=session, policy=session.policy_config
        )
        token = _admin_token(
            lti_settings, admin_participant=participant, session_id=session.id,
            role=AppRole.PROCTOR,
        )
        client = TestClient(app)
        r = client.post(
            f"/admin/flags/{flag.id}/review",
            json={"decision": "upheld", "notes": "confirmed"},
            headers={"Authorization": "Bearer " + token},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["decision"] == "upheld"
        assert body["session_status"] == "under_review"
        db.refresh(session)
        assert session.status == SessionStatus.UNDER_REVIEW
        # The Flag row is unchanged (append-only invariant)
        db.refresh(flag)
        assert flag.severity == FlagSeverity.MEDIUM

    def test_overturned_review_does_not_transition(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        lti_settings: LtiSettings,
    ) -> None:
        app, db = app_factory()
        participant, admin = _make_admin_participant_pair(
            db, role=AdminRole.PROCTOR, lms_user_reference="pr2"
        )
        session = _make_exam_session(
            db,
            participant=participant,
            policy=_make_minimal_policy(db),
            status=SessionStatus.ACTIVE,
        )
        flag = _make_flag(
            db, exam_session=session, policy=session.policy_config
        )
        token = _admin_token(
            lti_settings, admin_participant=participant, session_id=session.id,
            role=AppRole.PROCTOR,
        )
        client = TestClient(app)
        r = client.post(
            f"/admin/flags/{flag.id}/review",
            json={"decision": "overturned", "notes": "false positive"},
            headers={"Authorization": "Bearer " + token},
        )
        assert r.status_code == 201
        db.refresh(session)
        assert session.status == SessionStatus.ACTIVE

    def test_unknown_flag_returns_404(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        lti_settings: LtiSettings,
    ) -> None:
        app, db = app_factory()
        participant, admin = _make_admin_participant_pair(
            db, role=AdminRole.PROCTOR, lms_user_reference="pr3"
        )
        session = _make_exam_session(
            db, participant=participant, policy=_make_minimal_policy(db)
        )
        token = _admin_token(
            lti_settings, admin_participant=participant, session_id=session.id,
            role=AppRole.PROCTOR,
        )
        client = TestClient(app)
        r = client.post(
            f"/admin/flags/{uuid.uuid4()}/review",
            json={"decision": "upheld"},
            headers={"Authorization": "Bearer " + token},
        )
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "flag_not_found"


# ---------------------------------------------------------------------------
# Routes — session status
# ---------------------------------------------------------------------------


class TestSessionStatusRoute:
    def test_learner_owner_sees_status(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        lti_settings: LtiSettings,
    ) -> None:
        app, db = app_factory()
        participant = _make_participant(db, lms_user_reference="u_unknown")
        session = _make_exam_session(
            db, participant=participant, policy=_make_minimal_policy(db)
        )
        token = issue_session_token(
            participant_id=participant.id,
            exam_session_id=session.id,
            role=AppRole.LEARNER,
            settings=lti_settings,
        )
        client = TestClient(app)
        r = client.get(
            f"/sessions/{session.id}/status",
            headers={"Authorization": "Bearer " + token},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "pending"
        assert body["consent_recorded"] is True

    def test_learner_non_owner_returns_403(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        lti_settings: LtiSettings,
    ) -> None:
        app, db = app_factory()
        owner = _make_participant(db, lms_user_reference="owner")
        other = _make_participant(db, lms_user_reference="other")
        session = _make_exam_session(
            db, participant=owner, policy=_make_minimal_policy(db)
        )
        token = issue_session_token(
            participant_id=other.id,
            exam_session_id=session.id,
            role=AppRole.LEARNER,
            settings=lti_settings,
        )
        client = TestClient(app)
        r = client.get(
            f"/sessions/{session.id}/status",
            headers={"Authorization": "Bearer " + token},
        )
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "not_authorized"

    def test_admin_can_read_any_session(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        lti_settings: LtiSettings,
    ) -> None:
        app, db = app_factory()
        participant, admin = _make_admin_participant_pair(
            db, role=AdminRole.ADMIN, lms_user_reference="admin_r1"
        )
        session = _make_exam_session(
            db, participant=participant, policy=_make_minimal_policy(db)
        )
        token = _admin_token(
            lti_settings, admin_participant=participant, session_id=session.id,
            role=AppRole.ADMIN,
        )
        client = TestClient(app)
        r = client.get(
            f"/sessions/{session.id}/status",
            headers={"Authorization": "Bearer " + token},
        )
        assert r.status_code == 200

    def test_unknown_session_returns_404(
        self,
        app_factory: Callable[[], tuple[FastAPI, Session]],
        lti_settings: LtiSettings,
    ) -> None:
        app, db = app_factory()
        participant, admin = _make_admin_participant_pair(
            db, role=AdminRole.ADMIN, lms_user_reference="admin_r2"
        )
        session = _make_exam_session(
            db, participant=participant, policy=_make_minimal_policy(db)
        )
        token = _admin_token(
            lti_settings, admin_participant=participant, session_id=session.id,
            role=AppRole.ADMIN,
        )
        client = TestClient(app)
        r = client.get(
            f"/sessions/{uuid.uuid4()}/status",
            headers={"Authorization": "Bearer " + token},
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Service — flag persistence
# ---------------------------------------------------------------------------


class TestPersistFlagDecision:
    def test_inserts_flag_and_links(
        self,
        test_db: Session,
        active_policy: PolicyConfig,
    ) -> None:
        from proctoring_engine.fusion._types import FlagDecision
        from proctoring_engine.inference._types import ConfidenceInterval
        from proctoring_engine.orchestration import persist_flag_decision

        participant = _make_participant(test_db, lms_user_reference="u_unknown")
        session = _make_exam_session(
            test_db, participant=participant, policy=active_policy
        )
        te = TelemetryEvent(
            exam_session_id=session.id,
            modality=TelemetryModality.FACE,
            event_type="second_person",
            occurred_at=datetime.now(timezone.utc),
            confidence=0.99,
        )
        test_db.add(te)
        test_db.commit()
        test_db.refresh(te)

        decision = FlagDecision(
            rule_code="second_person",
            severity="critical",
            confidence=ConfidenceInterval(
                lower=0.95, score=0.99, upper=1.0
            ),
            triggered_termination=True,
            contributing_event_ids=(te.id,),
        )
        flag = persist_flag_decision(
            test_db, decision, exam_session=session
        )
        assert flag.rule_code == "second_person"
        assert flag.severity == FlagSeverity.CRITICAL
        assert flag.triggered_termination is True
        # FlagTelemetryEvent link
        links = test_db.execute(
            select(FlagTelemetryEvent).where(
                FlagTelemetryEvent.flag_id == flag.id
            )
        ).scalars().all()
        assert len(links) == 1
        assert links[0].telemetry_event_id == te.id

    def test_unknown_severity_rejected(
        self,
        test_db: Session,
        active_policy: PolicyConfig,
    ) -> None:
        from proctoring_engine.fusion._types import FlagDecision
        from proctoring_engine.inference._types import ConfidenceInterval
        from proctoring_engine.orchestration import (
            FlagPersistenceError,
            persist_flag_decision,
        )

        participant = _make_participant(test_db, lms_user_reference="u_unknown")
        session = _make_exam_session(
            test_db, participant=participant, policy=active_policy
        )
        with pytest.raises(FlagPersistenceError):
            persist_flag_decision(
                test_db,
                FlagDecision(
                    rule_code="x",
                    severity="bogus",
                    confidence=ConfidenceInterval(
                        lower=0.0, score=0.0, upper=0.0
                    ),
                ),
                exam_session=session,
            )

    def test_score_delta_increments_accumulator(
        self,
        test_db: Session,
        active_policy: PolicyConfig,
    ) -> None:
        from proctoring_engine.fusion._types import FlagDecision
        from proctoring_engine.inference._types import ConfidenceInterval
        from proctoring_engine.orchestration import persist_flag_decision

        participant = _make_participant(test_db, lms_user_reference="u_unknown")
        session = _make_exam_session(
            test_db, participant=participant, policy=active_policy
        )
        decision = FlagDecision(
            rule_code="gaze_away_frequency",
            severity="medium",
            confidence=ConfidenceInterval(
                lower=0.7, score=0.8, upper=0.9
            ),
            score_delta=1.5,
        )
        persist_flag_decision(
            test_db, decision, exam_session=session
        )
        test_db.refresh(session)
        assert float(session.accumulated_medium_score) == 1.5
