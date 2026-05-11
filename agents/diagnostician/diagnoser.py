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
    ) -> None:
        self.model = model
        self.max_turns = max_turns
        self.max_budget_usd = max_budget_usd

    async def diagnose(
        self,
        *,
        request: DiagnosisRequest,
        loki: LokiBackend | None = None,
        prom: PromBackend | None = None,
        code: TargetCodeReader | None = None,
    ) -> list[RootCauseHypothesis]:
        # Imported lazily so non-LLM paths (tests, dry-run) don't pay the SDK cost.
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            create_sdk_mcp_server,
            query,
            tool,
        )

        window_start, window_end = _chaos_window(request)

        # Code-reading tools (mirror the hypothesizer's surface).
        @tool("read_file", "Read a file from the target repo.", {"path": str})
        async def _read_file(args: dict) -> dict:
            if code is None:
                return _err("read_file: no TargetCodeReader configured for this diagnosis")
            try:
                return _text(code.read_file(args["path"]))
            except CodeReadError as e:
                return _err(f"read_file: {e}")

        @tool("list_files", "List files matching a glob in the target repo.", {"glob": str})
        async def _list_files(args: dict) -> dict:
            if code is None:
                return _err("list_files: no TargetCodeReader configured")
            try:
                return _text("\n".join(code.list_files(args["glob"])))
            except CodeReadError as e:
                return _err(f"list_files: {e}")

        @tool(
            "grep",
            "Regex search across files matching glob. Returns 'path:line:text' rows.",
            {"pattern": str, "glob": str},
        )
        async def _grep(args: dict) -> dict:
            if code is None:
                return _err("grep: no TargetCodeReader configured")
            try:
                hits = code.grep(args["pattern"], glob=args["glob"])
            except CodeReadError as e:
                return _err(f"grep: {e}")
            rows = "\n".join(f"{p}:{ln}:{txt}" for p, ln, txt in hits)
            return _text(rows or "(no matches)")

        # Log / metric tools — bounded to the chaos window so the model can't
        # accidentally pull hours of unrelated logs.
        @tool(
            "query_loki",
            (
                "LogQL query within the experiment's chaos window. "
                "Returns lines newline-separated. Limit defaults to 200."
            ),
            {"logql": str, "limit": int},
        )
        async def _query_loki(args: dict) -> dict:
            if loki is None:
                return _err("query_loki: no LokiBackend configured")
            try:
                lines = await loki.query_range(
                    args["logql"],
                    start=window_start,
                    end=window_end,
                    limit=int(args.get("limit", 200)),
                )
            except LokiQueryError as e:
                return _err(f"query_loki: {e}")
            return _text("\n".join(f"{ln.timestamp_ns}: {ln.line}" for ln in lines) or "(no lines)")

        @tool(
            "query_prometheus",
            "PromQL instant query at the end of the chaos window.",
            {"promql": str},
        )
        async def _query_prom(args: dict) -> dict:
            if prom is None:
                return _err("query_prometheus: no PromBackend configured")
            try:
                samples = await prom.query_instant(args["promql"], ts=window_end)
            except PromQueryError as e:
                return _err(f"query_prometheus: {e}")
            return _text(
                "\n".join(f"{s.value} {s.labels}" for s in samples) or "(no samples)"
            )

        mcp = create_sdk_mcp_server(
            "diagnostician_tools",
            "1.0.0",
            [_read_file, _list_files, _grep, _query_loki, _query_prom],
        )

        system_prompt = (_PROMPT_DIR / "diagnose.md").read_text(encoding="utf-8")
        user_prompt = _build_user_prompt(request, window_start, window_end)

        tool_names = [
            "mcp__diagnostician_tools__read_file",
            "mcp__diagnostician_tools__list_files",
            "mcp__diagnostician_tools__grep",
            "mcp__diagnostician_tools__query_loki",
            "mcp__diagnostician_tools__query_prometheus",
        ]
        options = ClaudeAgentOptions(
            model=self.model,
            system_prompt=system_prompt,
            mcp_servers={"diagnostician_tools": mcp},
            allowed_tools=tool_names,
            max_turns=self.max_turns,
            max_budget_usd=self.max_budget_usd,
            permission_mode="bypassPermissions",
        )

        final_text = ""
        async for msg in query(prompt=user_prompt, options=options):
            if isinstance(msg, AssistantMessage):
                parts = [b.text for b in msg.content if isinstance(b, TextBlock)]
                if parts:
                    final_text = "".join(parts)
            elif isinstance(msg, ResultMessage):
                break

        return _parse_hypotheses(final_text)


# ---------------------------------------------------------------------------- #
# Helpers                                                                      #
# ---------------------------------------------------------------------------- #


def _text(s: str) -> dict:
    """MCP-shaped success result."""
    return {"content": [{"type": "text", "text": s}]}


def _err(msg: str) -> dict:
    """MCP-shaped error result. Marking is_error lets the model see it as a tool failure."""
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


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
