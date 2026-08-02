"""Unit tests for the ``/lti/login`` and ``/lti/launch`` FastAPI routes.

Each test uses the ``httpx_mock`` fixture from ``pytest-httpx``
to mock the OIDC discovery + JWKS endpoints and constructs the
launch flow's own ``LtiSettings`` / state store / DB session via
a per-test app factory. The service is exercised through the
real DB layer (a SQLite in-memory engine), not mocked, so the
routes and the service are tested together as a single contract.

The route tests use a ``StaticPool`` so all sessions share the
same in-memory SQLite connection. The default ``QueuePool``
gives each new connection a fresh database, which breaks
cross-thread access when FastAPI runs the route handler in a
worker thread.
"""

from __future__ import annotations

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from proctoring_engine.lti import (
    InMemoryLaunchStateStore,
    JwksCache,
    LtiSettings,
    OidcDiscoveryCache,
    build_lti_router,
    decode_session_token,
)
from proctoring_engine.lti.roles import AppRole
from proctoring_engine.lti.routes import _RouterDeps
from proctoring_engine.models import Base, PolicyConfig
from .integration.oidc_test_double import (
    INSTRUCTOR_URI,
    build_signed_launch_claims,
    make_test_oidc_setup,
    register_oidc_responses,
)


# --- fixtures ---------------------------------------------------------


@pytest.fixture()
def settings() -> LtiSettings:
    return LtiSettings(
        tool_client_id="proctoring-engine",
        launch_url="http://localhost:8000/lti/launch",
        session_token_secret="x" * 32,
        oidc_http_timeout_seconds=1.0,
    )


@pytest.fixture()
def oidc_setup():
    return make_test_oidc_setup(issuer="https://lms.example.edu", kid="key-1")


@pytest.fixture()
def active_policy(test_db) -> PolicyConfig:
    policy = PolicyConfig(name="cs101-default", is_active=True)
    test_db.add(policy)
    test_db.commit()
    return policy


@pytest.fixture()
def test_db():
    """A SQLite in-memory engine with a ``StaticPool`` so all
    sessions across threads share the same connection.

    FastAPI runs the route handler in an anyio worker thread;
    with a default pool, each new connection from the pool
    gets a fresh in-memory database, so the route's
    ``process_launch`` query runs against an empty schema.
    ``StaticPool`` keeps one connection for the engine's
    lifetime, which is the right shape for a test-only
    in-memory database.
    """

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
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
def state_store() -> InMemoryLaunchStateStore:
    return InMemoryLaunchStateStore(ttl_seconds=600)


@pytest.fixture()
def jwks_cache() -> JwksCache:
    return JwksCache()


@pytest.fixture()
def discovery_cache() -> OidcDiscoveryCache:
    return OidcDiscoveryCache()


@pytest.fixture()
def app_factory(
    settings,
    state_store,
    jwks_cache,
    discovery_cache,
    test_db,
):
    """A factory that builds a fresh FastAPI app per test with
    the LTI router wired against the per-test dependencies.
    """

    def _build_app() -> FastAPI:
        # ``pytest-httpx`` patches the global async transport
        # so any ``httpx.AsyncClient()`` constructed inside
        # the route is intercepted. We use the default
        # factory so the routes use the patched transport.
        def _http_client_factory():
            import httpx
            return httpx.AsyncClient(timeout=1.0)

        def _get_db() -> Session:
            return test_db

        deps = _RouterDeps(
            settings=settings,
            state_store=state_store,
            jwks_cache=jwks_cache,
            discovery_cache=discovery_cache,
            http_client_factory=_http_client_factory,
            get_db=_get_db,
        )
        app = FastAPI()
        app.include_router(build_lti_router(deps))
        return app

    return _build_app


@pytest.fixture()
def client(app_factory) -> TestClient:
    return TestClient(app_factory())


def _register_full_oidc(httpx_mock, oidc_setup, *, optional: bool = False) -> None:
    register_oidc_responses(httpx_mock, oidc_setup, optional=optional)


def _sign_launch(oidc_setup, settings, *, policy_config_name="cs101-default", **overrides) -> str:
    """Build and sign a valid launch JWT for the test setup."""

    payload = build_signed_launch_claims(
        issuer=oidc_setup.issuer,
        audience=settings.tool_client_id,
        target_link_uri=settings.launch_url,
        kid=oidc_setup.kid,
        policy_config_name=policy_config_name,
        **overrides,
    )
    return oidc_setup.sign_launch(payload)


