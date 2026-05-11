"""
Cognitive surface of the fixer.

The Strategy decides what code/config to change for a given diagnosis. The agent
wraps Strategy output with deterministic safety checks (denylist) and produces
the final FixProposal.

Implementations:
    - FixtureFixerStrategy: returns predetermined output. Used by tests and dry-run.
    - ClaudeFixerStrategy: real LLM via Claude Agent SDK with read-only MCP tools.
      Emits a structured fix proposal (files_touched, reasoning, regression-test
      note). Actual file edits + `gh pr create` are a separate follow-on milestone.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from agents.diagnostician.tools.code_reader import CodeReadError, TargetCodeReader
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
# Claude-backed implementation                                                 #
# ---------------------------------------------------------------------------- #


class ClaudeFixerStrategy:
    """Real LLM fixer strategy.

    Wires claude-agent-sdk's ``query()`` with READ-ONLY MCP tools (read_file,
    list_files, grep). The model returns a JSON object describing the proposed
    fix: which files would change, what the diff intent is, whether a regression
    test would be added, and one-line reasoning per change.

    The strategy does NOT mutate the target repo. The artifact is written to
    ``experiments/runs/<experiment_id>/proposed/edits.json`` for human review.
    Actually applying edits + opening a draft PR is a follow-on milestone.

    Runtime requirements:
        - The ``claude`` CLI must be installed and authenticated.
        - Network access to the Anthropic API.
        - Pass an explicit TargetCodeReader; without it the model can only
          guess at files.

    Tests don't invoke this. Use FixtureFixerStrategy.
    """

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-7",
        max_turns: int = 30,
        max_budget_usd: float = 5.0,
        code: TargetCodeReader | None = None,
        artifact_root: Path | None = None,
    ) -> None:
        self.model = model
        self.max_turns = max_turns
        self.max_budget_usd = max_budget_usd
        self._code = code
        # Where the proposal artifact (edits.json) is written. Defaults to the
        # repo's experiments/runs/<exp>/proposed/ when None — set explicitly in
        # tests to avoid polluting the real runs dir.
        self._artifact_root = artifact_root

    async def propose(
        self, *, diagnosis: DiagnosisReport, intended_action: FixAction
    ) -> FixerOutput:
        if self._code is None:
            return FixerOutput(
                reasoning=(
                    "ClaudeFixerStrategy: no TargetCodeReader configured. "
                    "Pass code=... when constructing the strategy."
                ),
                files_touched=[],
                regression_test_added=False,
            )

        # Imported lazily so test paths don't pay the SDK cost.
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            create_sdk_mcp_server,
            query,
            tool,
        )

        code = self._code  # capture for closures

        @tool("read_file", "Read a file from the target repo.", {"path": str})
        async def _read_file(args: dict) -> dict:
            try:
                return _text(code.read_file(args["path"]))
            except CodeReadError as e:
                return _err(f"read_file: {e}")

        @tool("list_files", "List files matching a glob in the target repo.", {"glob": str})
        async def _list_files(args: dict) -> dict:
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
            try:
                hits = code.grep(args["pattern"], glob=args["glob"])
            except CodeReadError as e:
                return _err(f"grep: {e}")
            rows = "\n".join(f"{p}:{ln}:{txt}" for p, ln, txt in hits)
            return _text(rows or "(no matches)")

        mcp = create_sdk_mcp_server(
            "fixer_tools", "1.0.0", [_read_file, _list_files, _grep]
        )

        system_prompt = (Path(__file__).parent / "prompts" / "fix.md").read_text(encoding="utf-8")
        user_prompt = _build_user_prompt(diagnosis, intended_action)

        options = ClaudeAgentOptions(
            model=self.model,
            system_prompt=system_prompt,
            mcp_servers={"fixer_tools": mcp},
            allowed_tools=[
                "mcp__fixer_tools__read_file",
                "mcp__fixer_tools__list_files",
                "mcp__fixer_tools__grep",
            ],
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

        parsed = _parse_fix_proposal(final_text)
        if parsed is None:
            return FixerOutput(
                reasoning=(
                    "ClaudeFixerStrategy: model output did not parse as a fix proposal. "
                    f"Raw text length: {len(final_text)} chars."
                ),
                files_touched=[],
                regression_test_added=False,
            )

        # Persist the proposal artifact so humans can review what would change.
        artifact_path = self._write_artifact(diagnosis.experiment_id, parsed)
        reasoning_with_artifact = (
            f"{parsed['reasoning']}\n\n(Proposal artifact: {artifact_path})"
            if artifact_path
            else parsed["reasoning"]
        )

        return FixerOutput(
            reasoning=reasoning_with_artifact,
            files_touched=list(parsed["files_touched"]),
            regression_test_added=bool(parsed.get("regression_test_added", False)),
            pr_url=None,  # PR creation lands in a later milestone
        )

    def _write_artifact(self, experiment_id: str, parsed: dict) -> Path | None:
        """Persist the parsed proposal to disk for human review."""
        root = self._artifact_root or _default_artifact_root()
        out_dir = root / experiment_id / "proposed"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / "edits.json"
            path.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
            return path
        except OSError:
            return None


# ---------------------------------------------------------------------------- #
# Helpers                                                                      #
# ---------------------------------------------------------------------------- #


def _text(s: str) -> dict:
    """MCP-shaped success result."""
    return {"content": [{"type": "text", "text": s}]}


def _err(msg: str) -> dict:
    """MCP-shaped error result."""
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def _build_user_prompt(diagnosis: DiagnosisReport, intended_action: FixAction) -> str:
    """Encode the diagnosis + intended action so the model has full context."""
    payload = {
        "experiment_id": diagnosis.experiment_id,
        "intended_action": intended_action.value,
        "top_hypothesis": diagnosis.hypotheses[0].model_dump(mode="json"),
        "all_hypotheses": [h.model_dump(mode="json") for h in diagnosis.hypotheses],
    }
    return (
        "Propose a fix for the diagnosis below. Use your tools to read the target's "
        "code; do not invent files that don't exist. Return ONLY a JSON object with "
        'these fields: {"reasoning": str, "files_touched": [str], '
        '"regression_test_added": bool, "edits": [{"path": str, "intent": str}]}.\n\n'
        f"```json\n{json.dumps(payload, indent=2)}\n```"
    )


def _parse_fix_proposal(text: str) -> dict | None:
    """Pull a JSON object from the model's final text. Validates shape, not content."""
    if not text.strip():
        return None
    raw = _extract_json_blob(text)
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    # Required fields with sane defaults; the agent will still enforce its denylist
    # on whatever files_touched comes back, so we don't over-validate here.
    if "reasoning" not in payload or "files_touched" not in payload:
        return None
    if not isinstance(payload["files_touched"], list):
        return None
    if not all(isinstance(p, str) for p in payload["files_touched"]):
        return None
    return payload


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


def _default_artifact_root() -> Path:
    """Repo-relative `experiments/runs/`."""
    # agents/fixer/strategy.py -> repo root is parents[2]
    return Path(__file__).resolve().parents[2] / "experiments" / "runs"
