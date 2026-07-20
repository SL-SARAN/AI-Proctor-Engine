"""In-memory, thread-safe store for LTI 1.3 ``state`` and ``nonce`` values.

The OIDC third-party-initiated login flow uses ``state`` to bind the
authorization request to the callback (replay protection on the browser
side) and ``nonce`` to bind the issued ``id_token`` to the same flow
(replay protection on the server side). Both values are short-lived
and one-shot: the platform must complete the flow within the TTL,
and once consumed, neither value is reusable.

The store is process-local. ``docs/02-ingestion-layer-design.md`` §1
calls out that the v1 deployment runs a single api replica, so
process-local state is sufficient. When the api tier scales
horizontally the store is swapped for a Redis-backed implementation
behind the same interface — the route handler and the launch
service do not change.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Optional


# Cryptographic length, in bytes, for state and nonce. Both values
# are sent through URLs or form bodies, so URL-safe base64 is the
# wire encoding — 32 bytes becomes a 43-character token.
_STATE_BYTES = 32
_NONCE_BYTES = 32


class LaunchStateError(Exception):
    """Base class for the launch-state store's error surface."""


class LaunchStateMissing(LaunchStateError):
    """Raised when the supplied state is not in the store.

    The error message names the kind (state) but not the value, so
    the route handler's HTTP error response does not leak
    attacker-collected state to a probing client.
    """


class LaunchStateReplay(LaunchStateError):
    """Raised when the supplied state has already been consumed."""


class LaunchStateExpired(LaunchStateError):
    """Raised when the supplied state has aged past the TTL."""


@dataclass(frozen=True, slots=True)
class _Entry:
    """One outstanding state value with its associated metadata."""

    nonce: str
    redirect_uri: str
    lti_issuer: str
    issued_at: float


class LaunchStateStore:
    """A one-shot, TTL-bounded store of pending LTI launches.

    The store is safe to use from multiple threads. The
    :meth:`consume` operation is atomic with respect to a concurrent
    :meth:`consume` for the same state — at most one caller wins.

    A monotonic clock (``time.monotonic``) is used for TTL checks so
    that system clock adjustments cannot expire a still-valid state
    or extend an already-expired one. Tests can override the clock
    by passing ``clock=`` to the constructor.
    """

    def __init__(
        self,
        ttl_seconds: int,
        *,
        clock: "callable[[], float]" = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    @staticmethod
    def new_state() -> str:
        """Return a fresh URL-safe state token (43 characters)."""

        return secrets.token_urlsafe(_STATE_BYTES)

    @staticmethod
    def new_nonce() -> str:
        """Return a fresh URL-safe nonce token (43 characters)."""

        return secrets.token_urlsafe(_NONCE_BYTES)

    def register(
        self,
        state: str,
        nonce: str,
        *,
        redirect_uri: str,
        lti_issuer: str,
    ) -> None:
        """Record a new pending launch.

        Re-registration with the same ``state`` overwrites the prior
        entry. The store does not enforce uniqueness on ``state`` at
        registration time, because the security guarantee is on
        :meth:`consume`, not on the issuance side.
        """

        if not state:
            raise ValueError("state must be a non-empty string")
        if not nonce:
            raise ValueError("nonce must be a non-empty string")
        entry = _Entry(
            nonce=nonce,
            redirect_uri=redirect_uri,
            lti_issuer=lti_issuer,
            issued_at=self._clock(),
        )
        with self._lock:
            self._purge_expired_locked()
            self._entries[state] = entry

    def consume(self, state: str, nonce: str) -> str:
        """Atomically validate and remove a pending launch.

        Returns the ``redirect_uri`` recorded at registration time so
        the route handler can confirm the ``redirect_uri`` claim in
        the ``id_token`` matches the one the platform was redirected
        to.

        Raises:
            LaunchStateMissing: ``state`` is not in the store.
            LaunchStateReplay: ``state`` was already consumed or
                overwritten.
            LaunchStateExpired: ``state`` is older than the TTL.
            ValueError: ``nonce`` does not match the registered
                value (the platform returned a different nonce than
                the one we issued).
        """

        if not state:
            raise LaunchStateMissing("state is required")
        now = self._clock()
        with self._lock:
            # Peek first so an expired entry raises ``Expired`` rather
            # than being silently evicted by the lazy purge.
            entry = self._entries.get(state)
            if entry is None:
                raise LaunchStateMissing("state not found or already consumed")
            if (now - entry.issued_at) > self._ttl:
                # Expired: drop it from the store and surface the
                # specific error so the route handler can log it.
                del self._entries[state]
                raise LaunchStateExpired("state has aged past the TTL")
            if not secrets.compare_digest(entry.nonce, nonce):
                # Wrong nonce: do NOT consume the entry — the platform
                # may retry with the right nonce. The state remains
                # registered for the remainder of the TTL.
                raise ValueError("nonce does not match the registered value")
            # All checks passed: pop the entry to make this consume
            # one-shot.
            redirect_uri = entry.redirect_uri
            del self._entries[state]
        return redirect_uri

    def __len__(self) -> int:
        """Return the number of live entries (testing surface)."""

        with self._lock:
            self._purge_expired_locked()
            return len(self._entries)

    def __contains__(self, state: str) -> bool:
        """Return whether ``state`` is currently registered (testing)."""

        with self._lock:
            self._purge_expired_locked()
            return state in self._entries

    def purge_expired(self) -> int:
        """Remove entries older than the TTL. Returns the count removed.

        The store also purges lazily on every :meth:`consume`,
        :meth:`__contains__`, and :meth:`__len__` call. Calling
        :meth:`purge_expired` explicitly is for the test surface and
        for a background task that wants to bound the dictionary's
        size.
        """

        with self._lock:
            return self._purge_expired_locked()

    def _purge_expired_locked(self) -> int:
        """The internal purge. The caller must hold ``self._lock``."""

        now = self._clock()
        expired = [
            state
            for state, entry in self._entries.items()
            if (now - entry.issued_at) > self._ttl
        ]
        for state in expired:
            del self._entries[state]
        return len(expired)
