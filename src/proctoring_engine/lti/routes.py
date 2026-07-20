"""FastAPI routes for the LTI 1.3 launch flow.

Two endpoints:

* ``GET /lti/login`` — login initiation. The platform redirects
  the browser here with ``iss``, ``login_hint``,
  ``target_link_uri``, ``lti_message_hint``. We generate a
  32-byte ``state`` and ``nonce``, register them in the
  in-memory :class:`LaunchStateStore`, fetch the platform's
  OIDC discovery document, and 302-redirect to the platform's
  authorization endpoint with the OIDC third-party-initiated
  login query string.
* ``POST /lti/launch`` — launch callback. The platform
  ``form_post``-redirects the browser here with an ``id_token``
  JWT in the form body. We verify the JWT against the
  platform's JWKS, parse the LTI 1.3 claims, consume the
  ``state`` / ``nonce`` from the in-memory store (one-shot,
  fail-closed), then call :func:`process_launch`. On success
  we 302 to the exam client or the admin surface; on failure
  we return 400 with a closed error code.

The router is built via the :func:`build_lti_router` factory
so tests can wire their own :class:`LaunchStateStore`,
:class:`JwksCache`, and :class:`OidcDiscoveryCache`. The
production wiring lives in :mod:`proctoring_engine.api`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from proctoring_engine.lti.claims import (
    LtiClaimsError,
    LtiIdToken,
    require_policy_config_name,
)
from proctoring_engine.lti.config import LtiSettings
from proctoring_engine.lti.discovery import OidcDiscovery, OidcDiscoveryCache, OidcDiscoveryError
from proctoring_engine.lti.jwks import JwksCache, JwksError
from proctoring_engine.lti.roles import AppRole, map_roles
from proctoring_engine.lti.service import (
    LtiLaunchError,
    LtiLaunchErrorCode,
    process_launch,
)
from proctoring_engine.lti.state import (
    LaunchStateExpired,
    LaunchStateMissing,
    LaunchStateStore,
)


# --- error code mapping -------------------------------------------------


# The closed enumeration of HTTP-level error codes the launch
# handler returns. The route handler maps each LTI-layer failure
# to one of these. Closed (not free-form) so the test suite can
# assert on the code in the response body.
_LAUNCH_ERROR_HTTP = {
    "signature_invalid": status.HTTP_400_BAD_REQUEST,
    "claims_invalid": status.HTTP_400_BAD_REQUEST,
    "policy_not_found": status.HTTP_400_BAD_REQUEST,
    "state_unknown": status.HTTP_400_BAD_REQUEST,
    "state_expired": status.HTTP_400_BAD_REQUEST,
    "nonce_mismatch": status.HTTP_400_BAD_REQUEST,
    "audience_invalid": status.HTTP_400_BAD_REQUEST,
    "issuer_invalid": status.HTTP_400_BAD_REQUEST,
    "discovery_error": status.HTTP_502_BAD_GATEWAY,
}


def _http_error(code: str, detail: str = "") -> HTTPException:
    """Build an :class:`HTTPException` with a closed error code."""

    return HTTPException(
        status_code=_LAUNCH_ERROR_HTTP.get(code, status.HTTP_400_BAD_REQUEST),
        detail={"code": code, "message": detail or code},
    )


# --- request / response helpers ---------------------------------------


@dataclass(frozen=True, slots=True)
class _RouterDeps:
    """The dependencies :func:`build_lti_router` threads into
    the route handlers.

    A small dataclass keeps the dependency surface explicit:
    a route handler that needs a ``JwksCache`` reads it from
    ``deps.jwks_cache``, not from a hidden module-level
    global. This is what makes the unit tests
    straightforward — they instantiate a fresh
    ``_RouterDeps`` with their own services and pass it to
    ``build_lti_router``.
    """

    settings: LtiSettings
    state_store: LaunchStateStore
    jwks_cache: JwksCache
    discovery_cache: OidcDiscoveryCache
    http_client_factory: Callable[[], httpx.AsyncClient]
    get_db: Callable[[], Session]


def build_lti_router(deps: _RouterDeps) -> APIRouter:
    """Build the LTI 1.3 router.

    Returns a FastAPI :class:`APIRouter` with ``/lti/login`` and
    ``/lti/launch`` mounted. The router is not wired to a database
    session globally; the database session is requested via the
    ``deps.get_db`` callable so the integration test can swap
    in a transactional session.
    """

    router = APIRouter(tags=["lti"])

    @router.get("/lti/login")
    async def lti_login(
        iss: str = Query(..., min_length=1),
        login_hint: str = Query(..., min_length=1),
        target_link_uri: str = Query(..., min_length=1),
        lti_message_hint: str = Query(..., min_length=1),
        client_id: Optional[str] = Query(default=None),
    ) -> RedirectResponse:
        """Login initiation. The route that the platform calls to
        start a third-party-initiated OIDC login.
        """

        # The target_link_uri must match the registered launch URL
        # (LTI 1.3 §"Tool registration"). A mismatch is a
        # misconfiguration that we report as 400, not as a
        # silent 302 to the wrong destination.
        if target_link_uri != deps.settings.launch_url:
            raise _http_error(
                "claims_invalid",
                f"target_link_uri {target_link_uri!r} does not match the "
                f"registered launch URL",
            )

        # Fetch (and cache) the OIDC discovery document. The
        # platform's issuer is the cache key.
        try:
            discovery = await deps.discovery_cache.fetch(
                iss,
                http_client=deps.http_client_factory(),
                override_url=deps.settings.oidc_discovery_url,
            )
        except OidcDiscoveryError as exc:
            raise _http_error("discovery_error", str(exc)) from exc

        # Generate the state and nonce. Both are 32 bytes of
        # cryptographic randomness, URL-safe. The state is
        # bound to the launch URL and the issuer; the nonce is
        # the server-side replay protection.
        state = LaunchStateStore.new_state()
        nonce = LaunchStateStore.new_nonce()
        deps.state_store.register(
            state,
            nonce,
            redirect_uri=deps.settings.launch_url,
            lti_issuer=iss,
        )

        # Build the OIDC third-party-initiated login query
        # string. ``response_mode=form_post`` is the standard
        # LTI 1.3 mode; ``prompt=none`` is the platform's hint
        # that the user is already authenticated.
        effective_client_id = client_id or deps.settings.tool_client_id
        params = {
            "scope": "openid",
            "response_type": "id_token",
            "response_mode": "form_post",
            "prompt": "none",
            "client_id": effective_client_id,
            "redirect_uri": deps.settings.launch_url,
            "login_hint": login_hint,
            "state": state,
            "nonce": nonce,
            "lti_message_hint": lti_message_hint,
        }
        authz_url = f"{discovery.authorization_endpoint}?{urlencode(params)}"
        return RedirectResponse(url=authz_url, status_code=status.HTTP_302_FOUND)

    @router.post("/lti/launch")
    async def lti_launch(
        request: Request,
        id_token: str = Form(..., alias="id_token"),
    ) -> RedirectResponse:
        """Launch callback. The platform ``form_post``-redirects
        the browser here after the user authenticates.
        """

        # 1. Decode the JWT header to learn the ``kid`` and
        #    ``alg``. The header is *not* verified here; the
        #    verification happens below when we have the
        #    platform's JWK. We read the header to know which
        #    key to fetch.
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.InvalidTokenError as exc:
            raise _http_error("claims_invalid", "id_token header is invalid") from exc

        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise _http_error("claims_invalid", "id_token is missing the 'kid' header")

        # 2. Decode the JWT payload (unverified) to learn the
        #    issuer. The issuer names the JWKS endpoint we
        #    must fetch. We re-verify the signature below
        #    using the JWK from the platform's JWKS.
        try:
            unverified = jwt.decode(
                id_token,
                options={"verify_signature": False},
            )
        except jwt.InvalidTokenError as exc:
            raise _http_error("claims_invalid", "id_token payload is invalid") from exc

        issuer = unverified.get("iss")
        if not isinstance(issuer, str) or not issuer:
            raise _http_error("claims_invalid", "id_token is missing the 'iss' claim")

        # 2a. If the launch carries an OIDC ``state``, peek the
        #     state store to validate the ``iss`` claim against
        #     the state-registered issuer. A cross-issuer
        #     replay attempt (a state issued for one platform
        #     being replayed against another) is rejected here
        #     so we don't make an outbound HTTP call to the
        #     wrong platform's discovery endpoint. The actual
        #     state / nonce consumption still happens at step
        #     7 — peek does not remove the entry.
        state_value = unverified.get("state", "")
        if isinstance(state_value, str) and state_value:
            registered_issuer = deps.state_store.peek(state_value)
            if registered_issuer is not None and registered_issuer != issuer:
                raise _http_error(
                    "issuer_invalid",
                    f"id_token 'iss' claim {issuer!r} does not match the state-registered issuer",
                )

        # 3. Fetch the JWKS for the platform. The
        #    OidcDiscoveryCache owns the issuer → jwks_uri
        #    mapping; the JwksCache owns the jwks_uri → JWK
        #    set cache.
        try:
            discovery = await deps.discovery_cache.fetch(
                issuer,
                http_client=deps.http_client_factory(),
                override_url=deps.settings.oidc_discovery_url,
            )
        except OidcDiscoveryError as exc:
            raise _http_error("discovery_error", str(exc)) from exc

        try:
            jwk = await deps.jwks_cache.get_key(
                discovery.jwks_uri,
                kid,
                http_client=deps.http_client_factory(),
            )
        except JwksError as exc:
            raise _http_error("signature_invalid", str(exc)) from exc

        # 4. Verify the signature and the standard claims
        #    (``iss``, ``aud``, ``exp``, ``iat``). The
        #    audience is configurable — some platforms use the
        #    tool's deployment id, others the client id. The
        #    ``audience_override`` setting is for the
        #    non-conformant platforms.
        expected_aud = deps.settings.audience_override or deps.settings.tool_client_id
        try:
            payload = jwt.decode(
                id_token,
                key=jwk.key,
                algorithms=[jwk.algorithm_name or "RS256"],
                audience=expected_aud,
                issuer=issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise _http_error("claims_invalid", "id_token has expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise _http_error("audience_invalid", str(exc)) from exc
        except jwt.InvalidIssuerError as exc:
            raise _http_error("issuer_invalid", str(exc)) from exc
        except jwt.InvalidTokenError as exc:
            raise _http_error("signature_invalid", str(exc)) from exc

        # 5. Build the typed LTI 1.3 model. The parse path
        #    enforces the message-type, version, and required
        #    claim checks. ``require_policy_config_name``
        #    further enforces the custom claim.
        #
        #    The OIDC ``state`` claim rides alongside the LTI
        #    claims in the signed JWT body but is not an LTI
        #    claim, and the typed ``LtiIdToken`` model rejects
        #    unknown claims with ``extra=forbid``. We pop
        #    ``state`` before constructing the model; the
        #    OIDC-flow ``state`` was already consumed above
        #    and is no longer needed.
        try:
            claims_payload = dict(payload)
            claims_payload.pop("state", None)
            claims = LtiIdToken.from_jwt_payload(claims_payload)
            policy_name = require_policy_config_name(claims)
        except LtiClaimsError as exc:
            raise _http_error("claims_invalid", str(exc)) from exc
        del policy_name  # the service resolves the policy

        # 6. Map the LTI role URIs to an application role.
        #    This is part of the parse path semantically: a
        #    launch with an unknown role URI is rejected
        #    here, not silently demoted to learner.
        try:
            role = map_roles(claims.roles)
        except ValueError as exc:
            raise _http_error("claims_invalid", str(exc)) from exc

        # 7. Consume the state / nonce. This is the
        #    OIDC-third-party-initiated-login replay
        #    protection: a state can only be consumed once.
        #    The ``lti_issuer`` check happens earlier in the
        #    flow (step 2a) so a wrong-iss request is rejected
        #    before the discovery fetch.
        state_value = unverified.get("state", "")
        if not isinstance(state_value, str) or not state_value:
            raise _http_error("claims_invalid", "id_token is missing the 'state' claim")
        try:
            deps.state_store.consume(state_value, claims.nonce)
        except LaunchStateMissing as exc:
            raise _http_error("state_unknown", "state not found or already consumed") from exc
        except LaunchStateExpired as exc:
            raise _http_error("state_expired", "state has aged past the TTL") from exc
        except ValueError as exc:
            raise _http_error("nonce_mismatch", str(exc)) from exc

        # 8. Persist the launch + issue the session token.
        #    The route commits after the service returns so
        #    the launch is a single atomic transaction. A
        #    failure between "participant upserted" and
        #    "session created" rolls back the whole launch.
        db = deps.get_db()
        try:
            result = process_launch(db, claims, role, settings=deps.settings)
            db.commit()
        except LtiLaunchError as exc:
            db.rollback()
            raise _http_error(exc.code.value, str(exc)) from exc
        except Exception:
            db.rollback()
            raise

        return RedirectResponse(
            url=result.redirect_url, status_code=status.HTTP_302_FOUND
        )

    return router
