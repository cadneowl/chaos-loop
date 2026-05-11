"""
The cognitive surface of the diagnostician.

The Diagnoser Protocol exists so the agent can be tested with a deterministic
fixture, and switched to a Claude-Agent-SDK-backed implementation without the
surrounding wiring changing.

Implementations:
    - FixtureDiagnoser: returns predetermined hypotheses (or runs a callback).
      Used by tests and the mock-loop dry-run.
    - ClaudeDiagnoser: real LLM via Claude Agent SDK with MCP tools for log /
      metric / code reading. Costs real money to invoke (Anthropic API).
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol, get_args

from agents.diagnostician.tools.code_reader import CodeReadError, TargetCodeReader
from agents.diagnostician.tools.loki import LokiBackend, LokiQueryError
from agents.tester.tools.prometheus import PromBackend, PromQueryError
from shared.contracts import DiagnosisRequest, RootCauseHypothesis

_PROMPT_DIR = Path(__file__).parent / "prompts"

# RootCauseHypothesis.suggested_fix_class is a Literal[...] in shared/contracts.py.
# We extract the allowed values once so the parser can drop hallucinated ones
# rather than letting Pydantic raise (which would discard the whole batch).
_VALID_FIX_CLASSES: set[str] = set(
    get_args(RootCauseHypothesis.model_fields["suggested_fix_class"].annotation)
)


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
# Claude-backed implementation                                                 #
# ---------------------------------------------------------------------------- #


class ClaudeDiagnoser:
    """Real LLM diagnostician.

    Wires claude-agent-sdk's ``query()`` with five MCP tools that read evidence:
        - read_file, list_files, grep  -> target source code (TargetCodeReader)
        - query_loki                   -> logs within the chaos window
        - query_prometheus             -> metrics

    Runtime requirements:
        - The ``claude`` CLI must be installed and authenticated.
        - Network access to the Anthropic API.
        - Backends MUST be supplied at diagnose-time for each tool the model
          might use; missing backends yield clear tool-error messages so the
          model can adapt rather than crashing the session.

    Tests don't invoke this. Use FixtureDiagnoser.
    """

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-7",
        max_turns: int = 30,
        max_budget_usd: float = 5.0,
        api_base: str | None = None,
    ) -> None:
        self.model = model
        self.max_turns = max_turns
        self.max_budget_usd = max_budget_usd
        self.api_base = api_base

    async def diagnose(
        self,
        *,
        request: DiagnosisRequest,
        loki: LokiBackend | None = None,
        prom: PromBackend | None = None,
        code: TargetCodeReader | None = None,
    ) -> list[RootCauseHypothesis]:
        from agents._llm import LLMTool, complete_with_tools

        window_start, window_end = _chaos_window(request)

        # Code-reading tools — return error strings (not exceptions) when a
        # backend is missing so the model can adapt across turns.
        async def _read_file(args: dict) -> str:
            if code is None:
                return "error: no TargetCodeReader configured for this diagnosis"
            try:
                return code.read_file(args["path"])
            except CodeReadError as e:
                return f"error: read_file: {e}"

        async def _list_files(args: dict) -> str:
            if code is None:
                return "error: no TargetCodeReader configured"
            try:
                return "\n".join(code.list_files(args["glob"]))
            except CodeReadError as e:
                return f"error: list_files: {e}"

        async def _grep(args: dict) -> str:
            if code is None:
                return "error: no TargetCodeReader configured"
            try:
                hits = code.grep(args["pattern"], glob=args["glob"])
            except CodeReadError as e:
                return f"error: grep: {e}"
            return "\n".join(f"{p}:{ln}:{txt}" for p, ln, txt in hits) or "(no matches)"

        async def _query_loki(args: dict) -> str:
            if loki is None:
                return "error: no LokiBackend configured"
            try:
                lines = await loki.query_range(
                    args["logql"],
                    start=window_start,
                    end=window_end,
                    limit=int(args.get("limit", 200)),
                )
            except LokiQueryError as e:
                return f"error: query_loki: {e}"
            return "\n".join(f"{ln.timestamp_ns}: {ln.line}" for ln in lines) or "(no lines)"

        async def _query_prom(args: dict) -> str:
            if prom is None:
                return "error: no PromBackend configured"
            try:
                samples = await prom.query_instant(args["promql"], ts=window_end)
            except PromQueryError as e:
                return f"error: query_prometheus: {e}"
            return "\n".join(f"{s.value} {s.labels}" for s in samples) or "(no samples)"

        tools = [
            LLMTool(
                name="read_file",
                description="Read a file from the target repo.",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                handler=_read_file,
            ),
            LLMTool(
                name="list_files",
                description="List files matching a glob in the target repo.",
                parameters={
                    "type": "object",
                    "properties": {"glob": {"type": "string"}},
                    "required": ["glob"],
                },
                handler=_list_files,
            ),
            LLMTool(
                name="grep",
                description=(
                    "Regex search across files matching glob. Returns 'path:line:text' rows."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "glob": {"type": "string"},
                    },
                    "required": ["pattern", "glob"],
                },
                handler=_grep,
            ),
            LLMTool(
                name="query_loki",
                description=(
                    "LogQL query within the experiment's chaos window. "
                    "Returns lines newline-separated. Limit defaults to 200."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "logql": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["logql"],
                },
                handler=_query_loki,
            ),
            LLMTool(
                name="query_prometheus",
                description="PromQL instant query at the end of the chaos window.",
                parameters={
                    "type": "object",
                    "properties": {"promql": {"type": "string"}},
                    "required": ["promql"],
                },
                handler=_query_prom,
            ),
        ]

        system_prompt = (_PROMPT_DIR / "diagnose.md").read_text(encoding="utf-8")
        user_prompt = _build_user_prompt(request, window_start, window_end)

        result = await complete_with_tools(
            model=self.model,
            system=system_prompt,
            user=user_prompt,
            tools=tools,
            max_turns=self.max_turns,
            max_budget_usd=self.max_budget_usd,
            api_base=self.api_base,
        )
        return _parse_hypotheses(result.final_text)


# ---------------------------------------------------------------------------- #
# Helpers                                                                      #
# ---------------------------------------------------------------------------- #


def _chaos_window(request: DiagnosisRequest) -> tuple[float, float]:
    """Unix-seconds [start, end] window covering the chaos timeline, plus buffer.

    30s pre-buffer catches a fault's setup phase; 60s post catches lagging effects.
    """
    events = request.chaos_timeline.events
    if not events:
        # Degenerate — but still produce a sensible window so tools don't fail.
        import time

        now = time.time()
        return now - 30.0, now + 60.0
    first = events[0].timestamp.timestamp()
    last = events[-1].timestamp.timestamp()
    return first - 30.0, last + 60.0


def _build_user_prompt(
    request: DiagnosisRequest, window_start: float, window_end: float
) -> str:
    """One JSON payload the model can read, with the chaos window made explicit."""
    payload = {
        "experiment_id": request.experiment_id,
        "chaos_window_unix_seconds": {"start": window_start, "end": window_end},
        "failed_tester_report": (
            request.failed_tester_report.model_dump(mode="json")
            if request.failed_tester_report
            else None
        ),
        "failed_security_report": (
            request.failed_security_report.model_dump(mode="json")
            if request.failed_security_report
            else None
        ),
        "chaos_timeline": request.chaos_timeline.model_dump(mode="json"),
        "target_repo": request.target_repo,
    }
    return (
        "Diagnose the regression described below. Use your tools to gather evidence "
        "(logs in the chaos window, metrics, target source). Return ONLY a JSON array of "
        "RootCauseHypothesis objects matching the schema in your system prompt, ranked "
        "by confidence descending. Cite specific log lines, trace IDs, or file:line in "
        "the `evidence` field.\n\n"
        f"```json\n{json.dumps(payload, indent=2)}\n```"
    )


def _parse_hypotheses(text: str) -> list[RootCauseHypothesis]:
    """Pull a JSON array of RootCauseHypothesis objects from the model's final text.

    Hallucinated `suggested_fix_class` values are stripped (the model gets a known
    set of allowed values from the system prompt; if it invents one we drop the item
    rather than try to coerce).
    """
    if not text.strip():
        return []
    raw = _extract_json_blob(text)
    if raw is None:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return []

    out: list[RootCauseHypothesis] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        fix_cls = item.get("suggested_fix_class")
        if fix_cls not in _VALID_FIX_CLASSES:
            continue
        try:
            out.append(RootCauseHypothesis.model_validate(item))
        except Exception:
            continue
    return out


def _extract_json_blob(text: str) -> str | None:
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    indices = [(text.find(ch), ch) for ch in "[{"]
    valid = [(i, ch) for i, ch in indices if i != -1]
    if not valid:
        return None
    valid.sort()
    return text[valid[0][0]:].strip()
