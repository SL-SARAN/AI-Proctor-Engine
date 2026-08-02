"""Unit tests for the in-memory LTI launch-state store.

Boundary cases (per ``docs/08-test-strategy-design.md`` §"Ingestion
layer"): replay protection, expiration, missing state, and the
``nonce`` mismatch that signals a confused-deputy attack.
"""

from __future__ import annotations

import threading

import pytest

from proctoring_engine.lti.state import (
    InMemoryLaunchStateStore,
    LaunchStateExpired,
    LaunchStateMissing,
    LaunchStateReplay,  # noqa: F401  (imported for the smoke check)
)


def _fake_clock(start: float = 1_000.0):
    """Return a monotonic-style clock and a setter for advancing it."""

    value = [start]

    def clock() -> float:
        return value[0]

    def advance(seconds: float) -> None:
        value[0] += seconds

    return clock, advance


async def test_register_and_consume_returns_redirect_uri() -> None:
    """A successful register → consume round-trips the redirect URI."""

    store = InMemoryLaunchStateStore(ttl_seconds=60)
    state = InMemoryLaunchStateStore.new_state()
    nonce = InMemoryLaunchStateStore.new_nonce()
    await store.register(
        state,
        nonce,
        redirect_uri="http://localhost:8000/lti/launch",
        lti_issuer="https://lms.example.edu",
    )
    assert await store.__contains__(state)
    redirect_uri, lti_issuer = await store.consume(state, nonce)
    assert redirect_uri == "http://localhost:8000/lti/launch"
    assert lti_issuer == "https://lms.example.edu"
    assert not await store.__contains__(state)


async def test_consume_is_one_shot() -> None:
    """A second ``consume`` for the same state fails closed."""

    store = InMemoryLaunchStateStore(ttl_seconds=60)
    state = InMemoryLaunchStateStore.new_state()
    nonce = InMemoryLaunchStateStore.new_nonce()
    await store.register(state, nonce, redirect_uri="x", lti_issuer="y")

    await store.consume(state, nonce)

    with pytest.raises(LaunchStateMissing):
        await store.consume(state, nonce)


async def test_consume_unknown_state_raises_missing() -> None:
    """An unrecognised state value is reported as ``Missing``."""

    store = InMemoryLaunchStateStore(ttl_seconds=60)
    with pytest.raises(LaunchStateMissing):
        await store.consume("never-registered", InMemoryLaunchStateStore.new_nonce())


async def test_consume_after_ttl_raises_expired() -> None:
    """An entry past the TTL is reported as ``Expired``, not as a
    successful consume (the registered value is purged)."""

    clock, advance = _fake_clock()
    store = InMemoryLaunchStateStore(ttl_seconds=60, clock=clock)
    state = InMemoryLaunchStateStore.new_state()
    nonce = InMemoryLaunchStateStore.new_nonce()
    await store.register(state, nonce, redirect_uri="x", lti_issuer="y")

    advance(61)
    with pytest.raises(LaunchStateExpired):
        await store.consume(state, nonce)
    # The entry must be purged so it cannot reappear with a
    # backwards clock.
    assert not await store.__contains__(state)


async def test_consume_with_wrong_nonce_raises() -> None:
    """A nonce that does not match the registered value is rejected."""

    store = InMemoryLaunchStateStore(ttl_seconds=60)
    state = InMemoryLaunchStateStore.new_state()
    await store.register(
        state,
        InMemoryLaunchStateStore.new_nonce(),
        redirect_uri="x",
        lti_issuer="y",
    )
    with pytest.raises(ValueError):
        await store.consume(state, InMemoryLaunchStateStore.new_nonce())


async def test_register_overwrites_existing_state() -> None:
    """Re-registration is permitted (the security guarantee is on
    consume, not on register)."""

    store = InMemoryLaunchStateStore(ttl_seconds=60)
    state = InMemoryLaunchStateStore.new_state()
    await store.register(state, InMemoryLaunchStateStore.new_nonce(), redirect_uri="first", lti_issuer="y")
    new_nonce = InMemoryLaunchStateStore.new_nonce()
    await store.register(state, new_nonce, redirect_uri="second", lti_issuer="y")
    redirect_uri, _ = await store.consume(state, new_nonce)
    assert redirect_uri == "second"


async def test_purge_expired_removes_stale_entries() -> None:
    """The explicit purge evicts expired entries and reports the count.

    Note: ``__len__`` is itself a purge operation (the store keeps
    the dict trim on every observation). This test pins the contract
    of the explicit ``purge_expired`` method by counting the
    returned removal count and confirming the store is empty after.
    """

    clock, advance = _fake_clock()
    store = InMemoryLaunchStateStore(ttl_seconds=60, clock=clock)
    for _ in range(3):
        await store.register(
            InMemoryLaunchStateStore.new_state(),
            InMemoryLaunchStateStore.new_nonce(),
            redirect_uri="x",
            lti_issuer="y",
        )
    advance(61)
    removed = await store.purge_expired()
    assert removed == 3
    assert await store.__len__() == 0


async def test_register_rejects_empty_state_or_nonce() -> None:
    """Defensive: the store rejects empty inputs at register time."""

    store = InMemoryLaunchStateStore(ttl_seconds=60)
    with pytest.raises(ValueError):
        await store.register("", InMemoryLaunchStateStore.new_nonce(), redirect_uri="x", lti_issuer="y")
    with pytest.raises(ValueError):
        await store.register(InMemoryLaunchStateStore.new_state(), "", redirect_uri="x", lti_issuer="y")


async def test_store_is_thread_safe() -> None:
    """Concurrent register + consume calls do not race."""

    store = InMemoryLaunchStateStore(ttl_seconds=60)
    state = InMemoryLaunchStateStore.new_state()
    nonce = InMemoryLaunchStateStore.new_nonce()
    await store.register(state, nonce, redirect_uri="x", lti_issuer="y")

    results: list[object] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(16)

    def attempt() -> None:
        # Wait until all threads have entered the barrier so they
        # race as tightly as possible.
        barrier.wait()
        try:
            results.append(_run(store.consume(state, nonce)))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Exactly one of the attempts succeeds; the rest see the state
    # as missing.
    assert len(results) == 1
    assert len(errors) == 15
    assert all(isinstance(err, LaunchStateMissing) for err in errors)


def _run(coro):
    """Drive a coroutine to completion synchronously from a worker thread.

    ``consume`` is an ``async def`` because the route handler must
    ``await`` it uniformly across the in-memory and Redis-backed
    implementations. The in-memory implementation does no I/O, so
    the coroutine completes immediately; we use the same primitive
    in the threaded race test so the call shape matches the route
    handler's.
    """

    import asyncio

    return asyncio.run(coro)


def test_new_state_and_nonce_are_unique() -> None:
    """``new_state`` and ``new_nonce`` produce fresh tokens each call."""

    states = {InMemoryLaunchStateStore.new_state() for _ in range(64)}
    nonces = {InMemoryLaunchStateStore.new_nonce() for _ in range(64)}
    assert len(states) == 64
    assert len(nonces) == 64


def test_constructor_rejects_non_positive_ttl() -> None:
    """The constructor validates the TTL argument."""

    with pytest.raises(ValueError):
        InMemoryLaunchStateStore(ttl_seconds=0)
    with pytest.raises(ValueError):
        InMemoryLaunchStateStore(ttl_seconds=-1)