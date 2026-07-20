"""Unit tests for the JWKS fetcher and cache.

Boundary cases: a happy path that resolves a JWK by ``kid``; a
non-200 response; a malformed JWKS; an unknown ``kid`` triggers a
re-fetch; a key rotation handled by the re-fetch; a kid that is
genuinely absent after a fresh fetch is a hard error.
"""

from __future__ import annotations

import base64
import time
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from proctoring_engine.lti.jwks import JwksCache, JwksError


_JWKS_URL = "https://lms.example.edu/jwks"


def _generate_rsa_keypair() -> rsa.RSAPrivateKey:
    """Generate a fresh RSA keypair for the test harness."""

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64url_uint(value: int) -> str:
    """Encode an unsigned integer as base64url-no-padding (JWK form)."""

    byte_length = (value.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(value.to_bytes(byte_length, "big")).rstrip(b"=").decode("ascii")


def _jwk_from_public_key(public_key: rsa.RSAPublicKey, kid: str) -> dict[str, Any]:
    numbers = public_key.public_numbers()
    return {
        "kty": "RSA",
        "alg": "RS256",
        "use": "sig",
        "kid": kid,
        "n": _b64url_uint(numbers.n),
        "e": _b64url_uint(numbers.e),
    }


def _jwks_payload(*keys: rsa.RSAPrivateKey, kids: list[str]) -> dict[str, Any]:
    public_keys = [_jwk_from_public_key(k.public_key(), kid) for k, kid in zip(keys, kids)]
    return {"keys": public_keys}


async def test_get_key_returns_matching_jwk(httpx_mock) -> None:
    """A JWKS with the requested ``kid`` is parsed and returned."""

    key = _generate_rsa_keypair()
    httpx_mock.add_response(
        method="GET",
        url=_JWKS_URL,
        json=_jwks_payload(key, kids=["key-1"]),
    )
    cache = JwksCache()
    jwk = await cache.get_key(_JWKS_URL, "key-1")
    assert jwk.key_id == "key-1"


async def test_unknown_kid_triggers_refetch(httpx_mock) -> None:
    """An unknown ``kid`` re-fetches the JWKS (handles key rotation)."""

    key_old = _generate_rsa_keypair()
    key_new = _generate_rsa_keypair()
    # First response: only the old key. Second response: only the
    # new key. The cache must refresh when the first response has
    # no matching kid.
    httpx_mock.add_response(
        method="GET",
        url=_JWKS_URL,
        json=_jwks_payload(key_old, kids=["key-old"]),
    )
    httpx_mock.add_response(
        method="GET",
        url=_JWKS_URL,
        json=_jwks_payload(key_new, kids=["key-new"]),
    )
    cache = JwksCache()
    jwk = await cache.get_key(_JWKS_URL, "key-new")
    assert jwk.key_id == "key-new"
    assert len(httpx_mock.get_requests()) == 2


async def test_cache_hit_does_not_refetch(httpx_mock) -> None:
    """A cached JWKS within its TTL is reused."""

    key = _generate_rsa_keypair()
    httpx_mock.add_response(
        method="GET",
        url=_JWKS_URL,
        json=_jwks_payload(key, kids=["key-1"]),
    )
    cache = JwksCache()
    await cache.get_key(_JWKS_URL, "key-1")
    await cache.get_key(_JWKS_URL, "key-1")
    assert len(httpx_mock.get_requests()) == 1


async def test_stale_cache_triggers_refetch(httpx_mock) -> None:
    """A JWKS past its TTL is re-fetched on the next ``get_key``."""

    key_old = _generate_rsa_keypair()
    key_new = _generate_rsa_keypair()
    httpx_mock.add_response(
        method="GET",
        url=_JWKS_URL,
        json=_jwks_payload(key_old, kids=["key-1"]),
    )
    httpx_mock.add_response(
        method="GET",
        url=_JWKS_URL,
        json=_jwks_payload(key_new, kids=["key-1"]),
    )

    cache = JwksCache(ttl_seconds=0.001)
    await cache.get_key(_JWKS_URL, "key-1")
    time.sleep(0.05)
    await cache.get_key(_JWKS_URL, "key-1")
    assert len(httpx_mock.get_requests()) == 2


async def test_kid_genuinely_absent_raises(httpx_mock) -> None:
    """A ``kid`` not present even after a fresh fetch is a hard error."""

    key = _generate_rsa_keypair()
    payload = _jwks_payload(key, kids=["key-1"])
    # The cache refreshes once on a kid-miss to handle key
    # rotation, so two responses are needed to prove the kid is
    # genuinely absent.
    httpx_mock.add_response(method="GET", url=_JWKS_URL, json=payload)
    httpx_mock.add_response(method="GET", url=_JWKS_URL, json=payload)
    cache = JwksCache()
    with pytest.raises(JwksError):
        await cache.get_key(_JWKS_URL, "key-does-not-exist")


async def test_non_200_response_raises(httpx_mock) -> None:
    """A 5xx response is reported as ``JwksError``."""

    httpx_mock.add_response(method="GET", url=_JWKS_URL, status_code=503)
    cache = JwksCache()
    with pytest.raises(JwksError):
        await cache.get_key(_JWKS_URL, "any-kid")


async def test_non_json_response_raises(httpx_mock) -> None:
    """A non-JSON body is reported as ``JwksError``."""

    httpx_mock.add_response(method="GET", url=_JWKS_URL, text="not json")
    cache = JwksCache()
    with pytest.raises(JwksError):
        await cache.get_key(_JWKS_URL, "any-kid")


async def test_missing_keys_array_raises(httpx_mock) -> None:
    """A document without a ``keys`` array is rejected."""

    httpx_mock.add_response(method="GET", url=_JWKS_URL, json={"not": "keys"})
    cache = JwksCache()
    with pytest.raises(JwksError):
        await cache.get_key(_JWKS_URL, "any-kid")


async def test_key_without_kid_is_skipped(httpx_mock) -> None:
    """A JWK without a ``kid`` is silently skipped (cannot be referenced)."""

    key = _generate_rsa_keypair()
    jwk_no_kid = _jwk_from_public_key(key.public_key(), kid="ignored")
    del jwk_no_kid["kid"]
    payload = {"keys": [jwk_no_kid]}
    # Two responses to satisfy the rotation re-fetch; both are
    # the same JWKS with no kid, so the cache raises on the
    # second pass too.
    httpx_mock.add_response(method="GET", url=_JWKS_URL, json=payload)
    httpx_mock.add_response(method="GET", url=_JWKS_URL, json=payload)
    cache = JwksCache()
    with pytest.raises(JwksError):
        await cache.get_key(_JWKS_URL, "any-kid")


async def test_malformed_key_raises(httpx_mock) -> None:
    """A key with an inconsistent shape is rejected.

    ``PyJWK`` does not validate the modulus bytes at construction
    time (it only fails on a real signature verify), so a
    syntactically-broken base64url value would slip through. A
    genuinely malformed key — wrong ``kty`` for the supplied
    fields — is the smallest input ``PyJWK`` rejects.
    """

    bad = {
        "kty": "EC",  # wrong kty for the n/e fields below
        "alg": "RS256",
        "use": "sig",
        "kid": "key-bad",
        "n": _b64url_uint(65537),  # any value will do; kty check fires first
        "e": _b64url_uint(65537),
    }
    httpx_mock.add_response(method="GET", url=_JWKS_URL, json={"keys": [bad]})
    cache = JwksCache()
    with pytest.raises(JwksError):
        await cache.get_key(_JWKS_URL, "key-bad")


async def test_http_error_raises(httpx_mock) -> None:
    """A network error is surfaced as ``JwksError``."""

    httpx_mock.add_exception(httpx.ConnectError("connection refused"))
    cache = JwksCache()
    with pytest.raises(JwksError):
        await cache.get_key(_JWKS_URL, "any-kid")


async def test_invalidate_clears_cache(httpx_mock) -> None:
    """``invalidate`` drops the cached JWKS so the next call re-fetches."""

    key = _generate_rsa_keypair()
    httpx_mock.add_response(
        method="GET",
        url=_JWKS_URL,
        json=_jwks_payload(key, kids=["key-1"]),
    )
    httpx_mock.add_response(
        method="GET",
        url=_JWKS_URL,
        json=_jwks_payload(key, kids=["key-1"]),
    )
    cache = JwksCache()
    await cache.get_key(_JWKS_URL, "key-1")
    cache.invalidate(_JWKS_URL)
    await cache.get_key(_JWKS_URL, "key-1")
    assert len(httpx_mock.get_requests()) == 2


async def test_constructor_rejects_non_positive_ttl() -> None:
    """The TTL argument is validated at construction."""

    with pytest.raises(ValueError):
        JwksCache(ttl_seconds=0)
    with pytest.raises(ValueError):
        JwksCache(ttl_seconds=-1)
