"""
Meta harness — cross-cutting infrastructure for agent calls.

Every agent in this codebase shares the same shape: async methods that take
typed Pydantic input and return typed Pydantic output, may shell out to tools,
and may raise. The Harness wraps any such agent so we get a single place to
plug in:

    - structured logging (entry/exit/duration)
    - invocation log (kept in memory; persisted onto ExperimentRecord)
    - duration capture for budget accounting
    - error capture (raised through, never swallowed)
    - future hooks: token spend, retry, circuit-breaking

Design contract:
    1. The wrapped agent must satisfy the same Protocol as the inner one.
       Drop-in. No method-list maintenance.
    2. Errors propagate. The harness records the error but never hides it.
    3. The harness keeps invocations in insertion order. Tests + the orchestrator
       both read this list to assemble an audit trail.
    4. Sync attribute / method access (constructor, properties) is not wrapped —
       we only instrument coroutine methods.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class AgentInvocation:
    """One coroutine call through the harness. Persisted to the experiment record."""

    agent: str
    method: str
    started_at_ms: int
    finished_at_ms: int | None = None
    duration_ms: float | None = None
    ok: bool = False
    error: str | None = None
    input_summary: str = ""
    output_summary: str = ""
    spend_usd: float | None = None


# Per-task slot pointing at the currently-running AgentInvocation. ``_llm`` reads
# this to attribute LLM cost to the invocation that triggered the call, without
# threading a harness reference through every strategy constructor.
_current_invocation: ContextVar[AgentInvocation | None] = ContextVar(
    "_current_invocation", default=None
)


def record_llm_spend(usd: float) -> None:
    """Add ``usd`` to the current invocation's cumulative spend.

    Called from ``agents._llm.complete_with_tools`` after each completion.
    No-op when there's no current invocation (e.g., tests or direct CLI use).
    """
    if usd <= 0:
        return
    inv = _current_invocation.get()
    if inv is None:
        return
    inv.spend_usd = (inv.spend_usd or 0.0) + usd


@dataclass
class Harness:
    """Holds the invocation log + emits structured logs."""

    invocations: list[AgentInvocation] = field(default_factory=list)

    def instrument(self, name: str, agent: Any) -> _Wrapped:
        """Return a proxy that records every coroutine call to `agent`."""
        return _Wrapped(name=name, inner=agent, harness=self)

    def record(self, inv: AgentInvocation) -> None:
        self.invocations.append(inv)
        if inv.ok:
            log.info(
                "agent_call_ok agent=%s method=%s duration_ms=%.1f input=%s",
                inv.agent,
                inv.method,
                inv.duration_ms or 0.0,
                inv.input_summary,
            )
        else:
            log.warning(
                "agent_call_error agent=%s method=%s duration_ms=%.1f error=%s",
                inv.agent,
                inv.method,
                inv.duration_ms or 0.0,
                inv.error,
            )


class _Wrapped:
    """Proxy that captures every async method call to its inner agent.

    Synchronous attribute access passes through unchanged so callers can still
    introspect, e.g., `wrapped.model` -> the inner agent's model field.

    We resolve a coroutine vs. non-coroutine target at __getattr__ time and only
    wrap when it's a coroutine — keeping the proxy invisible for sync access.
    """

    def __init__(self, *, name: str, inner: Any, harness: Harness) -> None:
        # Stored as underscored to avoid name clashes with any inner attr.
        self.__dict__["_h_name"] = name
        self.__dict__["_h_inner"] = inner
        self.__dict__["_h_harness"] = harness

    def __getattr__(self, item: str) -> Any:
        inner = self.__dict__["_h_inner"]
        target = getattr(inner, item)
        if not asyncio.iscoroutinefunction(target):
            return target
        name = self.__dict__["_h_name"]
        harness: Harness = self.__dict__["_h_harness"]
        return _wrap_coroutine(name, item, target, harness)

    # Block accidental writes — wrapped agents are conceptually frozen views.
    def __setattr__(self, key: str, value: Any) -> None:
        raise AttributeError(f"cannot assign {key!r} on a wrapped agent (read-only proxy)")


def _wrap_coroutine(
    agent: str, method: str, target: Any, harness: Harness
) -> Any:
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        start_ms = time.time() * 1000
        inv = AgentInvocation(
            agent=agent,
            method=method,
            started_at_ms=int(start_ms),
            input_summary=_summarize_inputs(args, kwargs),
        )
        token = _current_invocation.set(inv)
        try:
            result = await target(*args, **kwargs)
            inv.ok = True
            inv.output_summary = _summarize_output(result)
            return result
        except Exception as e:
            inv.error = repr(e)
            raise
        finally:
            end_ms = time.time() * 1000
            inv.finished_at_ms = int(end_ms)
            inv.duration_ms = end_ms - start_ms
            _current_invocation.reset(token)
            harness.record(inv)

    return wrapped


def _summarize_inputs(args: tuple, kwargs: dict) -> str:
    """One-liner describing the call. Prefers `experiment_id` on Pydantic args."""
    parts: list[str] = []
    for a in args:
        eid = getattr(a, "experiment_id", None)
        if isinstance(eid, str):
            parts.append(f"experiment_id={eid}")
        kind = getattr(a, "kind", None) or getattr(a, "request_kind", None)
        if isinstance(kind, str):
            parts.append(f"kind={kind}")
    for k, v in kwargs.items():
        eid = getattr(v, "experiment_id", None)
        if isinstance(eid, str):
            parts.append(f"{k}.experiment_id={eid}")
    return " ".join(parts)


def _summarize_output(result: Any) -> str:
    """One-liner describing the return value. Pulls a few salient Pydantic fields."""
    parts: list[str] = []
    for attr in ("steady_state", "success", "action", "state"):
        val = getattr(result, attr, None)
        if val is not None:
            parts.append(f"{attr}={val!r}")
    for attr in ("findings", "hypotheses", "events"):
        seq = getattr(result, attr, None)
        if seq is not None:
            parts.append(f"{attr}_count={len(seq)}")
    return " ".join(parts)
