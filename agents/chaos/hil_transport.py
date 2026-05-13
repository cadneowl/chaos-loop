"""
Transports HilHardwareIO depends on, plus in-memory fakes for tests.

Two Protocols:
    SerialTransport — async, line-oriented bidirectional channel to the
                      attack-ESP32 over USB serial. Outgoing JSON commands,
                      incoming JSON responses, one per line.
    HttpTransport   — async, single-method GET that reads the DUT's
                      telemetry endpoint (existing NeoOwl firmware
                      already exposes one).

Both have concrete implementations (`PySerialTransport`, `HttpxTransport`)
and fakes (`FakeSerialTransport`, `FakeHttpTransport`) so the loop and
HilHardwareIO can be tested without a bench plugged in. Real hardware
swaps in the concrete implementations via the constructor.

Keeping transports separate from `hardware_io.py` lets us swap them
independently — e.g. replace serial with TCP later — without touching
the agent.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

# ---------------------------------------------------------------------- serial


class SerialTransport(Protocol):
    """Line-oriented async serial channel.

    The wire protocol is "one JSON object per line" in both directions.
    Implementations buffer partial lines internally; callers always get
    complete objects from `recv_line`.
    """

    async def send_line(self, line: str) -> None: ...
    """Write `line` plus a newline. Awaits until the OS buffer accepts it."""

    async def recv_line(self, *, timeout_s: float = 5.0) -> str: ...
    """Read one line (sans trailing newline). Raises `TimeoutError` on no data."""

    async def close(self) -> None: ...
    """Idempotent — repeated close is a no-op."""


@dataclass
class FakeSerialTransport:
    """In-memory two-queue serial used in tests.

    Pre-load `recv_queue` with the JSON responses the simulated attack
    device would reply with. Inspect `sent` to assert what the agent
    wrote. Use `respond_to` to install a request→response mapping when
    the test only cares about pairing, not ordering.
    """

    sent: list[str] = field(default_factory=list)
    recv_queue: list[str] = field(default_factory=list)
    # Optional dynamic responder: if set, sending a line resolves the
    # responder and pushes its output into recv_queue.
    responder: Callable[[str], Awaitable[str]] | None = None
    _closed: bool = False

    async def send_line(self, line: str) -> None:
        if self._closed:
            raise RuntimeError("transport closed")
        self.sent.append(line)
        if self.responder is not None:
            reply = await self.responder(line)
            self.recv_queue.append(reply)

    async def recv_line(self, *, timeout_s: float = 5.0) -> str:
        # `timeout_s` is honored by polling at a fine grain so tests don't
        # have to keep wall-clock time in sync.
        elapsed = 0.0
        step = 0.001
        while not self.recv_queue:
            if self._closed:
                raise RuntimeError("transport closed before reply arrived")
            if elapsed >= timeout_s:
                raise TimeoutError("FakeSerialTransport: no reply within timeout")
            await asyncio.sleep(step)
            elapsed += step
        return self.recv_queue.pop(0)

    async def close(self) -> None:
        self._closed = True


# ---------------------------------------------------------------------- http


class HttpTransport(Protocol):
    """Minimal HTTP GET used to read the DUT's telemetry endpoint.

    Just enough surface to read JSON; everything else (retry, auth,
    backoff) is the implementation's problem.
    """

    async def get_json(self, url: str, *, timeout_s: float = 5.0) -> Any: ...


@dataclass
class FakeHttpTransport:
    """In-memory HTTP responder.

    `responses` maps URL → static JSON payload, or
    `responder` is a callable that builds the response per request.
    """

    responses: dict[str, Any] = field(default_factory=dict)
    responder: Callable[[str], Awaitable[Any]] | None = None
    request_log: list[str] = field(default_factory=list)

    async def get_json(self, url: str, *, timeout_s: float = 5.0) -> Any:
        self.request_log.append(url)
        if self.responder is not None:
            return await self.responder(url)
        if url not in self.responses:
            raise KeyError(f"FakeHttpTransport has no response for {url!r}")
        # asyncio.sleep(0) yields so concurrent code sees us as truly async.
        await asyncio.sleep(0)
        return self.responses[url]


# ---------------------------------------------------------------------- json helpers


def encode(obj: Any) -> str:
    """JSON-encode without a trailing newline; the transport adds it."""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


def decode(line: str) -> Any:
    """JSON-decode one line, raising a clear error on garbage."""
    line = line.strip()
    if not line:
        raise ValueError("empty line from transport")
    return json.loads(line)