# --- /lti/login tests -------------------------------------------------


async def test_login_happy_path_redirects_to_authorization_endpoint(
    client, httpx_mock, oidc_setup
) -> None:
    """A valid ``/lti/login`` request 302s to the platform's
    authorization endpoint with the right query string.
    """

    httpx_mock.add_response(
        method="GET", url=oidc_setup.discovery_url, json=oidc_setup.discovery_payload
    )

    response = client.get(        "/lti/login",
        params={
            "iss": oidc_setup.issuer,
            "login_hint": "user-1",
            "target_link_uri": "http://localhost:8000/lti/launch",
            "lti_message_hint": "course-101:exam-midterm",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(oidc_setup.authorization_endpoint)
    assert "response_type=id_token" in location
    assert "response_mode=form_post" in location
    assert "prompt=none" in location
    assert "client_id=proctoring-engine" in location
    assert "redirect_uri=" in location
    assert "login_hint=user-1" in location


async def test_login_missing_iss_returns_400(client) -> None:
    response = client.get(
        "/lti/login",
        params={
            "login_hint": "user-1",
            "target_link_uri": "http://localhost:8000/lti/launch",
            "lti_message_hint": "course-101:exam-midterm",
        },
        follow_redirects=False,
    )
    # FastAPI returns 422 (unprocessable entity) for missing
    # required query params; we accept either 400 or 422.
    assert response.status_code in (400, 422)


async def test_login_missing_login_hint_returns_400(client) -> None:
    response = client.get(
        "/lti/login",
        params={
            "iss": "https://lms.example.edu",
            "target_link_uri": "http://localhost:8000/lti/launch",
            "lti_message_hint": "course-101:exam-midterm",
        },
        follow_redirects=False,
    )
    assert response.status_code in (400, 422)


async def test_login_discovery_failure_returns_502(client, httpx_mock, oidc_setup) -> None:
    """An OIDC discovery failure surfaces as 502."""

    httpx_mock.add_exception(httpx.ConnectError("connection refused"))

    response = client.get(        "/lti/login",
        params={
            "iss": oidc_setup.issuer,
            "login_hint": "user-1",
            "target_link_uri": "http://localhost:8000/lti/launch",
            "lti_message_hint": "course-101:exam-midterm",
        },
        follow_redirects=False,
    )
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "discovery_error"


async def test_login_target_link_uri_mismatch_returns_400(
    client, httpx_mock, oidc_setup
) -> None:
    """A ``target_link_uri`` that doesn't match the registered
    launch URL is rejected.
    """

    httpx_mock.add_response(
        method="GET",
        url=oidc_setup.discovery_url,
        json=oidc_setup.discovery_payload,
        is_optional=True,
    )

    response = client.get(
        "/lti/login",
        params={
            "iss": oidc_setup.issuer,
            "login_hint": "user-1",
            "target_link_uri": "http://attacker.example/lti/launch",
            "lti_message_hint": "course-101:exam-midterm",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400


# --- /lti/launch tests ------------------------------------------------


async def test_launch_happy_path_learner(
    client, httpx_mock, oidc_setup, settings, active_policy, state_store, test_db
) -> None:
    """A valid learner launch 302s to the exam client with a
    working session token; the rows are persisted.
    """

    _register_full_oidc(httpx_mock, oidc_setup)

    state = InMemoryLaunchStateStore.new_state()
    nonce = InMemoryLaunchStateStore.new_nonce()
    await state_store.register(
        state, nonce,
        redirect_uri=settings.launch_url,
        lti_issuer=oidc_setup.issuer,
    )
    id_token = _sign_launch(oidc_setup, settings, state=state, nonce=nonce)

    response = client.post("/lti/launch", data={"id_token": id_token}, follow_redirects=False)

    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(settings.exam_client_url)
    assert "session_token=" in location
    assert "session_id=" in location

    # The session token travels in the URL fragment, not the query
    # string. Fragments are not transmitted in the HTTP request and
    # so are never logged by reverse proxies or leaked via Referer.
    from urllib.parse import urlparse

    parsed = urlparse(location)
    assert parsed.query == ""
    fragment = parsed.fragment
    fragment_params = dict(p.split("=", 1) for p in fragment.split("&"))
    token = fragment_params["session_token"]
    decoded = decode_session_token(token, settings=settings)
    assert decoded.role == AppRole.LEARNER

    # The Participant and ExamSession rows are persisted.
    from sqlalchemy import select
    from proctoring_engine.models import ExamSession, Participant
    test_db.commit()
    participants = test_db.execute(select(Participant)).scalars().all()
    assert len(participants) == 1
    sessions = test_db.execute(select(ExamSession)).scalars().all()
    assert len(sessions) == 1
    assert sessions[0].consent_recorded_at is not None


async def test_launch_happy_path_instructor_creates_admin(
    client, httpx_mock, oidc_setup, settings, active_policy, state_store, test_db
) -> None:
    """An instructor launch 302s to the admin surface; an
    ``AdminUser`` row exists.
    """

    _register_full_oidc(httpx_mock, oidc_setup)

    state = InMemoryLaunchStateStore.new_state()
    nonce = InMemoryLaunchStateStore.new_nonce()
    await state_store.register(
        state, nonce,
        redirect_uri=settings.launch_url,
        lti_issuer=oidc_setup.issuer,
    )
    id_token = _sign_launch(
        oidc_setup, settings, state=state, nonce=nonce, role_uri=INSTRUCTOR_URI
    )

    response = client.post("/lti/launch", data={"id_token": id_token}, follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"].startswith(settings.admin_surface_url)

    from sqlalchemy import select
    from proctoring_engine.models import AdminUser
    test_db.commit()
    admins = test_db.execute(select(AdminUser)).scalars().all()
    assert len(admins) == 1
    assert admins[0].role.value == "instructor"


async def test_launch_replay_returns_state_unknown(
    client, httpx_mock, oidc_setup, settings, active_policy, state_store
) -> None:
    """The same ``state`` consumed twice → 400 ``state_unknown``."""

    _register_full_oidc(httpx_mock, oidc_setup)

    state = InMemoryLaunchStateStore.new_state()
    nonce = InMemoryLaunchStateStore.new_nonce()
    await state_store.register(
        state, nonce,
        redirect_uri=settings.launch_url,
        lti_issuer=oidc_setup.issuer,
    )
    id_token = _sign_launch(oidc_setup, settings, state=state, nonce=nonce)

    first = client.post("/lti/launch", data={"id_token": id_token}, follow_redirects=False)
    assert first.status_code == 302

    second = client.post("/lti/launch", data={"id_token": id_token}, follow_redirects=False)
    assert second.status_code == 400
    assert second.json()["detail"]["code"] == "state_unknown"


async def test_launch_expired_returns_claims_invalid(
    client, httpx_mock, oidc_setup, settings, active_policy, state_store
) -> None:
    """An expired ``exp`` → 400 ``claims_invalid``."""

    _register_full_oidc(httpx_mock, oidc_setup)

    state = InMemoryLaunchStateStore.new_state()
    nonce = InMemoryLaunchStateStore.new_nonce()
    await state_store.register(
        state, nonce,
        redirect_uri=settings.launch_url,
        lti_issuer=oidc_setup.issuer,
    )
    id_token = _sign_launch(
        oidc_setup, settings, state=state, nonce=nonce, exp_offset_seconds=-3600
    )

    response = client.post("/lti/launch", data={"id_token": id_token}, follow_redirects=False)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "claims_invalid"


async def test_launch_signature_from_unknown_key_returns_signature_invalid(
    client, httpx_mock, oidc_setup, settings, active_policy, state_store
) -> None:
    """A JWT signed by a key not in the JWKS → 400
    ``signature_invalid``.
    """

    _register_full_oidc(httpx_mock, oidc_setup)

    state = InMemoryLaunchStateStore.new_state()
    nonce = InMemoryLaunchStateStore.new_nonce()
    await state_store.register(
        state, nonce,
        redirect_uri=settings.launch_url,
        lti_issuer=oidc_setup.issuer,
    )

    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    payload = build_signed_launch_claims(
        issuer=oidc_setup.issuer,
        audience=settings.tool_client_id,
        target_link_uri=settings.launch_url,
        kid="attacker-key",
        nonce=nonce,
        state=state,
        policy_config_name="cs101-default",
    )
    id_token = jwt.encode(
        payload, attacker_key, algorithm="RS256", headers={"kid": "attacker-key"}
    )

    response = client.post("/lti/launch", data={"id_token": id_token}, follow_redirects=False)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "signature_invalid"


async def test_launch_wrong_iss_returns_issuer_invalid(
    client, httpx_mock, oidc_setup, settings, active_policy, state_store
) -> None:
    """A launch with ``iss`` that does not match a known
    discovery document → 400 ``issuer_invalid``.
    """

    _register_full_oidc(httpx_mock, oidc_setup, optional=True)

    state = InMemoryLaunchStateStore.new_state()
    nonce = InMemoryLaunchStateStore.new_nonce()
    await state_store.register(
        state, nonce,
        redirect_uri=settings.launch_url,
        lti_issuer=oidc_setup.issuer,
    )
    id_token = _sign_launch(
        oidc_setup, settings, state=state, nonce=nonce,
        iss_override="https://attacker.example.edu",
    )

    response = client.post("/lti/launch", data={"id_token": id_token}, follow_redirects=False)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "issuer_invalid"


async def test_launch_wrong_nonce_returns_nonce_mismatch(
    client, httpx_mock, oidc_setup, settings, active_policy, state_store
) -> None:
    """A launch with a ``nonce`` that doesn't match the
    registered state → 400 ``nonce_mismatch``.
    """

    _register_full_oidc(httpx_mock, oidc_setup)

    state = InMemoryLaunchStateStore.new_state()
    registered_nonce = InMemoryLaunchStateStore.new_nonce()
    await state_store.register(
        state, registered_nonce,
        redirect_uri=settings.launch_url,
        lti_issuer=oidc_setup.issuer,
    )
    id_token = _sign_launch(
        oidc_setup, settings, state=state,
        nonce=InMemoryLaunchStateStore.new_nonce(),
    )

    response = client.post("/lti/launch", data={"id_token": id_token}, follow_redirects=False)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "nonce_mismatch"


async def test_launch_missing_policy_returns_policy_not_found(
    client, httpx_mock, oidc_setup, settings, state_store
) -> None:
    """A launch with no active policy matching the
    ``custom.policy_config_name`` claim → 400 ``policy_not_found``.
    """

    _register_full_oidc(httpx_mock, oidc_setup)

    state = InMemoryLaunchStateStore.new_state()
    nonce = InMemoryLaunchStateStore.new_nonce()
    await state_store.register(
        state, nonce,
        redirect_uri=settings.launch_url,
        lti_issuer=oidc_setup.issuer,
    )
    id_token = _sign_launch(
        oidc_setup, settings, state=state, nonce=nonce,
        policy_config_name="never-defined",
    )

    response = client.post("/lti/launch", data={"id_token": id_token}, follow_redirects=False)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "policy_not_found"


async def test_launch_wrong_aud_returns_audience_invalid(
    client, httpx_mock, oidc_setup, settings, active_policy, state_store
) -> None:
    """A launch with ``aud`` that doesn't match the
    tool_client_id → 400 ``audience_invalid``.
    """

    _register_full_oidc(httpx_mock, oidc_setup)

    state = InMemoryLaunchStateStore.new_state()
    nonce = InMemoryLaunchStateStore.new_nonce()
    await state_store.register(
        state, nonce,
        redirect_uri=settings.launch_url,
        lti_issuer=oidc_setup.issuer,
    )
    id_token = _sign_launch(
        oidc_setup, settings, state=state, nonce=nonce,
        aud_override="https://attacker.example/tool",
    )

    response = client.post("/lti/launch", data={"id_token": id_token}, follow_redirects=False)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "audience_invalid"


async def test_launch_unknown_role_uri_returns_claims_invalid(
    client, httpx_mock, oidc_setup, settings, active_policy, state_store
) -> None:
    """A launch with a role URI the role mapper doesn't
    recognize → 400 ``claims_invalid``.
    """

    _register_full_oidc(httpx_mock, oidc_setup)

    state = InMemoryLaunchStateStore.new_state()
    nonce = InMemoryLaunchStateStore.new_nonce()
    await state_store.register(
        state, nonce,
        redirect_uri=settings.launch_url,
        lti_issuer=oidc_setup.issuer,
    )
    id_token = _sign_launch(
        oidc_setup, settings, state=state, nonce=nonce,
        roles=["http://purl.imsglobal.org/vocab/lis/v2/membership#Bogus"],
    )

    response = client.post("/lti/launch", data={"id_token": id_token}, follow_redirects=False)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "claims_invalid"
