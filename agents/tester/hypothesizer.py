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


def _build_system_prompt() -> str:
    """Read the hypothesize prompt + append the live catalogue of valid fault names.

    Building it at call-time means the prompt is always in sync with the catalogue;
    new faults show up automatically without prompt edits.
    """
    from agents.chaos.faults._meta import CATALOGUE

    base = (_PROMPT_DIR / "hypothesize.md").read_text(encoding="utf-8")
    lines = ["", "## Catalogue of valid `proposed_fault` values", ""]
    for name in sorted(CATALOGUE):
        f = CATALOGUE[name]
        approval = " (requires approval)" if f.requires_approval else ""
        lines.append(f"- `{name}`{approval} — {f.description}")
    lines.append("")
    lines.append(
        "**Any `proposed_fault` not in this list will be silently dropped from the report.**"
    )
    return base + "\n" + "\n".join(lines)


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

    Wires three read-only tools (read_file, list_files, grep) backed by a
    sandboxed TargetCodeReader, and runs a multi-turn tool-call completion via
    the universal LiteLLM runner.

    Default model is Anthropic's Claude. Pass any LiteLLM-supported model name
    (e.g. ``ollama/qwen2.5-coder:14b``) to use a different provider; for Ollama,
    also pass ``api_base="http://localhost:11434"``.

    Tests: don't invoke. Use FixtureHypothesizer.
    """

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-7",
        max_turns: int = 25,
        max_budget_usd: float = 3.0,
        api_base: str | None = None,
    ) -> None:
        self.model = model
        self.max_turns = max_turns
        self.max_budget_usd = max_budget_usd
        self.api_base = api_base

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

        from agents._llm import LLMTool, complete_with_tools

        async def _read_file(args: dict) -> str:
            return code.read_file(args["path"])

        async def _list_files(args: dict) -> str:
            return "\n".join(code.list_files(args["glob"]))

        async def _grep(args: dict) -> str:
            hits = code.grep(args["pattern"], glob=args["glob"])
            return "\n".join(f"{p}:{ln}:{txt}" for p, ln, txt in hits) or "(no matches)"

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
                    "Regex search across files matching glob. "
                    "Returns 'path:line:text' rows."
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
        ]

        system_prompt = _build_system_prompt()
        user_prompt = (
            f"target_app: {target_app}\n"
            f"target_repo: {target_repo or '(not specified)'}\n\n"
            "Use your tools to read the actual code, then produce a JSON array "
            "of Hypothesis objects matching the schema in your system prompt. "
            "Return ONLY the JSON array, no surrounding prose."
        )

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
