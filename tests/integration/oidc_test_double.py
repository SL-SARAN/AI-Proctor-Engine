"""Test double for an LTI 1.3 platform's OIDC discovery + JWKS endpoints.

The unit and integration tests both need a small OIDC server they
can point the launch flow at. The shape is identical to a real
platform's surface (discovery document + JWKS endpoint, signed
JWTs against the platform's published key) so the launch flow
exercises the real ``httpx`` / ``pyjwt`` code path. The difference
is the transport: ``pytest-httpx`` mocks the HTTP calls so the
tests don't need a real socket or a real LMS.

The double is in ``tests/integration/`` because it depends on
``pytest-httpx`` (a dev dep). The unit tests import it from
there — the import-from-integration pattern is short and the
helper itself is small, so a separate ``tests/_helpers/`` tree
is overkill for a single module.

Exports:

* :class:`TestOidcSetup` — a dataclass with the generated keypair,
  the ``sign_launch`` callable, and the discovery + JWKS payloads.
* :func:`make_test_oidc_setup` — generates a fresh setup.
* :func:`register_oidc_responses` — registers the discovery + JWKS
  responses with a ``pytest-httpx`` mock.
* :func:`build_signed_launch_claims` — builds the JWT claim
  payload for a test launch, with sensible defaults.
* The role URI constants (:data:`LEARNER_URI`, etc.) — the
  canonical LTI 1.3 role URIs the helper uses by default.
"""

from __future__ import annotations

import base64
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


# --- Role URI constants -----------------------------------------------

#: The LTI 1.3 Learner role URI (context-namespace).
LEARNER_URI = "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"
#: The LTI 1.3 Instructor role URI (context-namespace).
INSTRUCTOR_URI = "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"
#: The LTI 1.3 Administrator role URI (membership-namespace).
ADMIN_URI = "http://purl.imsglobal.org/vocab/lis/v2/membership#Administrator"
#: The 1EdTech Proctor role extension URI.
PROCTOR_URI = "http://purl.imsglobal.org/vocab/lti/role/proctor#Proctor"


# --- Setup dataclass --------------------------------------------------


@dataclass(frozen=True, slots=True)
class TestOidcSetup:
    """The state of one OIDC test double.

    The ``kid`` is the JWK's key id; it is also what the launch
    flow expects in the JWT header. ``sign_launch`` returns a
    valid RS256-signed JWT against the generated private key;
    the launch flow verifies it against the public key in
    ``jwks_payload``.
    """

    issuer: str
    kid: str
    authorization_endpoint: str
    jwks_uri: str
    discovery_url: str
    discovery_payload: dict[str, Any] = field(default_factory=dict)
    jwks_payload: dict[str, Any] = field(default_factory=dict)
    sign_launch: Callable[[dict[str, Any]], str] = field(default=lambda payload: "")
    _private_key: Optional[rsa.RSAPrivateKey] = field(default=None, repr=False)


# --- Setup factory ----------------------------------------------------


def make_test_oidc_setup(
    *,
    issuer: str = "https://lms.example.edu",
    kid: str = "key-1",
    authorization_endpoint: Optional[str] = None,
    jwks_uri: Optional[str] = None,
) -> TestOidcSetup:
    """Generate a fresh RSA keypair and the OIDC payloads that
    describe it.

    The default ``issuer`` is a non-routable example domain so
    the unit tests do not accidentally hit a real LMS. Tests
    that need a different issuer (e.g. to exercise the
    ``issuer_invalid`` path) pass it explicitly.
    """

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()

    authz_endpoint = authorization_endpoint or f"{issuer}/authorize"
    jwks_endpoint = jwks_uri or f"{issuer}/jwks"
    discovery_endpoint = f"{issuer}/.well-known/openid-configuration"

    discovery_payload = {
        "issuer": issuer,
        "authorization_endpoint": authz_endpoint,
        "jwks_uri": jwks_endpoint,
        "token_endpoint": f"{issuer}/token",
        "id_token_signing_alg_values_supported": ["RS256"],
        "response_types_supported": ["id_token"],
        "subject_types_supported": ["public"],
        "scopes_supported": ["openid"],
    }
    jwks_payload = {
        "keys": [_jwk_from_public_key(public_key, kid)],
    }

    def sign_launch(payload: dict[str, Any]) -> str:
        """Sign the given payload as a valid LTI 1.3 launch JWT.

        The header carries the ``kid`` the launch flow expects
        in ``unverified_header``; the body is the payload
        verbatim, with ``iat`` and ``exp`` set if not already
        present.
        """

        now = int(time.time())
        body = dict(payload)
        body.setdefault("iat", now)
        body.setdefault("exp", now + 300)
        return jwt.encode(body, private_key, algorithm="RS256", headers={"kid": kid})

    return TestOidcSetup(
        issuer=issuer,
        kid=kid,
        authorization_endpoint=authz_endpoint,
        jwks_uri=jwks_endpoint,
        discovery_url=discovery_endpoint,
        discovery_payload=discovery_payload,
        jwks_payload=jwks_payload,
        sign_launch=sign_launch,
        _private_key=private_key,
    )


