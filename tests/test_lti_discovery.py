"""Unit tests for the OIDC discovery-document fetcher.

Boundary cases: a happy path against a mocked OIDC provider, a
non-200 response, a malformed response (not JSON, not an object),
a missing field, and an issuer mismatch (the document's
``issuer`` does not match the URL we fetched).
"""

from __future__ import annotations

import httpx
import pytest

from proctoring_engine.lti.discovery import (
    OidcDiscovery,
    OidcDiscoveryCache,
    OidcDiscoveryError,
)


_DISCOVERY_URL = "https://lms.example.edu/.well-known/openid-configuration"
_ISSUER = "https://lms.example.edu"


def _valid_document() -> dict:
    return {
        "issuer": _ISSUER,
        "authorization_endpoint": f"{_ISSUER}/auth",
        "jwks_uri": f"{_ISSUER}/jwks",
        "token_endpoint": f"{_ISSUER}/token",
        "scopes_supported": ["openid"],
    }


async def test_happy_path_returns_typed_discovery(httpx_mock) -> None:
    """A well-formed discovery document is parsed and returned."""

    httpx_mock.add_response(
        method="GET",
        url=_DISCOVERY_URL,
        json=_valid_document(),
    )
    cache = OidcDiscoveryCache()
    discovery = await cache.fetch(_ISSUER)
    assert isinstance(discovery, OidcDiscovery)
    assert discovery.issuer == _ISSUER
    assert discovery.authorization_endpoint == f"{_ISSUER}/auth"
    assert discovery.jwks_uri == f"{_ISSUER}/jwks"
    assert discovery.token_endpoint == f"{_ISSUER}/token"


async def test_cache_returns_same_instance(httpx_mock) -> None:
    """A second fetch for the same issuer does not refetch."""

    httpx_mock.add_response(
        method="GET",
        url=_DISCOVERY_URL,
        json=_valid_document(),
    )
    cache = OidcDiscoveryCache()
    first = await cache.fetch(_ISSUER)
    second = await cache.fetch(_ISSUER)
    assert first is second
    # ``pytest-httpx`` would raise if the mocked URL were called
    # twice; the assertion below makes the intent explicit.
    assert len(httpx_mock.get_requests()) == 1


async def test_override_url_skips_default_path(httpx_mock) -> None:
    """``override_url`` bypasses the standard ``.well-known`` path."""

    override = "https://lms.example.edu/custom/discovery.json"
    httpx_mock.add_response(method="GET", url=override, json=_valid_document())
    cache = OidcDiscoveryCache()
    discovery = await cache.fetch(
        _ISSUER,
        override_url=override,
    )
    assert discovery.issuer == _ISSUER


async def test_non_200_response_raises(httpx_mock) -> None:
    """A 4xx/5xx response is reported as ``OidcDiscoveryError``."""

    httpx_mock.add_response(
        method="GET",
        url=_DISCOVERY_URL,
        status_code=500,
        text="upstream error",
    )
    cache = OidcDiscoveryCache()
    with pytest.raises(OidcDiscoveryError):
        await cache.fetch(_ISSUER)


async def test_non_json_response_raises(httpx_mock) -> None:
    """A non-JSON body is reported as ``OidcDiscoveryError``."""

    httpx_mock.add_response(
        method="GET",
        url=_DISCOVERY_URL,
        text="<html>not json</html>",
    )
    cache = OidcDiscoveryCache()
    with pytest.raises(OidcDiscoveryError):
        await cache.fetch(_ISSUER)


async def test_non_object_response_raises(httpx_mock) -> None:
    """A JSON array (not an object) is rejected."""

    httpx_mock.add_response(
        method="GET",
        url=_DISCOVERY_URL,
        json=["not", "an", "object"],
    )
    cache = OidcDiscoveryCache()
    with pytest.raises(OidcDiscoveryError):
        await cache.fetch(_ISSUER)


async def test_issuer_mismatch_raises(httpx_mock) -> None:
    """A document whose ``issuer`` does not match the URL is rejected."""

    bad = _valid_document()
    bad["issuer"] = "https://different.example.edu"
    httpx_mock.add_response(method="GET", url=_DISCOVERY_URL, json=bad)
    cache = OidcDiscoveryCache()
    with pytest.raises(OidcDiscoveryError):
        await cache.fetch(_ISSUER)


async def test_missing_authorization_endpoint_raises(httpx_mock) -> None:
    """A document missing ``authorization_endpoint`` is rejected."""

    bad = _valid_document()
    del bad["authorization_endpoint"]
    httpx_mock.add_response(method="GET", url=_DISCOVERY_URL, json=bad)
    cache = OidcDiscoveryCache()
    with pytest.raises(OidcDiscoveryError):
        await cache.fetch(_ISSUER)


async def test_missing_jwks_uri_raises(httpx_mock) -> None:
    """A document missing ``jwks_uri`` is rejected."""

    bad = _valid_document()
    del bad["jwks_uri"]
    httpx_mock.add_response(method="GET", url=_DISCOVERY_URL, json=bad)
    cache = OidcDiscoveryCache()
    with pytest.raises(OidcDiscoveryError):
        await cache.fetch(_ISSUER)


async def test_empty_issuer_raises(httpx_mock) -> None:
    """An empty issuer argument is rejected before any HTTP call."""

    cache = OidcDiscoveryCache()
    with pytest.raises(OidcDiscoveryError):
        await cache.fetch("")


async def test_http_error_raises(httpx_mock) -> None:
    """A network error is surfaced as ``OidcDiscoveryError``."""

    httpx_mock.add_exception(
        httpx.ConnectError("connection refused"),
    )
    cache = OidcDiscoveryCache()
    with pytest.raises(OidcDiscoveryError):
        await cache.fetch(_ISSUER)


async def test_token_endpoint_may_be_absent(httpx_mock) -> None:
    """A discovery document without ``token_endpoint`` is accepted;
    the launch flow does not need a token endpoint.
    """

    doc = _valid_document()
    del doc["token_endpoint"]
    httpx_mock.add_response(method="GET", url=_DISCOVERY_URL, json=doc)
    cache = OidcDiscoveryCache()
    discovery = await cache.fetch(_ISSUER)
    assert discovery.token_endpoint is None
