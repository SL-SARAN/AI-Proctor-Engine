"""Async fetcher and parser for an LTI platform's JWKS document.

The JWKS (JSON Web Key Set) is the platform's public-key
publication. The ``id_token`` JWT carries a ``kid`` claim that
identifies which key signed it; the tool fetches the matching
public key from the JWKS endpoint and verifies the signature
against it.

The fetcher caches the JWKS per-URI for a bounded TTL (10 minutes
by default). When a JWT presents an unknown ``kid`` — a key
rotation event — the cache is invalidated and the JWKS is
re-fetched. This handles both *additive* rotations (the new
key is added to the set; old tokens still verify) and *replacing*
rotations (the old key is removed; the cache is updated on the
next miss).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
import jwt
from jwt import PyJWK


_DEFAULT_JWKS_TTL_SECONDS = 600.0
_JWKS_TIMEOUT_SECONDS = 5.0


class JwksError(Exception):
    """Raised when a JWKS document cannot be fetched or parsed."""


@dataclass(frozen=True, slots=True)
class JwksEntry:
    """One cached JWKS document plus the time it was fetched.

    The ``fetched_at`` field is the ``time.monotonic`` reading at
    fetch time, so a system clock adjustment cannot extend or
    shorten the cache window.
    """

    jwks_uri: str
    keys_by_kid: dict[str, PyJWK]
    fetched_at: float
    ttl_seconds: float

    def is_fresh(self, *, now: Optional[float] = None) -> bool:
        """Return whether the entry is still within its TTL."""

        current = now if now is not None else time.monotonic()
        return (current - self.fetched_at) < self.ttl_seconds


class JwksCache:
    """A TTL-bounded, thread-safe cache of JWKS documents.

    The cache is keyed by JWKS URI and stores the parsed
    :class:`jwt.PyJWK` objects keyed by ``kid``. A
    :class:`httpx.AsyncClient` may be supplied for connection
    reuse; when not supplied, a fresh client is created for each
    fetch (the test suite passes a client backed by
    ``pytest-httpx``).
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = _DEFAULT_JWKS_TTL_SECONDS,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl = ttl_seconds
        self._cache: dict[str, JwksEntry] = {}
        self._lock = threading.Lock()

    async def get_key(
        self,
        jwks_uri: str,
        kid: str,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> PyJWK:
        """Return the JWK with the given ``kid``, fetching as needed.

        Resolution rules:

        1. If a fresh cache entry exists and contains the ``kid``,
           return it.
        2. Otherwise, refresh the entry. If the refreshed entry
           contains the ``kid``, return it.
        3. If the refreshed entry still does not contain the
           ``kid``, refresh *once more* — this handles the
           key-rotation case where the platform just started
           publishing a new key and the first response predates
           it. A ``kid`` that is still missing after the second
           refresh is a hard error: the platform signed the JWT
           with a key it does not publish.
        """

        cached = self._entry_locked(jwks_uri)
        if cached is not None and kid in cached.keys_by_kid and cached.is_fresh():
            return cached.keys_by_kid[kid]

        await self._refresh(jwks_uri, http_client=http_client)
        refreshed = self._entry_locked(jwks_uri)
        if refreshed is not None and kid in refreshed.keys_by_kid:
            return refreshed.keys_by_kid[kid]

        # Key rotation: the first refresh may have happened before
        # the platform published the new key. Refresh again before
        # giving up — at most once, so a genuinely absent kid
        # surfaces as a hard error rather than a hot loop.
        await self._refresh(jwks_uri, http_client=http_client)
        refreshed = self._entry_locked(jwks_uri)
        if refreshed is None or kid not in refreshed.keys_by_kid:
            raise JwksError(
                f"JWKS at {jwks_uri} does not contain a key with kid={kid!r}"
            )
        return refreshed.keys_by_kid[kid]

    async def refresh(
        self,
        jwks_uri: str,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> JwksEntry:
        """Force-refresh the cache entry for ``jwks_uri``.

        Public entry point for an admin endpoint that wants to
        flush the cache (e.g. after a manual key-rotation event).
        Returns the new entry.
        """

        await self._refresh(jwks_uri, http_client=http_client)
        entry = self._entry_locked(jwks_uri)
        assert entry is not None  # invariant: refresh always populates
        return entry

    def invalidate(self, jwks_uri: str) -> None:
        """Drop the cache entry for ``jwks_uri`` if any."""

        with self._lock:
            self._cache.pop(jwks_uri, None)

    def _entry_locked(self, jwks_uri: str) -> Optional[JwksEntry]:
        with self._lock:
            return self._cache.get(jwks_uri)

    async def _refresh(
        self,
        jwks_uri: str,
        *,
        http_client: Optional[httpx.AsyncClient],
    ) -> None:
        owns_client = http_client is None
        client = http_client or httpx.AsyncClient(timeout=_JWKS_TIMEOUT_SECONDS)
        try:
            response = await client.get(jwks_uri)
        except httpx.HTTPError as exc:
            raise JwksError(f"failed to fetch JWKS at {jwks_uri}: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()
        if response.status_code != 200:
            raise JwksError(
                f"JWKS at {jwks_uri} returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise JwksError(f"JWKS at {jwks_uri} is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise JwksError(f"JWKS at {jwks_uri} is not a JSON object")
        keys_raw = payload.get("keys")
        if not isinstance(keys_raw, list):
            raise JwksError(f"JWKS at {jwks_uri} is missing the 'keys' array")

        keys_by_kid: dict[str, PyJWK] = {}
        for key_dict in keys_raw:
            if not isinstance(key_dict, dict):
                raise JwksError(
                    f"JWKS at {jwks_uri} contains a non-object key entry"
                )
            kid = key_dict.get("kid")
            if not isinstance(kid, str) or not kid:
                # Skip keys without a kid; they cannot be referenced
                # by an id_token's header, so storing them would be
                # dead weight. This is per RFC 7517 §4.5.
                continue
            try:
                keys_by_kid[kid] = PyJWK(key_dict)
            except (jwt.InvalidKeyError, ValueError) as exc:
                raise JwksError(
                    f"JWKS at {jwks_uri} contains a malformed key with kid={kid!r}: {exc}"
                ) from exc

        entry = JwksEntry(
            jwks_uri=jwks_uri,
            keys_by_kid=keys_by_kid,
            fetched_at=time.monotonic(),
            ttl_seconds=self._ttl,
        )
        with self._lock:
            self._cache[jwks_uri] = entry
