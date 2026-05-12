"""Tests for agents._retry.async_retry."""

from __future__ import annotations

import asyncio

import pytest

from agents._retry import async_retry


def test_succeeds_first_try() -> None:
    calls = 0

    async def ok() -> str:
        nonlocal calls
        calls += 1
        return "result"

    out = asyncio.run(async_retry(ok))
    assert out == "result"
    assert calls == 1


def test_retries_then_succeeds() -> None:
    """First two attempts raise; third succeeds."""
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError("transient")
        return "ok"

    out = asyncio.run(
        async_retry(flaky, max_attempts=3, backoff_base=0.0, jitter=0.0)
    )
    assert out == "ok"
    assert calls == 3


def test_exhausts_attempts_and_raises_last_error() -> None:
    calls = 0

    async def always_fails() -> str:
        nonlocal calls
        calls += 1
        raise ConnectionError(f"attempt {calls}")

    with pytest.raises(ConnectionError, match="attempt 3"):
        asyncio.run(
            async_retry(always_fails, max_attempts=3, backoff_base=0.0, jitter=0.0)
        )
    assert calls == 3


def test_does_not_retry_on_non_matching_exception() -> None:
    """ValueError isn't in retry_on -> propagate immediately."""
    calls = 0

    async def bad_input() -> str:
        nonlocal calls
        calls += 1
        raise ValueError("not transient")

    with pytest.raises(ValueError, match="not transient"):
        asyncio.run(
            async_retry(
                bad_input,
                max_attempts=3,
                retry_on=(ConnectionError,),
                backoff_base=0.0,
                jitter=0.0,
            )
        )
    assert calls == 1  # only one attempt


def test_max_attempts_one_means_no_retry() -> None:
    calls = 0

    async def always_fails() -> str:
        nonlocal calls
        calls += 1
        raise ConnectionError("boom")

    with pytest.raises(ConnectionError):
        asyncio.run(async_retry(always_fails, max_attempts=1, jitter=0.0))
    assert calls == 1


def test_max_attempts_zero_raises_value_error() -> None:
    async def noop() -> str:
        return "x"

    with pytest.raises(ValueError, match="max_attempts"):
        asyncio.run(async_retry(noop, max_attempts=0))


def test_backoff_increases_per_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify exponential schedule (jitter off to make this deterministic)."""
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr("agents._retry.asyncio.sleep", fake_sleep)

    async def always_fails() -> str:
        raise ConnectionError("nope")

    with pytest.raises(ConnectionError):
        asyncio.run(
            async_retry(
                always_fails,
                max_attempts=4,
                backoff_base=0.1,
                backoff_cap=10.0,
                jitter=0.0,
            )
        )
    # Sleeps between attempts: 0.1, 0.2, 0.4 (no sleep after the final attempt).
    assert sleeps == [0.1, 0.2, 0.4]


def test_backoff_cap_clamps_long_delays(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr("agents._retry.asyncio.sleep", fake_sleep)

    async def always_fails() -> str:
        raise ConnectionError("nope")

    with pytest.raises(ConnectionError):
        asyncio.run(
            async_retry(
                always_fails,
                max_attempts=6,
                backoff_base=1.0,
                backoff_cap=2.0,
                jitter=0.0,
            )
        )
    # 1, 2 (capped), 2, 2, 2
    assert sleeps == [1.0, 2.0, 2.0, 2.0, 2.0]
