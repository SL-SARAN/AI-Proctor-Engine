"""Unit tests for the Redis-backed LTI launch-state store.

Two surfaces are exercised:

1. The :class:`RedisLaunchStateStore` against a ``FakeAsyncRedis``
   mock (from ``fakeredis[lua]``). Lua scripts work in the mock
   with the ``[lua]`` extra; verified empirically before this test
   was written.
2. A two-client "replica" scenario: two ``RedisLaunchStateStore``
   instances backed by the same :class:`FakeAsyncRedis` mock,
   demonstrating the cross-replica visibility that the in-memory
   store cannot provide.

Boundary cases (per ``docs/08-test-strategy-design.md`` §"Ingestion
layer") are mirrored from :mod:`tests.test_lti_state`: replay
protection, expiration, missing state, and the ``nonce`` mismatch
that signals a confused-deputy attack.
"""

from __future__ import annotations

import asyncio

import fakeredis
import pytest

from proctoring_engine.lti.state import (
    LaunchStateMissing,
    RedisLaunchStateStore,
    generate_launch_nonce,
    generate_launch_state,
)


# All tests are async because the Redis client surface is async.
# ``pytest-asyncio`` with ``asyncio_mode = "auto"`` (set in
# ``pyproject.toml``) dispatches async test functions without an
# explicit ``@pytest.mark.asyncio`` decorator.


def _make_store(
    shared_server: fakeredis.FakeServer | None = None,
    *,
    ttl_seconds: int = 60,
    key_prefix: str = "proctoring:lti:state",
) -> tuple[RedisLaunchStateStore, fakeredis.FakeAsyncRedis]:
    """Build a :class:`RedisLaunchStateStore` over a fake async client.

    Two stores constructed with the same ``shared_server`` see each
    other's writes — this is the property under test for the
    cross-replica scenario.
    """

    client = fakeredis.FakeAsyncRedis(server=shared_server)
    store = RedisLaunchStateStore(
        client=client,
        ttl_seconds=ttl_seconds,
        key_prefix=key_prefix,
    )
    return store, client


async def test_register_then_consume_round_trip() -> None:
    """The happy path: register, then consume returns the recorded values."""

    store, _ = _make_store()
    state = generate_launch_state()
    nonce = generate_launch_nonce()
    await store.register(
        state,
        nonce,
        redirect_uri="http://localhost:8000/lti/launch",
        lti_issuer="https://lms.example.edu",
    )
    redirect_uri, lti_issuer = await store.consume(state, nonce)
    assert redirect_uri == "http://localhost:8000/lti/launch"
    assert lti_issuer == "https://lms.example.edu"


async def test_consume_is_one_shot() -> None:
    """A second consume for the same state raises ``Missing``."""

    store, _ = _make_store()
    state = generate_launch_state()
    nonce = generate_launch_nonce()
    await store.register(state, nonce, redirect_uri="x", lti_issuer="y")

    await store.consume(state, nonce)
    with pytest.raises(LaunchStateMissing):
        await store.consume(state, nonce)


async def test_consume_unknown_state_raises_missing() -> None:
    """An unrecognised state value is reported as ``Missing``."""

    store, _ = _make_store()
    with pytest.raises(LaunchStateMissing):
        await store.consume("never-registered", generate_launch_nonce())


async def test_consume_with_wrong_nonce_raises() -> None:
    """A nonce that does not match keeps the entry registered for retry."""

    store, _ = _make_store()
    state = generate_launch_state()
    await store.register(
        state,
        generate_launch_nonce(),
        redirect_uri="x",
        lti_issuer="y",
    )
    # The entry must still be present after a nonce-mismatch — the
    # platform may retry with the correct nonce within the TTL.
    with pytest.raises(ValueError):
        await store.consume(state, generate_launch_nonce())
    # ``__contains__`` is async on the Redis store; await it.
    assert await store.__contains__(state)


async def test_register_overwrites_existing_state() -> None:
    """Re-registration is permitted; the second call wins."""

    store, _ = _make_store()
    state = generate_launch_state()
    await store.register(state, generate_launch_nonce(), redirect_uri="first", lti_issuer="y")
    new_nonce = generate_launch_nonce()
    await store.register(state, new_nonce, redirect_uri="second", lti_issuer="y")
    redirect_uri, _ = await store.consume(state, new_nonce)
    assert redirect_uri == "second"


async def test_peek_returns_issuer_without_consuming() -> None:
    """``peek`` is non-destructive."""

    store, _ = _make_store()
    state = generate_launch_state()
    nonce = generate_launch_nonce()
    await store.register(state, nonce, redirect_uri="x", lti_issuer="https://lms.example.edu")
    assert await store.peek(state) == "https://lms.example.edu"
    # ``peek`` must not consume — a subsequent ``consume`` succeeds.
    redirect_uri, _ = await store.consume(state, nonce)
    assert redirect_uri == "x"


