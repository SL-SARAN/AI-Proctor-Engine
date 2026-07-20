"""Unit tests for the HS256 session-token issuer and decoder.

Boundary cases (per ``docs/08-test-strategy-design.md``): wrong
signature, wrong issuer, wrong audience, missing claim, and
expired token.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from proctoring_engine.lti.config import LtiSettings, set_lti_settings
from proctoring_engine.lti.roles import AppRole
from proctoring_engine.lti.session_token import (
    SessionTokenExpired,
    SessionTokenInvalid,
    decode_session_token,
    issue_session_token,
)


@pytest.fixture()
def settings() -> LtiSettings:
    """A unique LTI settings instance per test (no shared state)."""

    s = LtiSettings(
        tool_client_id="tool",
        launch_url="http://localhost:8000/lti/launch",
        session_token_secret=secrets.token_urlsafe(32),
        session_token_ttl_seconds=60,
    )
    set_lti_settings(s)
    return s


def test_round_trip_returns_matching_claims(settings: LtiSettings) -> None:
    """A fresh token decodes to the inputs that produced it."""

    participant = uuid.uuid4()
    exam_session = uuid.uuid4()
    token = issue_session_token(
        participant,
        exam_session,
        AppRole.LEARNER,
        settings=settings,
    )
    claims = decode_session_token(token, settings=settings)
    assert claims.subject == str(participant)
    assert claims.session_id == str(exam_session)
    assert claims.role == AppRole.LEARNER
    assert claims.issuer == settings.session_token_issuer
    assert claims.audience == settings.session_token_audience
    assert claims.jti  # non-empty


def test_expired_token_raises_expired(settings: LtiSettings) -> None:
    """A token issued in the past is rejected with the
    expired-specific exception.
    """

    participant = uuid.uuid4()
    exam_session = uuid.uuid4()
    issued = datetime(2024, 1, 1, tzinfo=timezone.utc)
    token = issue_session_token(
        participant,
        exam_session,
        AppRole.LEARNER,
        settings=settings,
        now=issued,
    )
    with pytest.raises(SessionTokenExpired):
        decode_session_token(token, settings=settings)


def test_token_signed_with_wrong_secret_raises_invalid(
    settings: LtiSettings,
) -> None:
    """A token signed by a different secret is rejected."""

    other = LtiSettings(
        tool_client_id="tool",
        launch_url="http://localhost:8000/lti/launch",
        session_token_secret=secrets.token_urlsafe(32),
    )
    token = issue_session_token(
        uuid.uuid4(),
        uuid.uuid4(),
        AppRole.LEARNER,
        settings=other,
    )
    with pytest.raises(SessionTokenInvalid):
        decode_session_token(token, settings=settings)


def test_token_with_wrong_audience_raises_invalid(settings: LtiSettings) -> None:
    """A token whose ``aud`` claim does not match the setting is rejected."""

    payload = {
        "sub": str(uuid.uuid4()),
        "sid": str(uuid.uuid4()),
        "role": AppRole.LEARNER.value,
        "iss": settings.session_token_issuer,
        "aud": "some-other-audience",
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": int(
            (datetime.now(timezone.utc) + timedelta(seconds=60)).timestamp()
        ),
        "jti": str(uuid.uuid4()),
    }
    token = jwt.encode(payload, settings.session_token_secret, algorithm="HS256")
    with pytest.raises(SessionTokenInvalid):
        decode_session_token(token, settings=settings)


def test_token_with_wrong_issuer_raises_invalid(settings: LtiSettings) -> None:
    """A token whose ``iss`` claim does not match the setting is rejected."""

    payload = {
        "sub": str(uuid.uuid4()),
        "sid": str(uuid.uuid4()),
        "role": AppRole.LEARNER.value,
        "iss": "some-other-issuer",
        "aud": settings.session_token_audience,
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": int(
            (datetime.now(timezone.utc) + timedelta(seconds=60)).timestamp()
        ),
        "jti": str(uuid.uuid4()),
    }
    token = jwt.encode(payload, settings.session_token_secret, algorithm="HS256")
    with pytest.raises(SessionTokenInvalid):
        decode_session_token(token, settings=settings)


def test_token_missing_required_claim_raises_invalid(
    settings: LtiSettings,
) -> None:
    """A token missing ``role`` (one of the application-level claims)
    is rejected at the ``require`` option layer.
    """

    payload = {
        "sub": str(uuid.uuid4()),
        "sid": str(uuid.uuid4()),
        "iss": settings.session_token_issuer,
        "aud": settings.session_token_audience,
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": int(
            (datetime.now(timezone.utc) + timedelta(seconds=60)).timestamp()
        ),
        "jti": str(uuid.uuid4()),
    }
    token = jwt.encode(payload, settings.session_token_secret, algorithm="HS256")
    with pytest.raises(SessionTokenInvalid):
        decode_session_token(token, settings=settings)


def test_token_with_unknown_role_value_raises_invalid(
    settings: LtiSettings,
) -> None:
    """A token whose ``role`` claim is not a known ``AppRole`` is
    rejected at the typed-view layer.
    """

    payload = {
        "sub": str(uuid.uuid4()),
        "sid": str(uuid.uuid4()),
        "role": "shapeshifter",
        "iss": settings.session_token_issuer,
        "aud": settings.session_token_audience,
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": int(
            (datetime.now(timezone.utc) + timedelta(seconds=60)).timestamp()
        ),
        "jti": str(uuid.uuid4()),
    }
    token = jwt.encode(payload, settings.session_token_secret, algorithm="HS256")
    with pytest.raises(SessionTokenInvalid):
        decode_session_token(token, settings=settings)


def test_garbage_token_raises_invalid(settings: LtiSettings) -> None:
    """A non-JWT string is rejected."""

    with pytest.raises(SessionTokenInvalid):
        decode_session_token("not-a-jwt", settings=settings)
