"""Standalone test server for Gateway End-to-End integration tests.

Exposes a FastAPI app with the real WebSocket router mounted, pre-populated
with test sessions and issued JWT tokens.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from proctoring_engine.lti import LtiSettings, issue_session_token
from proctoring_engine.lti.roles import AppRole
from proctoring_engine.models import (
    Base,
    ExamSession,
    Participant,
    PolicyConfig,
    SessionStatus,
)
from proctoring_engine.websocket.routes import (
    _WsRouterDeps,
    build_ws_router,
)
from proctoring_engine.websocket.server import TelemetryEventBuffer

import logging

logging.basicConfig(level=logging.DEBUG, format="[%(name)s] %(levelname)s: %(message)s")

NOW = datetime.now(timezone.utc)
SECRET = "test-secret-key-for-gateway-e2e-32bytes-min"


def create_app_and_tokens():
    settings = LtiSettings(
        tool_client_id="proctoring-engine-test",
        launch_url="http://localhost:8000/lti/launch",
        session_token_secret=SECRET,
        oidc_http_timeout_seconds=1.0,
    )

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    db = SessionLocal()

    # Create policy
    policy = PolicyConfig(name="e2e-policy", is_active=True)
    db.add(policy)
    db.commit()

    # Create participant
    p = Participant(
        lti_issuer="https://lms.example.edu",
        lms_user_reference="student-e2e",
        display_name="E2E Student",
    )
    db.add(p)
    db.commit()

    # Session 1: Valid active session with consent
    sess_valid = ExamSession(
        participant_id=p.id,
        policy_config_id=policy.id,
        lti_issuer="https://lms.example.edu",
        lti_context_id="course-1:exam-1",
        exam_reference="exam-1",
        attempt_reference=str(uuid.uuid4()),
        status=SessionStatus.ACTIVE,
        consent_recorded_at=NOW,
        started_at=NOW,
    )
    db.add(sess_valid)

    # Session 2: Active session WITHOUT consent
    sess_no_consent = ExamSession(
        participant_id=p.id,
        policy_config_id=policy.id,
        lti_issuer="https://lms.example.edu",
        lti_context_id="course-1:exam-1",
        exam_reference="exam-1",
        attempt_reference=str(uuid.uuid4()),
        status=SessionStatus.ACTIVE,
        consent_recorded_at=None,
        started_at=NOW,
    )
    db.add(sess_no_consent)

    # Session 3: TERMINATED session
    sess_terminated = ExamSession(
        participant_id=p.id,
        policy_config_id=policy.id,
        lti_issuer="https://lms.example.edu",
        lti_context_id="course-1:exam-1",
        exam_reference="exam-1",
        attempt_reference=str(uuid.uuid4()),
        status=SessionStatus.TERMINATED,
        consent_recorded_at=NOW,
        started_at=NOW,
    )
    db.add(sess_terminated)
    db.commit()

    # Issue tokens
    valid_learner_token = issue_session_token(
        p.id, sess_valid.id, AppRole.LEARNER, settings=settings, now=NOW
    )
    instructor_token = issue_session_token(
        p.id, sess_valid.id, AppRole.INSTRUCTOR, settings=settings, now=NOW
    )
    no_consent_token = issue_session_token(
        p.id, sess_no_consent.id, AppRole.LEARNER, settings=settings, now=NOW
    )
    terminated_token = issue_session_token(
        p.id, sess_terminated.id, AppRole.LEARNER, settings=settings, now=NOW
    )

    event_buffer = TelemetryEventBuffer(maxlen=128)
    deps = _WsRouterDeps(
        settings=settings,
        get_db=lambda: SessionLocal(),
        event_buffer=event_buffer,
        heartbeat_interval_seconds=10.0,
        heartbeat_timeout_seconds=20.0,
    )

    app = FastAPI()

    # Wrap the ws route with logging
    router = build_ws_router(deps)
    app.include_router(router)

    tokens = {
        "valid_session_id": str(sess_valid.id),
        "valid_learner_token": valid_learner_token,
        "instructor_token": instructor_token,
        "no_consent_session_id": str(sess_no_consent.id),
        "no_consent_token": no_consent_token,
        "terminated_session_id": str(sess_terminated.id),
        "terminated_token": terminated_token,
        "participant_id": str(p.id),
    }

    return app, tokens


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    args = parser.parse_args()

    app, tokens = create_app_and_tokens()

    # Write tokens to stdout before server starts
    print(f"READY:{json.dumps(tokens)}", flush=True)

    config = uvicorn.Config(app=app, host=args.host, port=args.port, log_level="info")
    server = uvicorn.Server(config)
    server.run()


if __name__ == "__main__":
    main()
