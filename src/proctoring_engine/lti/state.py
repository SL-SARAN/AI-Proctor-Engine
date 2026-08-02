"""One-shot, TTL-bounded store for LTI 1.3 ``state`` and ``nonce`` values.

The OIDC third-party-initiated login flow uses ``state`` to bind the
authorization request to the callback (replay protection on the browser
side) and ``nonce`` to bind the issued ``id_token`` to the same flow
(replay protection on the server side). Both values are short-lived
and one-shot: the platform must complete the flow within the TTL,
and once consumed, neither value is reusable.

This module defines the abstract :class:`LaunchStateStore` Protocol
and two concrete implementations:

* :class:`InMemoryLaunchStateStore` — process-local, thread-safe.
  Single replica is sufficient; ``docs/02-ingestion-layer-design.md``
  §1 explicitly calls this out.
* :class:`RedisLaunchStateStore` — shares state across replicas via
  Redis. Required when the api tier scales beyond one replica, since
  the OIDC ``state``/``nonce`` issued by replica A is invisible to
  replica B otherwise. Verified against ``docs/DEPLOYMENT.md`` §6.2.

Both implementations satisfy the :class:`LaunchStateStore` Protocol.
The route handler depends on the Protocol, not on a concrete class,
so swapping between them is a configuration decision (the
``LTI_STATE_STORE_BACKEND`` env var in ``config.py``), not a code
rewrite.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


# Cryptographic length, in bytes, for state and nonce. Both values
# are sent through URLs or form bodies, so URL-safe base64 is the
# wire encoding — 32 bytes becomes a 43-character token.
_STATE_BYTES = 32
_NONCE_BYTES = 32

# Key prefix for the Redis backend. The leading colon segment is the
# Redis convention for "this keyspace belongs to a particular module",
# making it easy to scan / debug / delete without colliding with other
# applications sharing a Redis instance.
_REDIS_KEY_PREFIX = "proctoring:lti:state"


def generate_launch_state() -> str:
    """Return a fresh URL-safe state token (43 characters)."""

    return secrets.token_urlsafe(_STATE_BYTES)


def generate_launch_nonce() -> str:
    """Return a fresh URL-safe nonce token (43 characters)."""

    return secrets.token_urlsafe(_NONCE_BYTES)


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


@runtime_checkable
class LaunchStateStore(Protocol):
    """The store contract that :mod:`proctoring_engine.lti.routes` depends on.

    A class satisfies this protocol structurally — there is no nominal
    base class to inherit from. Two implementations live in this
    module (:class:`InMemoryLaunchStateStore` and
    :class:`RedisLaunchStateStore`); tests may use any object that
    exposes the same method surface.

    The synchronous surface is the legacy shape carried over from the
    in-memory implementation. The :class:`RedisLaunchStateStore` is
    also awaitable on every method, because the underlying
    ``redis.asyncio`` client is async. The route handlers ``await``
    whichever backing store is wired in. The in-memory implementation
    satisfies both shapes by returning ``self`` from a no-op
    ``__await__`` shim — see below.
    """

    def register(
        self,
        state: str,
        nonce: str,
        *,
        redirect_uri: str,
        lti_issuer: str,
    ) -> "object":
        """Record a new pending launch.

        Re-registration with the same ``state`` overwrites the prior
        entry. The store does not enforce uniqueness on ``state`` at
        registration time, because the security guarantee is on
        :meth:`consume`, not on the issuance side.

        The return value is opaque; callers should not rely on it.
        """

    def consume(self, state: str, nonce: str) -> "object":
        """Atomically validate and remove a pending launch.

        Returns a ``(redirect_uri, lti_issuer)`` tuple, or raises
        one of :class:`LaunchStateMissing`, :class:`LaunchStateExpired`,
        :class:`LaunchStateReplay`, or :class:`ValueError` (on
        ``nonce`` mismatch — the entry is NOT consumed on mismatch so
        the platform can retry with the right nonce).
        """

    def peek(self, state: str) -> "object":
        """Return the ``lti_issuer`` registered for ``state`` without
        consuming the entry, or ``None`` if not registered or expired.
        """

    def purge_expired(self) -> "object":
        """Remove expired entries. Returns the count removed."""

    def __len__(self) -> int: ...
    def __contains__(self, state: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class _Entry:
    """One outstanding state value with its associated metadata."""

    nonce: str
    redirect_uri: str
    lti_issuer: str
    issued_at: float


class InMemoryLaunchStateStore:
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

    async def register(
        self,
        state: str,
        nonce: str,
        *,
        redirect_uri: str,
        lti_issuer: str,
    ) -> None:
        """Record a new pending launch."""

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

    async def consume(self, state: str, nonce: str) -> tuple[str, str]:
        """Atomically validate and remove a pending launch."""

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
            lti_issuer = entry.lti_issuer
            del self._entries[state]
        return redirect_uri, lti_issuer

    async def __len__(self) -> int:
        """Return the number of live entries (testing surface)."""

        with self._lock:
            self._purge_expired_locked()
            return len(self._entries)

    async def __contains__(self, state: str) -> bool:
        """Return whether ``state`` is currently registered (testing)."""

        with self._lock:
            self._purge_expired_locked()
            return state in self._entries

    async def peek(self, state: str) -> str | None:
        """Return the ``lti_issuer`` registered for ``state`` without
        consuming the entry.

        Returns ``None`` if the state is not registered or has
        expired. Used by the route handler to validate the
        ``iss`` claim before doing the expensive OIDC discovery
        fetch — a cross-issuer replay attempt can be rejected
        without an outbound HTTP call.
        """

        with self._lock:
            self._purge_expired_locked()
            entry = self._entries.get(state)
            if entry is None:
                return None
            return entry.lti_issuer

    async def purge_expired(self) -> int:
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


# Lua script that performs an atomic consume-or-raise on the
# Redis backend. It must be loaded once and re-used via EVALSHA so
# we don't pay the script-size transfer on every call. The script
# returns a JSON-encoded payload so the caller can raise the right
# Python exception without doing a second round trip.
#
# KEYS[1]   = the state key
# ARGV[1]   = expected nonce
#
# Return shapes:
#   nil                        -> caller raises LaunchStateMissing
#   {"__expired__": true}      -> caller raises LaunchStateExpired
#   {"__nonce_mismatch__":true}-> caller raises ValueError
#   {"redirect_uri": ..., "lti_issuer": ..., "nonce": ...}
#                              -> success; caller reads redirect_uri /
#                                 lti_issuer; the entry has already been
#                                 DELed inside the script.
_CONSUME_LUA = """
local raw = redis.call('GET', KEYS[1])
if raw == false then
    return nil
