"""
Cognitive surface of the tester's hypothesize mode.

The Hypothesizer Protocol exists so the tester agent can:
    - Use a FixtureHypothesizer in tests (deterministic, no API calls)
    - Use a ClaudeHypothesizer in production (real LLM, code-reading MCP tools)
without the surrounding wiring caring which is in use.

Hypotheses are returned as `Hypothesis` instances. The agent post-validates them
against the fault catalogue so a hallucinated fault name doesn't propagate.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from agents.diagnostician.tools.code_reader import TargetCodeReader
from shared.contracts import Hypothesis

_PROMPT_DIR = Path(__file__).parent / "prompts"


class Hypothesizer(Protocol):
    """Generates code-grounded chaos hypotheses for a target."""

    async def generate(
        self,
        *,
        target_app: str,
        target_repo: str | None,
        code: TargetCodeReader | None,
    ) -> list[Hypothesis]: ...


# ---------------------------------------------------------------------------- #
# Fixture (tests + offline runs)                                               #
# ---------------------------------------------------------------------------- #


FixtureFn = Callable[[str, str | None, TargetCodeReader | None], Awaitable[list[Hypothesis]]]


class FixtureHypothesizer:
    """Returns predetermined hypotheses, or runs an async callback."""

    def __init__(self, hypotheses: list[Hypothesis] | FixtureFn) -> None:
        self._h = hypotheses

    async def generate(
        self,
        *,
        target_app: str,
        target_repo: str | None,
        code: TargetCodeReader | None,
    ) -> list[Hypothesis]:
        if callable(self._h):
            return await self._h(target_app, target_repo, code)
        return list(self._h)


# ---------------------------------------------------------------------------- #
# Claude-backed implementation                                                 #
# ---------------------------------------------------------------------------- #


class ClaudeHypothesizer:
    """Real LLM hypothesizer.

    Wires claude-agent-sdk's ``query()`` with three MCP tools that read the
    target's repo (sandboxed via TargetCodeReader). The model is instructed to
    return a JSON array of Hypothesis objects; we parse and validate.

    Runtime requirements:
        - The ``claude`` CLI must be installed and authenticated.
        - The host running this must have network access to the Anthropic API.
        - Pass an explicit TargetCodeReader; we won't auto-resolve a repo path.

    Tests: don't invoke. Use FixtureHypothesizer.
    """

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-7",
        max_turns: int = 25,
        max_budget_usd: float = 3.0,
    ) -> None:
        self.model = model
        self.max_turns = max_turns
        self.max_budget_usd = max_budget_usd

    async def generate(
        self,
        *,
        target_app: str,
        target_repo: str | None,
        code: TargetCodeReader | None,
    ) -> list[Hypothesis]:
        if code is None:
            raise ValueError(
                "ClaudeHypothesizer needs a TargetCodeReader; pass code=... to the tester agent"
            )

        # Imported here so non-LLM paths (tests, dry-run) don't pay the SDK import cost.
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            create_sdk_mcp_server,
            query,
            tool,
        )

        # Define MCP tools that proxy our sandboxed code reader. The tool's
        # input_schema is a Python type dict; the implementation returns the
        # MCP-shaped result the SDK expects.
        @tool("read_file", "Read a file from the target repo.", {"path": str})
        async def _read_file(args: dict) -> dict:
            content = code.read_file(args["path"])
            return {"content": [{"type": "text", "text": content}]}

        @tool("list_files", "List files matching a glob in the target repo.", {"glob": str})
        async def _list_files(args: dict) -> dict:
            paths = code.list_files(args["glob"])
            return {"content": [{"type": "text", "text": "\n".join(paths)}]}

        @tool(
            "grep",
            "Search for a regex pattern across files matching a glob. Returns 'path:line:text' rows.",
            {"pattern": str, "glob": str},
        )
        async def _grep(args: dict) -> dict:
            hits = code.grep(args["pattern"], glob=args["glob"])
            rows = "\n".join(f"{p}:{ln}:{txt}" for p, ln, txt in hits)
            return {"content": [{"type": "text", "text": rows or "(no matches)"}]}

        mcp = create_sdk_mcp_server(
            "target_code", "1.0.0", [_read_file, _list_files, _grep]
        )

        system_prompt = (_PROMPT_DIR / "hypothesize.md").read_text(encoding="utf-8")
        user_prompt = (
            f"target_app: {target_app}\n"
            f"target_repo: {target_repo or '(not specified)'}\n\n"
            "Read the codebase via your tools and produce a JSON array of "
            "Hypothesis objects matching the schema in your system prompt. "
            "Return ONLY the JSON array, no surrounding prose."
        )

        options = ClaudeAgentOptions(
            model=self.model,
            system_prompt=system_prompt,
            mcp_servers={"target_code": mcp},
            allowed_tools=[
                "mcp__target_code__read_file",
                "mcp__target_code__list_files",
                "mcp__target_code__grep",
            ],
            max_turns=self.max_turns,
            max_budget_usd=self.max_budget_usd,
            permission_mode="bypassPermissions",
        )

        final_text = ""
        async for msg in query(prompt=user_prompt, options=options):
            if isinstance(msg, AssistantMessage):
                # Only consider the final assistant text; later messages overwrite.
                text_parts = [b.text for b in msg.content if isinstance(b, TextBlock)]
                if text_parts:
                    final_text = "".join(text_parts)
            elif isinstance(msg, ResultMessage):
                # ResultMessage signals the run is done; nothing more to consume.
                break

        return _parse_hypotheses(final_text)


# ---------------------------------------------------------------------------- #
# Parsing                                                                      #
# ---------------------------------------------------------------------------- #


def _parse_hypotheses(text: str) -> list[Hypothesis]:
    """Pull a JSON array of Hypothesis objects from the model's final text.

    Robust to: surrounding prose, code-fence wrappers (```json ... ```), and
    object-vs-array forms (we wrap a single dict in a list).

    Validation: each item is run through ``Hypothesis.model_validate``; items
    that don't validate are dropped (not the whole batch).
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

    out: list[Hypothesis] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            out.append(Hypothesis.model_validate(item))
        except Exception:
            continue
    return out


def _extract_json_blob(text: str) -> str | None:
    """Find the JSON array (or object) in `text`. Strips ```json fences if present.

    For non-fenced inputs we take whichever of '[' or '{' appears first — taking
    just one would mis-fire on an object whose body contains an inner '['
    (e.g., a "success_criteria" array inside a Hypothesis object).
    """
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    indices = [(text.find(ch), ch) for ch in "[{"]
    valid = [(i, ch) for i, ch in indices if i != -1]
    if not valid:
        return None
    valid.sort()  # earliest position wins
    start = valid[0][0]
    return text[start:].strip()
