"""
The cognitive surface of the diagnostician.

The Diagnoser Protocol exists so the agent can be tested with a deterministic
fixture, and later swapped to a Claude-Agent-SDK-backed implementation without
the surrounding wiring changing.

Implementations:
    - FixtureDiagnoser: returns predetermined hypotheses (or runs a callback);
      used by tests and the mock-loop dry-run.
    - ClaudeDiagnoser: real LLM via Claude Agent SDK. Lives behind a feature gate;
      not implemented in M5.1 (M5.x).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from agents.diagnostician.tools.code_reader import TargetCodeReader
from agents.diagnostician.tools.loki import LokiBackend
from agents.tester.tools.prometheus import PromBackend
from shared.contracts import DiagnosisRequest, RootCauseHypothesis


class Diagnoser(Protocol):
    """Produces ranked root-cause hypotheses for a DiagnosisRequest."""

    async def diagnose(
        self,
        *,
        request: DiagnosisRequest,
        loki: LokiBackend | None = None,
        prom: PromBackend | None = None,
        code: TargetCodeReader | None = None,
    ) -> list[RootCauseHypothesis]: ...


# ---------------------------------------------------------------------------- #
# Fixture (tests + dry-run)                                                    #
# ---------------------------------------------------------------------------- #


FixtureFn = Callable[[DiagnosisRequest], Awaitable[list[RootCauseHypothesis]]]


class FixtureDiagnoser:
    """Returns a static list of hypotheses, or runs a caller-supplied async function."""

    def __init__(
        self,
        hypotheses: list[RootCauseHypothesis] | FixtureFn,
    ) -> None:
        self._h = hypotheses

    async def diagnose(
        self,
        *,
        request: DiagnosisRequest,
        loki: LokiBackend | None = None,
        prom: PromBackend | None = None,
        code: TargetCodeReader | None = None,
    ) -> list[RootCauseHypothesis]:
        if callable(self._h):
            return await self._h(request)
        return list(self._h)


# ---------------------------------------------------------------------------- #
# Claude-backed (placeholder; M5.x)                                            #
# ---------------------------------------------------------------------------- #


class ClaudeDiagnoser:
    """Real LLM implementation. Stub: implemented in M5.x once we wire Claude Agent SDK."""

    def __init__(self, *, model: str = "claude-opus-4-7") -> None:
        self.model = model

    async def diagnose(
        self,
        *,
        request: DiagnosisRequest,
        loki: LokiBackend | None = None,
        prom: PromBackend | None = None,
        code: TargetCodeReader | None = None,
    ) -> list[RootCauseHypothesis]:
        raise NotImplementedError(
            "ClaudeDiagnoser is a milestone-5.x task; for now use FixtureDiagnoser "
            "or supply your own Diagnoser implementation."
        )
