"""
Cognitive surface of the fixer.

The Strategy decides what code/config to change for a given diagnosis. The agent
wraps Strategy output with deterministic safety checks (denylist) and produces
the final FixProposal.

Implementations:
    - FixtureFixerStrategy: returns predetermined output. Used by tests and dry-run.
    - ClaudeFixerStrategy: real LLM via Claude Agent SDK. Stubbed in M6.0c
      (NotImplementedError), wired in M6.x.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from shared.contracts import DiagnosisReport, FixAction


@dataclass(frozen=True)
class FixerOutput:
    """Raw strategy output. Agent assembles this into a FixProposal."""

    reasoning: str
    files_touched: list[str] = field(default_factory=list)
    regression_test_added: bool = False
    pr_url: str | None = None


class FixerStrategy(Protocol):
    """Given a diagnosis and the intended action, produce a fix."""

    async def propose(
        self,
        *,
        diagnosis: DiagnosisReport,
        intended_action: FixAction,
    ) -> FixerOutput: ...


# ---------------------------------------------------------------------------- #
# Fixture (tests + dry-run)                                                    #
# ---------------------------------------------------------------------------- #


FixtureFn = Callable[[DiagnosisReport, FixAction], Awaitable[FixerOutput]]


class FixtureFixerStrategy:
    """Returns a fixed FixerOutput, or runs an async callback."""

    def __init__(self, output: FixerOutput | FixtureFn) -> None:
        self._o = output

    async def propose(
        self, *, diagnosis: DiagnosisReport, intended_action: FixAction
    ) -> FixerOutput:
        if callable(self._o):
            return await self._o(diagnosis, intended_action)
        return self._o


# ---------------------------------------------------------------------------- #
# Claude-backed (M6.x)                                                         #
# ---------------------------------------------------------------------------- #


class ClaudeFixerStrategy:
    """Real LLM implementation. Stub: implemented in M6.x."""

    def __init__(self, *, model: str = "claude-opus-4-7") -> None:
        self.model = model

    async def propose(
        self, *, diagnosis: DiagnosisReport, intended_action: FixAction
    ) -> FixerOutput:
        raise NotImplementedError(
            "ClaudeFixerStrategy is a milestone-6.x task; use FixtureFixerStrategy or "
            "supply your own implementation."
        )