def register_oidc_responses(httpx_mock, setup: TestOidcSetup, *, optional: bool = False) -> None:
    """Register the discovery + JWKS responses with ``pytest-httpx``.

    Both endpoints are matched by URL. The launch flow fetches
    discovery first (to learn the JWKS URL) and the JWKS
    second (to verify the signature), so the order in which
    they're added matters.

    The JWKS response is registered with ``is_reusable=True``
    because :class:`JwksCache.get_key` may issue a second fetch
    when the first response does not contain the requested
    ``kid`` (key-rotation case).

    When ``optional=True``, the responses are registered with
    ``is_optional=True`` so the test client can reject a launch
    (e.g. cross-issuer state-reuse) before the OIDC fetch happens
    without tripping the ``assert_all_responses_were_requested``
    guard. Use ``optional=True`` for the negative tests that
    reject before the discovery fetch.
    """

    httpx_mock.add_response(
        method="GET", url=setup.discovery_url,
        json=setup.discovery_payload, is_optional=optional,
    )
    httpx_mock.add_response(
        method="GET", url=setup.jwks_uri,
        json=setup.jwks_payload, is_reusable=True, is_optional=optional,
    )


# --- Claim builder ---------------------------------------------------


def build_signed_launch_claims(
    *,
    issuer: str,
    audience: str,
    target_link_uri: str,
    kid: str,
    nonce: str,
    state: str,
    policy_config_name: str,
    role_uri: str = LEARNER_URI,
    subject: str = "user-1",
    name: Optional[str] = "Test User",
    iss_override: Optional[str] = None,
    aud_override: Optional[str] = None,
    exp_offset_seconds: int = 300,
    iat_offset_seconds: int = 0,
    roles: Optional[list[str]] = None,
    extra_claims: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build the LTI 1.3 claim payload for a test launch.

    The default values produce a valid launch that
    ``LtiIdToken.from_jwt_payload`` accepts and
    ``process_launch`` resolves to a learner. Tests override
    individual fields to exercise boundary cases:

    * ``exp_offset_seconds = -1`` produces an expired token.
    * ``policy_config_name = ""`` (or unset) produces a missing
      custom-claim payload that ``require_policy_config_name``
      rejects.
    * ``roles = ["http://purl.imsglobal.org/vocab/lis/v2/membership#Bogus"]``
      produces an unknown role URI.
    * ``iss_override`` decouples the issuer claim from the
      ``state``-registered issuer (the wrong-issuer path).
    """

    now = int(time.time())
    effective_roles = list(roles) if roles is not None else [role_uri]
    payload: dict[str, Any] = {
        "iss": iss_override if iss_override is not None else issuer,
        "sub": subject,
        "aud": aud_override if aud_override is not None else audience,
        "exp": now + exp_offset_seconds,
        "iat": now + iat_offset_seconds,
        "nonce": nonce,
        "https://purl.imsglobal.org/spec/lti/claim/message_type": "LtiResourceLinkRequest",
        "https://purl.imsglobal.org/spec/lti/claim/version": "1.3.0",
        "https://purl.imsglobal.org/spec/lti/claim/deployment_id": "deployment-1",
        "https://purl.imsglobal.org/spec/lti/claim/roles": effective_roles,
        "https://purl.imsglobal.org/spec/lti/claim/context": {
            "id": "course-101",
            "label": "CS101",
            "title": "Intro to CS",
        },
        "https://purl.imsglobal.org/spec/lti/claim/resource_link": {
            "id": "exam-midterm",
            "title": "Midterm Exam",
        },
        "https://purl.imsglobal.org/spec/lti/claim/tool_platform": {
            "guid": "lms-guid-1",
            "name": "Example LMS",
            "product_family_code": "example",
        },
        "https://purl.imsglobal.org/spec/lti/claim/target_link_uri": target_link_uri,
        "https://purl.imsglobal.org/spec/lti/claim/custom": {
            "policy_config_name": policy_config_name,
        },
        "name": name,
    }
    if extra_claims:
        payload.update(extra_claims)
    # ``state`` is part of the OIDC third-party-initiated
    # login flow (it rides alongside the LTI claims in the
    # signed JWT) but is not an LTI claim. The ``LtiIdToken``
    # model rejects unknown claims with ``extra=forbid``,
    # so callers that want to read it from a parsed payload
    # must not feed the helper's output through
    # ``LtiIdToken.from_jwt_payload``. The route handler
    # reads ``state`` from the unverified payload (before
    # claim-parsing) and removes it before constructing the
    # typed model. Test code that builds an ``LtiIdToken``
    # directly (e.g. the service unit tests) calls
    # ``build_signed_launch_claims`` *without* ``state``,
    # and the route tests do their own state-claim
    # construction.
    if state is not None:
        payload["state"] = state
    return payload


# --- helpers ---------------------------------------------------------


def _jwk_from_public_key(public_key: rsa.RSAPublicKey, kid: str) -> dict[str, Any]:
    """Encode a public key as a JWK dict.

    Uses base64url-no-padding (the RFC 7517 §3 wire format).
    The ``alg`` field is the suggested algorithm for the key;
    the launch flow uses it when verifying the JWT.
    """

    numbers = public_key.public_numbers()
    n_b64url = _b64url_uint(numbers.n)
    e_b64url = _b64url_uint(numbers.e)
    return {
        "kty": "RSA",
        "alg": "RS256",
        "use": "sig",
        "kid": kid,
        "n": n_b64url,
        "e": e_b64url,
    }


def _b64url_uint(value: int) -> str:
    """Encode an unsigned integer as base64url-no-padding (JWK form)."""

    byte_length = (value.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(value.to_bytes(byte_length, "big")).rstrip(b"=").decode("ascii")


__all__ = [
    "ADMIN_URI",
    "INSTRUCTOR_URI",
    "LEARNER_URI",
    "PROCTOR_URI",
    "TestOidcSetup",
    "build_signed_launch_claims",
    "make_test_oidc_setup",
    "register_oidc_responses",
]