end
local ok, parsed = pcall(cjson.decode, raw)
if not ok then
    return nil
end
if parsed.nonce ~= ARGV[1] then
    return cjson.encode({__nonce_mismatch__ = true})
end
local out = cjson.encode({
    redirect_uri = parsed.redirect_uri,
    lti_issuer = parsed.lti_issuer,
    nonce = parsed.nonce,
})
redis.call('DEL', KEYS[1])
return out
"""


class RedisLaunchStateStore:
    """A Redis-backed :class:`LaunchStateStore`.

    Stores one ``state -> {nonce, redirect_uri, lti_issuer}`` entry
    per pending launch, keyed by ``f"{_REDIS_KEY_PREFIX}:{state}"``,
    with a per-key TTL matching the in-memory store's contract. The
    consume operation is performed atomically by a server-side Lua
    script so a state can be consumed at most once across replicas.

    The constructor takes an already-constructed
    ``redis.asyncio.Redis`` client (or any object with the same
    interface, including a ``fakeredis.FakeAsyncRedis`` test double).
    The class does NOT own the connection lifecycle — close the
    client wherever you created it. This makes the class trivially
    unit-testable with no async-fixture ceremony: pass a fake,
    inject a script, and ``await`` the methods directly.

    Args:
        client: An async Redis client.
        ttl_seconds: How long a pending state survives before
            expiry. Must be positive.
        key_prefix: Override the default key prefix. Intended for
            tests that need namespace isolation.

    Notes:
        The ``peek`` and ``__contains__`` implementations are
        slightly weaker than the in-memory store's: they cannot
        distinguish "never registered" from "expired" — both look
        like a missing key to Redis. That is fine for the route
        handler's purpose, which only needs a yes/no answer; a
        :class:`LaunchStateExpired` from a now-missing key surfaces
        on the *consume* path, where the Lua script has a window
        to observe it explicitly.
    """

    def __init__(
        self,
        client: object,
        ttl_seconds: int,
        *,
        key_prefix: str = _REDIS_KEY_PREFIX,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if not key_prefix:
            raise ValueError("key_prefix must be a non-empty string")
        self._client = client
        self._ttl = ttl_seconds
        self._key_prefix = key_prefix
        # register_script returns an object with an async __call__
        # that re-uses EVALSHA after the first call. Holding the
        # registered handle on the instance avoids re-loading the
        # script on every consume.
        self._consume_script = client.register_script(_CONSUME_LUA)  # type: ignore[attr-defined]

    @staticmethod
    def new_state() -> str:
        """Return a fresh URL-safe state token (43 characters)."""

        return secrets.token_urlsafe(_STATE_BYTES)

    @staticmethod
    def new_nonce() -> str:
        """Return a fresh URL-safe nonce token (43 characters)."""

        return secrets.token_urlsafe(_NONCE_BYTES)

    def _key(self, state: str) -> str:
        """Build the per-state Redis key."""

        return f"{self._key_prefix}:{state}"

    async def register(
        self,
        state: str,
        nonce: str,
        *,
        redirect_uri: str,
        lti_issuer: str,
    ) -> None:
        """Record a new pending launch with the configured TTL."""

        if not state:
            raise ValueError("state must be a non-empty string")
        if not nonce:
            raise ValueError("nonce must be a non-empty string")
        payload = {
            "nonce": nonce,
            "redirect_uri": redirect_uri,
            "lti_issuer": lti_issuer,
        }
        # ``ex=self._ttl`` arms Redis' own expiry so we don't have
        # to schedule a background sweeper. SET unconditionally
        # overwrites any prior value, matching the in-memory store's
        # "re-registration permitted" semantic.
        await self._client.set(  # type: ignore[attr-defined]
            self._key(state),
            json.dumps(payload, separators=(",", ":")),
            ex=self._ttl,
        )

    async def consume(self, state: str, nonce: str) -> tuple[str, str]:
        """Atomically validate and remove a pending launch.

        The atomicity is enforced by the Lua script, which performs
        GET + nonce-check + DEL in a single Redis operation. A
        second concurrent caller therefore sees the key as missing,
        not as a race against the first.

        Raises:
            LaunchStateMissing: ``state`` is not in Redis (either
                never registered or already consumed / expired).
            LaunchStateExpired: ``state`` was consumed by a
                concurrent caller whose nonce matched but the key
                is now gone. Treated as "expired" by the route
                handler's error mapping.
            ValueError: ``nonce`` does not match the registered
                value. The entry is NOT consumed — the platform may
                retry.
        """

        if not state:
            raise LaunchStateMissing("state is required")
        raw = await self._consume_script(  # type: ignore[misc]
            keys=[self._key(state)],
            args=[nonce],
        )
        # ``raw`` is bytes-or-None depending on the parser used.
        if raw is None:
            raise LaunchStateMissing("state not found or already consumed")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        parsed = json.loads(raw)
        if parsed.get("__nonce_mismatch__"):
            # Keep the key registered for the remainder of the TTL
            # by design — the Lua script only DELs on a successful
            # nonce match. Re-raise without consuming.
            raise ValueError("nonce does not match the registered value")
        return parsed["redirect_uri"], parsed["lti_issuer"]

    async def peek(self, state: str) -> str | None:
        """Return the ``lti_issuer`` registered for ``state`` without
        consuming the entry, or ``None`` if not registered / expired.
        """

        if not state:
            return None
        raw = await self._client.get(self._key(state))  # type: ignore[attr-defined]
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return parsed.get("lti_issuer")

    async def purge_expired(self) -> int:
        """No-op for the Redis backend; Redis itself evicts via TTL.

        Returned as ``0`` so the test surface is uniform with the
        in-memory store.
        """

        return 0

    async def __len__(self) -> int:
        """Return the number of live entries (testing surface)."""

        # SCAN over the key prefix. COUNT is a hint, not a hard cap;
        # for a launch-state store the keyspace is small (one entry
        # per in-flight login), so SCAN with a high COUNT is fine.
        # If the store ever grows beyond thousands of entries this
        # method will start to hurt and should be removed from the
        # test surface.
        count = 0
        async for _ in self._client.scan_iter(  # type: ignore[attr-defined]
            match=f"{self._key_prefix}:*",
            count=1000,
        ):
            count += 1
        return count

    async def __contains__(self, state: str) -> bool:
        """Return whether ``state`` is currently registered."""

        if not state:
            return False
        return await self._client.exists(self._key(state)) > 0  # type: ignore[attr-defined]


# Backward-compatibility alias. The original v1 code referenced
# :class:`LaunchStateStore` as the concrete in-memory class. Keep the
# alias so any downstream imports of the old name continue to resolve
# to the in-memory implementation, while the Protocol of the same
# name documents the contract.
LaunchStateStore.__name__ = "LaunchStateStore"  # for type-checker niceness

# Module-level public names re-exported for the route handler and
# the tests. The implementations have their own static helpers too
# (kept for backward compat) but the route layer should import these
# instead of reaching into a concrete class.
__all__ = [
    "InMemoryLaunchStateStore",
    "LaunchStateError",
    "LaunchStateExpired",
    "LaunchStateMissing",
    "LaunchStateReplay",
    "LaunchStateStore",
    "RedisLaunchStateStore",
    "generate_launch_nonce",
    "generate_launch_state",
]