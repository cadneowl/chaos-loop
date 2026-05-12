"""
Small async retry helper.

We have multiple HTTP-backed backends (Loki, Prometheus, future scanners) that
all want the same behavior: retry on transient errors with exponential backoff,
give up after a few attempts.

Adding a tenacity dep just for this would be overkill — this file is ~30 lines.

Usage:

    from agents._retry import async_retry

    async def _do_request():
        return await client.get(url)

    result = await async_retry(
        _do_request,
        max_attempts=3,
        backoff_base=0.2,
        retry_on=(httpx.TransportError, httpx.TimeoutException),
    )

Non-matching exceptions are re-raised immediately (don't retry on 4xx).
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")

log = logging.getLogger(__name__)


async def async_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    backoff_base: float = 0.2,
    backoff_cap: float = 5.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    jitter: float = 0.1,
) -> T:
    """Call ``fn`` up to ``max_attempts`` times with exponential backoff.

    Retries only when the raised exception is an instance of any class in
    ``retry_on``. Other exceptions propagate immediately. After all attempts
    fail, the last exception is re-raised.

    Backoff: ``min(backoff_cap, backoff_base * 2**attempt) + uniform(0, jitter)``.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return await fn()
        except retry_on as e:
            last_exc = e
            if attempt + 1 >= max_attempts:
                break
            delay = min(backoff_cap, backoff_base * (2**attempt))
            if jitter > 0:
                delay += random.uniform(0, jitter)
            log.debug(
                "async_retry: attempt %d/%d failed with %s; sleeping %.2fs",
                attempt + 1, max_attempts, type(e).__name__, delay,
            )
            await asyncio.sleep(delay)
    # All attempts exhausted; re-raise the last exception preserving the chain.
    assert last_exc is not None  # narrowing for type checkers
    raise last_exc
