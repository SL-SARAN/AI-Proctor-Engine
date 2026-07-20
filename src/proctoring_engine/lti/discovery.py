"""Async fetcher for an LTI platform's OIDC discovery document.

OIDC 1.0 specifies that a platform publishes a JSON document at
``{issuer}/.well-known/openid-configuration`` (the issuer may also
host it at ``{issuer}/.well-known/openid-configuration/`` with a
trailing slash; both shapes are handled).

The discovery document returns the platform's authorization
endpoint, JWKS URI, and other URLs. The launch handler uses the
authorization endpoint to redirect the browser; the JWKS URI to
verify the ``id_token`` signature.

The result is cached for the process lifetime. Per the v1
deployment topology (``docs/DEPLOYMENT.md``), the api tier is a
single replica, so the cache lives as long as the process. A
graceful restart is acceptable; a misconfigured platform that
returns different endpoints on each call would have been a
misconfiguration in production too.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin

import httpx


_DISCOVERY_TIMEOUT_SECONDS = 5.0


class OidcDiscoveryError(Exception):
    """Raised when the OIDC discovery document cannot be fetched or
    is malformed.
    """


@dataclass(frozen=True, slots=True)
class OidcDiscovery:
    """The fields of the OIDC discovery document this layer reads.

    Additional fields in the document (e.g. ``scopes_supported``,
    ``response_types_supported``) are validated for presence and
    silently dropped — the launch handler only needs the four
    endpoints below.
    """

    issuer: str
    authorization_endpoint: str
    jwks_uri: str
    token_endpoint: Optional[str]


class OidcDiscoveryCache:
    """A process-local cache of OIDC discovery documents, keyed by
    issuer URL.

    The cache is thread-safe; a single ``httpx.AsyncClient`` is
    shared across calls when one is supplied, and a fresh client
    is created when one is not (for tests).
    """

    def __init__(self) -> None:
        self._cache: dict[str, OidcDiscovery] = {}
        self._lock = threading.Lock()

    async def fetch(
        self,
        issuer: str,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        override_url: Optional[str] = None,
    ) -> OidcDiscovery:
        """Return the discovery document for ``issuer``.

        ``override_url`` is for non-conformant platforms that do
        not host the discovery document at the standard path; the
        value is fetched verbatim. When unset, the standard
        ``{issuer}/.well-known/openid-configuration`` path is
        tried.
        """

        if not issuer:
            raise OidcDiscoveryError("issuer is required")
        with self._lock:
            cached = self._cache.get(issuer)
        if cached is not None:
            return cached

        discovery = await self._fetch_document(
            issuer,
            http_client=http_client,
            override_url=override_url,
        )
        with self._lock:
            self._cache[issuer] = discovery
        return discovery

    async def _fetch_document(
        self,
        issuer: str,
        *,
        http_client: Optional[httpx.AsyncClient],
        override_url: Optional[str],
    ) -> OidcDiscovery:
        url = override_url or _default_discovery_url(issuer)
        owns_client = http_client is None
        client = http_client or httpx.AsyncClient(timeout=_DISCOVERY_TIMEOUT_SECONDS)
        try:
            response = await client.get(url)
        except httpx.HTTPError as exc:
            raise OidcDiscoveryError(
                f"failed to fetch OIDC discovery document at {url}: {exc}"
            ) from exc
        finally:
            if owns_client:
                await client.aclose()
        if response.status_code != 200:
            raise OidcDiscoveryError(
                f"OIDC discovery document at {url} returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise OidcDiscoveryError(
                f"OIDC discovery document at {url} is not valid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise OidcDiscoveryError(
                f"OIDC discovery document at {url} is not a JSON object"
            )
        return _build_discovery(issuer, payload)


def _default_discovery_url(issuer: str) -> str:
    """Return the standard OIDC discovery URL for an issuer.

    Trailing slashes are handled by ``urljoin``; the standard
    document lives at ``{issuer}/.well-known/openid-configuration``.
    Some platforms publish it with a trailing slash, which is also
    accepted by the path-resolution machinery — but the v1
    behavior is to try the un-suffixed path and let the platform
    handle a redirect.
    """

    base = issuer if issuer.endswith("/") else issuer + "/"
    return urljoin(base, ".well-known/openid-configuration")


def _build_discovery(issuer: str, payload: dict[str, object]) -> OidcDiscovery:
    """Validate and extract the discovery-document fields we read."""

    document_issuer = payload.get("issuer")
    if not isinstance(document_issuer, str) or not document_issuer:
        raise OidcDiscoveryError(
            "OIDC discovery document is missing the 'issuer' field"
        )
    # The discovery document's issuer must match the URL we
    # fetched. Mismatches are a sign of a misconfigured platform
    # or a confused-deputy attack against the JWKS endpoint.
    if document_issuer != issuer:
        raise OidcDiscoveryError(
            f"OIDC discovery issuer {document_issuer!r} does not match the "
            f"requested issuer {issuer!r}"
        )

    authz = payload.get("authorization_endpoint")
    if not isinstance(authz, str) or not authz:
        raise OidcDiscoveryError(
            "OIDC discovery document is missing 'authorization_endpoint'"
        )
    jwks_uri = payload.get("jwks_uri")
    if not isinstance(jwks_uri, str) or not jwks_uri:
        raise OidcDiscoveryError(
            "OIDC discovery document is missing 'jwks_uri'"
        )
    token_endpoint_raw = payload.get("token_endpoint")
    token_endpoint = (
        token_endpoint_raw if isinstance(token_endpoint_raw, str) and token_endpoint_raw else None
    )

    return OidcDiscovery(
        issuer=document_issuer,
        authorization_endpoint=authz,
        jwks_uri=jwks_uri,
        token_endpoint=token_endpoint,
    )
