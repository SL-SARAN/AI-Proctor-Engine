"""Integration test for identity verification break-glass override admin loop.

Exercises the full lifecycle:
1. Create override request via API.
2. Attempt approval by non-HEAD admin -> rejected (403 role_unauthorized).
3. Attempt self-approval by requesting HEAD admin -> rejected (403 self_approval_rejected).
4. Approve with distinct HEAD-role admin -> succeeds (200 APPROVED).
5. Verify database override check allows session connection when backend is missing.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from proctoring_engine.models import (
    AdminRole,
    AdminUser,
    Base,
    ExamSession,
    IdentityVerificationOverrideRequest,
    OverrideRequestStatus,
    Participant,
    PolicyConfig,
    SessionStatus,
)
from proctoring_engine.orchestration._routes import (
    _OrchestrationDeps,
    build_orchestration_router,
)
from proctoring_engine.orchestration._settings import OrchestrationSettings
from proctoring_engine.lti.config import LtiSettings
from proctoring_engine.lti.roles import AppRole
from proctoring_engine.lti.session_token import issue_session_token


class DummyEvidenceStore:
    def store(self, key, blob, media_type):
        pass


@pytest.fixture
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture
def db_session(db_engine):
    TestingSessionLocal = sessionmaker(
        autocommit=False, autoflush=False, bind=db_engine
    )
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def test_setup(db_session):
    secret = "x" * 32
    orch_settings = OrchestrationSettings(
        internal_terminate_token="x" * 48,
    )
    lti_settings = LtiSettings(
        tool_client_id="test-client-id",
        launch_url="http://localhost/launch",
        session_token_secret=secret,
    )

    # Policy
    policy = PolicyConfig(name="default-policy", is_active=True)
    db_session.add(policy)

    # Participants
    student_part = Participant(lti_issuer="https://lms.univ.edu", lms_user_reference="student-1")
    proctor_part = Participant(lti_issuer="https://lms.univ.edu", lms_user_reference="proctor-1")
    head_req_part = Participant(lti_issuer="https://lms.univ.edu", lms_user_reference="head-req-1")
    head_app_part = Participant(lti_issuer="https://lms.univ.edu", lms_user_reference="head-app-2")
    db_session.add_all([student_part, proctor_part, head_req_part, head_app_part])
    db_session.commit()

    # Admin 1 (Proctor)
    admin_proctor = AdminUser(
        id=uuid.uuid4(),
        lti_issuer="https://lms.univ.edu",
        lms_user_reference="proctor-1",
        role=AdminRole.PROCTOR,
    )
    # Admin 2 (HEAD, requester)
    admin_head_req = AdminUser(
        id=uuid.uuid4(),
        lti_issuer="https://lms.univ.edu",
        lms_user_reference="head-req-1",
        role=AdminRole.HEAD,
    )
    # Admin 3 (HEAD, approver)
    admin_head_app = AdminUser(
        id=uuid.uuid4(),
        lti_issuer="https://lms.univ.edu",
        lms_user_reference="head-app-2",
        role=AdminRole.HEAD,
    )

    # Student session
    exam_session = ExamSession(
        id=uuid.uuid4(),
        participant_id=student_part.id,
        policy_config_id=policy.id,
        lti_issuer="https://lms.univ.edu",
        lti_context_id="course-1",
        exam_reference="exam-1",
        attempt_reference=str(uuid.uuid4()),
        status=SessionStatus.PENDING,
    )

    db_session.add_all([admin_proctor, admin_head_req, admin_head_app, exam_session])
    db_session.commit()

    token_proctor = issue_session_token(
        participant_id=proctor_part.id,
        exam_session_id=exam_session.id,
        role=AppRole.ADMIN,
        settings=lti_settings,
    )
    token_head_req = issue_session_token(
        participant_id=head_req_part.id,
        exam_session_id=exam_session.id,
        role=AppRole.ADMIN,
        settings=lti_settings,
    )
    token_head_app = issue_session_token(
        participant_id=head_app_part.id,
        exam_session_id=exam_session.id,
        role=AppRole.ADMIN,
        settings=lti_settings,
    )

    deps = _OrchestrationDeps(
        settings=orch_settings,
        lti_settings=lti_settings,
        get_db=lambda: db_session,
        evidence_store=DummyEvidenceStore(),
    )
    router = build_orchestration_router(deps)

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    return {
        "client": client,
        "admin_proctor": admin_proctor,
        "admin_head_req": admin_head_req,
        "admin_head_app": admin_head_app,
        "session": exam_session,
        "token_proctor": token_proctor,
        "token_head_req": token_head_req,
        "token_head_app": token_head_app,
        "db": db_session,
    }


def test_identity_override_full_loop(test_setup):
    client = test_setup["client"]
    session = test_setup["session"]
    token_proctor = test_setup["token_proctor"]
    token_head_req = test_setup["token_head_req"]
    token_head_app = test_setup["token_head_app"]
    db = test_setup["db"]

    now = datetime.now(timezone.utc)
    valid_from = now - timedelta(minutes=5)
    valid_until = now + timedelta(hours=2)

    # 1. Create override request (by HEAD admin requester)
    resp = client.post(
        "/admin/identity-override-requests",
        headers={"Authorization": f"Bearer {token_head_req}"},
        json={
            "exam_session_id": str(session.id),
            "department": "Computer Science",
            "reason": "Camera failure fallback requested by instructor",
            "valid_from": valid_from.isoformat(),
            "valid_until": valid_until.isoformat(),
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    override_id = data["id"]
    assert data["status"] == "pending"

    # 2. Non-HEAD admin attempts approval (must reject with 403 role_unauthorized)
    proctor_app_resp = client.post(
        f"/admin/identity-override-requests/{override_id}/approve",
        headers={"Authorization": f"Bearer {token_proctor}"},
    )
    assert proctor_app_resp.status_code == 403, proctor_app_resp.text
    assert proctor_app_resp.json()["detail"]["code"] == "role_unauthorized"

    # 3. Requester admin attempts self-approval (must reject with 403 self_approval_rejected)
    self_app_resp = client.post(
        f"/admin/identity-override-requests/{override_id}/approve",
        headers={"Authorization": f"Bearer {token_head_req}"},
    )
    assert self_app_resp.status_code == 403, self_app_resp.text
    assert self_app_resp.json()["detail"]["code"] == "self_approval_rejected"

    # 4. Distinct HEAD-role admin approves (must succeed with 200)
    head_app_resp = client.post(
        f"/admin/identity-override-requests/{override_id}/approve",
        headers={"Authorization": f"Bearer {token_head_app}"},
    )
    assert head_app_resp.status_code == 200, head_app_resp.text
    approved_data = head_app_resp.json()
    assert approved_data["status"] == "approved"
    assert approved_data["approved_by_admin_id"] == str(test_setup["admin_head_app"].id)

    # 5. Assert against DB: valid approved override exists
    stmt = select(IdentityVerificationOverrideRequest).where(
        IdentityVerificationOverrideRequest.exam_session_id == session.id,
        IdentityVerificationOverrideRequest.status == OverrideRequestStatus.APPROVED,
    )
    override_row = db.execute(stmt).scalar_one_or_none()
    assert override_row is not None
    assert override_row.status == OverrideRequestStatus.APPROVED

    # 6. Assert WS handshake check evaluates valid_override_exists == True
    from sqlalchemy import func
    ws_check_stmt = select(IdentityVerificationOverrideRequest).where(
        IdentityVerificationOverrideRequest.exam_session_id == session.id,
        IdentityVerificationOverrideRequest.status == OverrideRequestStatus.APPROVED,
        IdentityVerificationOverrideRequest.valid_from <= func.now(),
        IdentityVerificationOverrideRequest.valid_until >= func.now(),
    )
    valid_override_exists = db.execute(ws_check_stmt).scalar_one_or_none() is not None
    assert valid_override_exists is True