async def test_peek_missing_state_returns_none() -> None:
    """``peek`` of an unrecognised state returns ``None`` (not an error)."""

    store, _ = _make_store()
    assert await store.peek("never-registered") is None


async def test_redis_ttl_expires_entries() -> None:
    """An entry whose Redis-side TTL has passed is reported as ``Missing``.

    We simulate the passage of TTL by directly issuing a ``PEXPIRE``
    with a small value; ``fakeredis`` honours real Redis TTL
    semantics so the key disappears once the deadline elapses.
    """

    server = fakeredis.FakeServer()
    store, client = _make_store(shared_server=server, ttl_seconds=60)
    state = generate_launch_state()
    nonce = generate_launch_nonce()
    await store.register(state, nonce, redirect_uri="x", lti_issuer="y")

    # Shrink the remaining TTL to 1 ms.
    await client.pexpire(f"{store._key_prefix}:{state}", 1)
    # ``fakeredis`` evicts lazily on the next access; nudge it.
    await asyncio.sleep(0.05)

    with pytest.raises(LaunchStateMissing):
        await store.consume(state, nonce)


async def test_purge_expired_is_noop_for_redis() -> None:
    """The Redis backend has nothing to purge (Redis evicts via TTL)."""

    store, _ = _make_store()
    removed = await store.purge_expired()
    assert removed == 0


async def test_register_rejects_empty_state_or_nonce() -> None:
    """Defensive: the store rejects empty inputs at register time."""

    store, _ = _make_store()
    with pytest.raises(ValueError):
        await store.register("", generate_launch_nonce(), redirect_uri="x", lti_issuer="y")
    with pytest.raises(ValueError):
        await store.register(generate_launch_state(), "", redirect_uri="x", lti_issuer="y")


async def test_constructor_rejects_non_positive_ttl() -> None:
    """The constructor validates the TTL argument."""

    client = fakeredis.FakeAsyncRedis()
    with pytest.raises(ValueError):
        RedisLaunchStateStore(client=client, ttl_seconds=0)
    with pytest.raises(ValueError):
        RedisLaunchStateStore(client=client, ttl_seconds=-1)


async def test_constructor_rejects_empty_key_prefix() -> None:
    """A non-empty key prefix is required for namespace isolation."""

    client = fakeredis.FakeAsyncRedis()
    with pytest.raises(ValueError):
        RedisLaunchStateStore(client=client, ttl_seconds=60, key_prefix="")


# ----- cross-replica scenario ----------------------------------------


async def test_two_stores_share_state_via_fake_server() -> None:
    """The whole point of the Redis backend: replica A's state is
    visible to replica B.

    The two ``RedisLaunchStateStore`` instances are constructed
    against a shared ``FakeServer``; writes from the first are
    readable from the second. This is the exact property that the
    in-memory store cannot provide, and the bug this turn fixes
    (per ``docs/DEPLOYMENT.md`` §6.2).
    """

    server = fakeredis.FakeServer()
    store_a, _ = _make_store(shared_server=server, key_prefix="proctoring:lti:state")
    store_b, _ = _make_store(shared_server=server, key_prefix="proctoring:lti:state")

    state = generate_launch_state()
    nonce = generate_launch_nonce()
    await store_a.register(
        state,
        nonce,
        redirect_uri="http://localhost:8000/lti/launch",
        lti_issuer="https://lms.example.edu",
    )

    # Replica B sees the state registered by replica A.
    assert await store_b.peek(state) == "https://lms.example.edu"
    redirect_uri, lti_issuer = await store_b.consume(state, nonce)
    assert redirect_uri == "http://localhost:8000/lti/launch"
    assert lti_issuer == "https://lms.example.edu"

    # Replica A sees the consume too (the key is gone from both).
    assert not await store_a.__contains__(state)
    assert not await store_b.__contains__(state)


async def test_concurrent_consume_runs_through_lua_atomicity() -> None:
    """The Lua script guarantees at-most-one winner under contention.

    Multiple concurrent ``consume`` calls on the same state via
    the same :class:`RedisLaunchStateStore` must result in exactly
    one success and the rest raising :class:`LaunchStateMissing`.
    The atomicity is the Lua-script side; this test is the
    regression guard against a future refactor that drops it.
    """

    store, _ = _make_store()
    state = generate_launch_state()
    nonce = generate_launch_nonce()
    await store.register(state, nonce, redirect_uri="x", lti_issuer="y")

    attempts = 16

    async def attempt() -> tuple[bool, BaseException | None]:
        try:
            await store.consume(state, nonce)
            return True, None
        except BaseException as exc:  # noqa: BLE001
            return False, exc

    results = await asyncio.gather(*[attempt() for _ in range(attempts)])
    successes = [ok for ok, _ in results]
    failures = [exc for ok, exc in results if not ok]
    assert successes.count(True) == 1
    assert all(isinstance(exc, LaunchStateMissing) for exc in failures)