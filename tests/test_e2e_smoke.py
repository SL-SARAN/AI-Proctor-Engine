"""End-to-end smoke test for the v1 pipeline (turn 10).

This is the first test that exercises the **full client-to-flag loop**
through a real FastAPI app: a fake LTI launch issues a session token,
the client opens a WebSocket with that token, streams telemetry
through the real pipeline, and a flag fires. Nothing is mocked except
the OIDC discovery + JWKS endpoints (which is the same pattern
``tests/test_lti_routes.py`` uses for the launch flow).

**Path exercised:** the second-person detection rule
(``RULE_SECOND_PERSON = "second_person"`` in
``src/proctoring_engine/fusion/aggregator.py``). The aggregator
emits a CRITICAL flag with ``triggered_termination=True`` after
``second_face_confirmation_frames`` consecutive frames report
``face_count >= 2``. The default ``PolicySnapshot`` built in
``src/proctoring_engine/websocket/routes.py`` uses
``second_face_confirmation_frames=3``, so the test sends three
``telemetry_light`` envelopes.

**Why not identity-match?** Identity-match's
``_AlwaysZeroBackend`` fallback (``src/proctoring_engine/orchestration/_frame_dispatcher.py:835``)
is an organic Turn 9a placeholder, not a designed fail-closed/block-by-default
mechanism — it lets the session proceed and fires a CRITICAL
``identity_mismatch`` flag on the first sampled window. There is no
``ExamSession.identity_verification_status`` field
grep-verified across the codebase, so the proctor reviewing the flag
cannot distinguish "library not installed" from "looks like
impersonation." Avoiding the heavy-frame path entirely (sending only
``telemetry_light`` events) sidesteps this for the smoke test;
the identity-match parked item is still parked.

**Contract drift surfaced (not fixed in this turn):** the Python
``KillSwitchPayload`` docstring and the TypeScript client's
``REASON_MESSAGES`` map (in ``client/src/kill-switch.ts``) use keys
like ``"second_person_detected"``, but the actual emitted values are
the ``RULE_SECOND_PERSON`` constant (``"second_person"``). The smoke
test asserts the actual emitted values (the Python ``RULE_*`` constants
are the source of truth) and surfaces this drift in the turn's
output. Aligning the two is a separate atomic turn.

**Test design:** one test function with eight phases and a polling
helper for the asynchronous persistence step. SQLite-only — does not
require ``INTEGRATION_DATABASE_URL`` and is not marked as an
integration test. Uses ``StaticPool`` with
``check_same_thread=False`` (mirroring ``tests/test_websocket.py``) so
the persistence worker thread can write to the same DB the test
reads, plus ``PRAGMA foreign_keys=ON`` (mirroring
``tests/conftest.py``) so the FK constraints on the worker writes
are enforced.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from proctoring_engine.evidence._s3 import InMemoryEvidenceStore
from proctoring_engine.lti import (
    InMemoryLaunchStateStore,
    JwksCache,
    LtiSettings,
    OidcDiscoveryCache,
    build_lti_router,
    decode_session_token,
)
from proctoring_engine.lti.roles import AppRole
from proctoring_engine.lti.routes import _RouterDeps as _LtiRouterDeps
from proctoring_engine.models import (
    Base,
    EvidenceArtifact,
    ExamSession,
    Flag,
    FlagSeverity,
    Participant,
    PolicyConfig,
    SessionStatus,
    TelemetryEvent,
    TerminationRecord,
)
from proctoring_engine.orchestration import (
    OrchestrationSettings,
    build_orchestration_router,
)
from proctoring_engine.orchestration._routes import (
    _OrchestrationDeps,
)
from proctoring_engine.websocket import (
    TelemetryEventBuffer,
    build_ws_router,
)
from proctoring_engine.websocket.routes import _WsRouterDeps

from .integration.oidc_test_double import (
    LEARNER_URI,
    build_signed_launch_claims,
    make_test_oidc_setup,
    register_oidc_responses,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The time the smoke test waits for the persistence worker to finish
#: writing before failing the assertion. 500ms is well above the
#: dispatcher's 50ms empty-buffer sleep + the worker's 50ms queue.get
#: timeout + the SQLite round-trip; even on a busy CI runner 500ms is
#: plenty. If this constant is too tight the test will fail
#: intermittently; bump it if that happens.
PERSIST_DEADLINE_SECONDS = 5.0

#: The polling interval the test uses against ``PERSIST_DEADLINE_SECONDS``.
#: 50ms matches the dispatcher / worker idle cadence so the test
#: resolves at the first moment the row is visible.
PERSIST_POLL_INTERVAL_SECONDS = 0.05


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def lti_settings() -> LtiSettings:
    """``LtiSettings`` configured for the smoke test.

    Mirrors the fixture in ``tests/test_lti_routes.py`` and
    ``tests/test_orchestration.py`` so the launch flow's
    ``process_launch`` resolves the policy by the same name the
    test creates.
    """

    return LtiSettings(
        tool_client_id="proctoring-engine",
        launch_url="http://localhost:8000/lti/launch",
        session_token_secret="x" * 32,
        oidc_http_timeout_seconds=1.0,
    )


@pytest.fixture()
def orchestration_settings() -> OrchestrationSettings:
    """``OrchestrationSettings`` with the minimum 32-byte
    ``internal_terminate_token`` and a 1-day retention window
    for the evidence-seal step.
    """

    return OrchestrationSettings(
        internal_terminate_token="t" * 32,
        retention_default_seconds=86_400,
    )


@pytest.fixture()
def test_db_engine():
    """A SQLite in-memory engine with ``StaticPool`` AND
    ``PRAGMA foreign_keys=ON`` so cross-thread persistence-worker
    writes are visible to the test thread AND the FK constraints on
    ``Flag``/``FlagTelemetryEvent``/``TelemetryEvent``/``EvidenceArtifact``
    are enforced.

    The combination mirrors ``tests/test_websocket.py``'s
    ``StaticPool`` + ``check_same_thread=False`` shape (the
    persistence worker thread writes to the same connection the
    test reads) plus ``tests/conftest.py``'s
    ``enable_foreign_keys`` listener (the FK constraints matter
    for the ORM validator at the worker write path).

    Returns the engine (not a Session) because multiple threads
    need to call ``Session(bind=engine)`` and SQLAlchemy requires
    a session to be used by exactly one thread at a time — sharing
    a session across threads detaches the loaded instances when
    the other thread closes the session.  The shared engine +
    ``StaticPool`` is the right granularity.
    """

    engine: Engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture()
def test_db(test_db_engine) -> Session:
    """A per-thread ``Session`` bound to ``test_db_engine``.

    The test thread owns this session for its queries; the WS
    handler and the persistence worker each open their own
    sessions via ``deps.get_db`` (also bound to the same engine).
    All three sessions see the same data because they share the
    underlying connection (StaticPool) and the worker commits
    are immediately visible to a fresh query.
    """

    SessionLocal = sessionmaker(bind=test_db_engine, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def evidence_store() -> InMemoryEvidenceStore:
    return InMemoryEvidenceStore()


@pytest.fixture()
def state_store() -> InMemoryLaunchStateStore:
    return InMemoryLaunchStateStore(ttl_seconds=600)


@pytest.fixture()
def jwks_cache() -> JwksCache:
    return JwksCache()


@pytest.fixture()
def discovery_cache() -> OidcDiscoveryCache:
    return OidcDiscoveryCache()


@pytest.fixture()
def event_buffer() -> TelemetryEventBuffer:
    return TelemetryEventBuffer(maxlen=128)


@pytest.fixture()
def active_policy(test_db: Session) -> PolicyConfig:
    """A versioned ``PolicyConfig`` with the name the LTI launch's
    ``custom.policy_config_name`` claim will resolve against.
    """

    policy = PolicyConfig(name="cs101-default", is_active=True)
    test_db.add(policy)
    test_db.commit()
    test_db.refresh(policy)
    return policy


@pytest.fixture()
def app_factory(
    test_db_engine,
    lti_settings: LtiSettings,
    orchestration_settings: OrchestrationSettings,
    evidence_store: InMemoryEvidenceStore,
    state_store: InMemoryLaunchStateStore,
    jwks_cache: JwksCache,
    discovery_cache: OidcDiscoveryCache,
    event_buffer: TelemetryEventBuffer,
):
    """A factory that builds a fresh FastAPI app with all three
    routers mounted (LTI, WebSocket, orchestration) sharing the
    same per-test dependencies.

    The app composition mirrors ``src/proctoring_engine/api.py``
    (``_lifespan``) — three routers, all wired against the same
    ``get_db`` factory, the same ``LtiSettings``, the same
    ``InMemoryEvidenceStore``, the same ``TelemetryEventBuffer``.

    ``get_db`` returns a **fresh** ``Session`` per call (bound to
    the shared engine).  The FastAPI WS handler runs in a worker
    thread; if it closed a shared session, the test thread's
    loaded instances would detach.  Each thread gets its own
    session; the underlying engine is shared via StaticPool.
    """

    def _build() -> FastAPI:
        SessionLocal = sessionmaker(
            bind=test_db_engine, expire_on_commit=False
        )

        def _http_client_factory():
            import httpx as _httpx

            return _httpx.AsyncClient(timeout=1.0)

        def _get_db() -> Session:
            return SessionLocal()

        lti_deps = _LtiRouterDeps(
            settings=lti_settings,
            state_store=state_store,
            jwks_cache=jwks_cache,
            discovery_cache=discovery_cache,
            http_client_factory=_http_client_factory,
            get_db=_get_db,
        )
        ws_deps = _WsRouterDeps(
            settings=lti_settings,
            get_db=_get_db,
            event_buffer=event_buffer,
            heartbeat_interval_seconds=300,  # Long enough to avoid heartbeat churn in tests.
            heartbeat_timeout_seconds=300,
        )
        orch_deps = _OrchestrationDeps(
            settings=orchestration_settings,
            lti_settings=lti_settings,
            get_db=_get_db,
            evidence_store=evidence_store,
        )

        app = FastAPI()
        app.include_router(build_lti_router(lti_deps))
        app.include_router(build_ws_router(ws_deps))
        app.include_router(build_orchestration_router(orch_deps))
        return app

    return _build


@pytest.fixture()
def e2e_client(app_factory) -> TestClient:
    """A ``TestClient`` bound to the per-test FastAPI app."""

    return TestClient(app_factory())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_for_db_row(
    test_db: Session,
    model: type,
    *,
    deadline_seconds: float = PERSIST_DEADLINE_SECONDS,
    **filters: Any,
) -> list:
    """Poll ``test_db`` for ``model`` rows matching ``filters``.

    The persistence worker writes from a background thread, so the
    test thread can issue the query before the row is visible. The
    SQLAlchemy session also caches rows in its identity map, so
    ``expire_all()`` is called on every iteration to force a
    re-fetch. Returns the list of matching rows once at least one
    matches, or ``[]`` if the deadline passes.
    """

    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        test_db.expire_all()
        rows = test_db.query(model).filter_by(**filters).all()
        if rows:
            return rows
        time.sleep(PERSIST_POLL_INTERVAL_SECONDS)
    test_db.expire_all()
    return test_db.query(model).filter_by(**filters).all()


def _drive_async(coro: Any) -> Any:
    """Run a coroutine to completion on a private event loop.

    The smoke test is synchronous (the test body has no ``async``
    keywords, and ``TestClient`` runs the WS handler in a worker
    thread). The launch state store's ``register`` is async, so
    we drive the coroutine via a one-shot loop and discard the
    loop afterwards. The state store's internal locks are released
    before this returns, so the route's ``await consume`` call sees
    the registered entry.

    ``asyncio.run`` cannot be used here in case pytest-asyncio or
    the ``TestClient`` has already installed a running loop on this
    thread (it has not, on this code path, but the explicit
    ``new_event_loop``/``close`` shape is defensive).
    """
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _envelope(
    msg_type: str,
    payload: dict[str, Any],
    *,
    session_id: str,
    captured_at: str | None = None,
) -> dict[str, Any]:
    """Build a raw client envelope for ``websocket.send_json``."""

    return {
        "type": msg_type,
        "session_id": session_id,
        "captured_at": captured_at or datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }


def _evidence_jpeg_bytes() -> bytes:
    """32 bytes that look enough like a JPEG to satisfy the
    SHA-256 checksum path.  The real frame bytes are irrelevant
    to the smoke test — we only care that the seal round-trip
    writes the blob + the row.

    The first three bytes are the JPEG magic prefix and the
    remaining bytes pad the size to 32 — they are not a valid
    JPEG, but the evidence service does not validate the image
    format, only the checksum.
    """

    return b"\xff\xd8\xff" + b"\x00" * 29


# ---------------------------------------------------------------------------
# The smoke test
# ---------------------------------------------------------------------------


def test_e2e_second_person_triggers_termination(
    e2e_client: TestClient,
    httpx_mock,
    lti_settings: LtiSettings,
    active_policy: PolicyConfig,
    state_store: InMemoryLaunchStateStore,
    test_db: Session,
    evidence_store: InMemoryEvidenceStore,
) -> None:
    """The full v1 pipeline: LTI launch → WS connection → three
    telemetry_light frames → CRITICAL flag → kill-switch → evidence
    seal → termination record.

    Eight phases; each phase asserts what the previous phase
    established. Uses the polling helper above to wait for the
    async persistence worker to commit rows.
    """

    # ----- Phase 1: Fake LTI launch ---------------------------------

    oidc_setup = make_test_oidc_setup(issuer="https://lms.example.edu", kid="key-1")
    register_oidc_responses(httpx_mock, oidc_setup)

    state = InMemoryLaunchStateStore.new_state()
    nonce = InMemoryLaunchStateStore.new_nonce()
    # ``InMemoryLaunchStateStore.register`` is async; the test is
    # sync, so we drive the coroutine to completion via a private
    # event loop. The state store's locks are released before this
    # loop closes, so the launch route's ``await consume`` call sees
    # the registered entry.
    _drive_async(
        state_store.register(
            state, nonce,
            redirect_uri=lti_settings.launch_url,
            lti_issuer=oidc_setup.issuer,
        )
    )

    payload = build_signed_launch_claims(
        issuer=oidc_setup.issuer,
        audience=lti_settings.tool_client_id,
        target_link_uri=lti_settings.launch_url,
        kid=oidc_setup.kid,
        nonce=nonce,
        state=state,
        policy_config_name="cs101-default",
        role_uri=LEARNER_URI,
        subject="student-42",
        name="Smoke Test Student",
    )
    id_token = oidc_setup.sign_launch(payload)

    launch_response = e2e_client.post(
        "/lti/launch",
        data={"id_token": id_token},
        follow_redirects=False,
    )
    assert launch_response.status_code == 302, (
        f"Expected a 302 redirect to the exam client, got "
        f"{launch_response.status_code}: {launch_response.text}"
    )
    location = launch_response.headers["location"]
    parsed = urlparse(location)
    assert parsed.query == "", (
        "Session token must travel in the URL fragment, not the query "
        "string (proxy access-log invariant)."
    )
    fragment_params = dict(p.split("=", 1) for p in parsed.fragment.split("&"))
    session_token = fragment_params["session_token"]
    session_id = fragment_params["session_id"]

    decoded = decode_session_token(session_token, settings=lti_settings)
    assert decoded.role == AppRole.LEARNER
    assert decoded.session_id == session_id

    # The launch committed Participant + ExamSession rows.
    test_db.expire_all()
    participants = test_db.query(Participant).all()
    assert len(participants) == 1
    assert str(participants[0].id) == decoded.subject
    sessions = test_db.query(ExamSession).all()
    assert len(sessions) == 1
    exam_session = sessions[0]
    assert str(exam_session.id) == session_id
    assert exam_session.status == SessionStatus.PENDING
    assert exam_session.consent_recorded_at is not None

    # ----- Phase 2: Open WebSocket -----------------------------------

    subprotocol = f"proctoring-v1.{session_token}"
    with e2e_client.websocket_connect("/ws", subprotocols=[subprotocol]) as ws:
        # First message is a pong — the handler ignores pongs, so
        # this is a no-op round-trip that confirms the connection
        # is alive before we start streaming telemetry.
        ws.send_json({"type": "pong"})

        # Wait for the PENDING → ACTIVE transition (the handler
        # commits in a worker thread, so the SQLite identity map
        # may need a beat).
        act_rows = _wait_for_db_row(
            test_db, ExamSession,
            id=exam_session.id, status=SessionStatus.ACTIVE,
        )
        assert act_rows, "PENDING → ACTIVE transition did not commit in time"

        # ----- Phase 3: Stream frames that trigger second-person ----

        for seq in range(3):
            ws.send_json(_envelope(
                "telemetry_light",
                {
                    "modality": "face_presence",
                    "face_count": 2,
                    "confidence": 0.95,
                    "bbox": [0.1, 0.2, 0.3, 0.4],
                },
                session_id=session_id,
            ))
            ack = ws.receive_json()
            assert ack["type"] == "ack", (
                f"Expected ack for light frame {seq}, got {ack}"
            )
            assert ack["payload"]["seq"] == seq

        # ----- Phase 4: Flag persisted with structural proof ---------

        flag_rows = _wait_for_db_row(
            test_db, Flag,
            exam_session_id=exam_session.id,
        )
        assert flag_rows, "Flag was not persisted within the deadline"
        flag = flag_rows[0]
        assert flag.rule_code == "second_person"
        assert flag.severity == FlagSeverity.CRITICAL
        assert flag.triggered_termination is True
        assert 0.0 <= flag.confidence_lower <= flag.confidence_score
        assert flag.confidence_score <= flag.confidence_upper <= 1.0
        assert flag.policy_config_id == active_policy.id

        # Three TelemetryEvent rows (one per light frame).
        telemetry_rows = _wait_for_db_row(
            test_db, TelemetryEvent,
            exam_session_id=exam_session.id,
        )
        assert len(telemetry_rows) == 3
        for row in telemetry_rows:
            assert row.event_type == "second_person"
            assert row.occurred_at is not None

        # Three FlagTelemetryEvent links, one per contributing event.
        test_db.expire_all()
        reload_flag = test_db.get(Flag, flag.id)
        assert reload_flag is not None
        assert len(reload_flag.telemetry_links) == 3

        # ----- Phase 5: Kill-switch delivered ------------------------

        # The kill-switch is sent on the WS after the callback fires;
        # use a polling receive_json to skip any interleaved acks.
        deadline = time.monotonic() + PERSIST_DEADLINE_SECONDS
        killswitch = None
        while time.monotonic() < deadline:
            msg = ws.receive_json()
            if msg.get("type") == "kill_switch":
                killswitch = msg
                break
        assert killswitch is not None, (
            "Kill-switch was not delivered within the deadline"
        )
        assert killswitch["payload"]["reason"] == "second_person"
        assert killswitch["payload"]["flag_id"] == str(flag.id)

        # ----- Phase 6: Kill-switch ack round-trips ------------------

        ws.send_json(_envelope(
            "kill_switch_ack",
            {"flag_id": str(flag.id), "answered_questions": 42},
            session_id=session_id,
        ))
        ack_response = ws.receive_json()
        assert ack_response["type"] == "ack", (
            f"Expected ack for kill-switch ack, got {ack_response}"
        )

        # ----- Phase 7: Evidence seal via the orchestration route ----

        blob = _evidence_jpeg_bytes()
        seal_response = e2e_client.post(
            f"/sessions/{session_id}/flags/{flag.id}/evidence",
            params={
                "artifact_type": "frame",
                "media_type": "image/jpeg",
                "capture_started_at": datetime.now(timezone.utc).isoformat(),
            },
            files={"blob": ("frame.jpg", blob, "image/jpeg")},
        )
        assert seal_response.status_code == 201, (
            f"Expected 201 Created from the seal route, got "
            f"{seal_response.status_code}: {seal_response.text}"
        )
        seal_body = seal_response.json()
        assert seal_body["flag_id"] == str(flag.id)
        assert seal_body["byte_size"] == len(blob)
        assert seal_body["content_sha256"]  # 64 hex chars

        # The blob is in the InMemoryEvidenceStore.
        storage_key = seal_body["storage_uri"].removeprefix("s3://")
        assert evidence_store.exists(storage_key)

        # The EvidenceArtifact row is committed.
        artifact_rows = _wait_for_db_row(
            test_db, EvidenceArtifact,
            flag_id=flag.id,
        )
        assert len(artifact_rows) == 1
        artifact = artifact_rows[0]
        assert artifact.byte_size == len(blob)
        assert artifact.content_sha256 == seal_body["content_sha256"]

        # ----- Phase 8: TerminationRecord exists ---------------------

        termination_rows = _wait_for_db_row(
            test_db, TerminationRecord,
            exam_session_id=exam_session.id,
        )
        assert len(termination_rows) == 1
        termination = termination_rows[0]
        assert termination.triggering_flag_id == flag.id
        assert termination.reason == "second_person"

    # The ``with`` context closes the WS, which triggers the route's
    # ``finally`` block: cancel the kill-switch drain task, stop the
    # persistence worker, stop the dispatcher, cancel the heartbeat.
    # Give the worker a beat to commit any in-flight work.
    time.sleep(0.2)
