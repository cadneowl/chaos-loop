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
# Static (template-based, no LLM)                                              #
# ---------------------------------------------------------------------------- #


# Per fix-class: the proposal we'd hand a human reviewer.
#
# These templates don't produce real diffs — that needs the LLM (or a future
# AST-aware patcher). They produce a clear, actionable summary of what change
# is needed plus a sketched test path. Good enough to draft a human-reviewable
# work item without spending tokens or guessing in code.
@dataclass(frozen=True)
class _FixTemplate:
    summary: str  # one-liner for FixerOutput.reasoning leading line
    detail: str  # multi-line "what to change and why"
    suggests_regression_test: bool


_FIX_TEMPLATES: dict[str, _FixTemplate] = {
    "missing-retry": _FixTemplate(
        summary="Add retry/backoff around the dependency call",
        detail=(
            "Wrap the offending call in a retry decorator with a small number "
            "of attempts and exponential backoff. Recommended primitives:\n"
            "  - tenacity (`@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=0.1, max=2))`)\n"
            "  - or hand-rolled `for attempt in range(3): try: ... except <TransientError>: ...`\n"
            "Cap the total time so the hot path can't be tied up by a slow dep."
        ),
        suggests_regression_test=True,
    ),
    "missing-timeout": _FixTemplate(
        summary="Add an explicit timeout to the call",
        detail=(
            "Add `timeout=<seconds>` to the call. For HTTP, 5-10s is typical for "
            "user-facing paths, 30s for batch. For subprocess, set a hard cap "
            "and `wait_for(timeout=...)` if async. Match the timeout to the "
            "downstream SLA, not the network."
        ),
        suggests_regression_test=True,
    ),
    "missing-circuit-breaker": _FixTemplate(
        summary="Add a circuit breaker around the failing dependency",
        detail=(
            "Wrap the call in a circuit breaker (e.g., circuitbreaker, pybreaker, "
            "or custom). Open the circuit after N consecutive failures, fail "
            "fast for the cooldown window, then probe."
        ),
        suggests_regression_test=True,
    ),
    "missing-fallback": _FixTemplate(
        summary="Add a graceful-degradation fallback when the dependency is unavailable",
        detail=(
            "Catch the dependency error and return a degraded but valid response "
            "(empty cart, cached value, default config). The user-visible failure "
            "mode should be 'no enrichment' rather than '5xx'. If no degraded "
            "response is acceptable, document the dependency as critical and "
            "ensure the on-call runbook covers it."
        ),
        suggests_regression_test=True,
    ),
    "auth-control-gap": _FixTemplate(
        summary="Tighten the auth control to fail closed when the IdP is unavailable",
        detail=(
            "Audit the auth path's behavior when the IdP returns 5xx, times out, "
            "or rate-limits. The default must be DENY (5xx, not 200). Remove or "
            "guard any 'dev fallback' branches; verify with an integration test "
            "that exercises the auth-down path."
        ),
        suggests_regression_test=True,
    ),
    "secret-handling": _FixTemplate(
        summary="Make secret handling tolerate rotation / revocation",
        detail=(
            "Reload secret material on a fixed interval or on first 401/403 from "
            "the dependency. Don't cache for the lifetime of the process. For "
            "certs, refresh from the truststore periodically; for API keys, read "
            "from the secret store on each call (or cache with a short TTL)."
        ),
        suggests_regression_test=True,
    ),
    "image-policy": _FixTemplate(
        summary="Tighten admission policy to reject the offending image class",
        detail=(
            "Add (or correct) a Kyverno / Gatekeeper / OPA policy that rejects "
            "deployments with vulnerable or unsigned images. Verify with a "
            "policy unit test (conftest / kyverno test) that the offending image "
            "is denied at admission."
        ),
        suggests_regression_test=False,  # config-only; uses policy tests, not code tests
    ),
    "config-change": _FixTemplate(
        summary="Adjust configuration to match the observed failure mode",
        detail=(
            "Determine the right config knob (resource limit, replica count, "
            "network policy, timeout budget) and apply it to the relevant "
            "manifest. No application code change needed."
        ),
        suggests_regression_test=False,
    ),
    "test-gap": _FixTemplate(
        summary="Add the test that should have caught this",
        detail=(
            "The bug exists because the test suite didn't cover this scenario. "
            "Write the regression test FIRST (it should fail against current "
            "code), then either (a) the code change is obvious from the test, "
            "or (b) hand off to humans with a clear failing test."
        ),
        suggests_regression_test=True,
    ),
    "code-patch": _FixTemplate(
        summary="Apply a code patch (specifics depend on the diagnosis)",
        detail=(
            "Generic code-patch class — see the diagnosis evidence for what "
            "needs to change. Static templates can't infer the exact change; "
            "this proposal is a placeholder for human or LLM follow-up."
        ),
        suggests_regression_test=True,
    ),
}


def _suggested_test_path(source_path: str) -> str:
    """Sketch a sensible regression-test path for a source file.

    Keeps the language convention: ``services/cart/handler.py`` ->
    ``services/cart/tests/test_handler_regression.py``.
    """
    parts = source_path.replace("\\", "/").split("/")
    if len(parts) == 1:
        return f"tests/test_{Path(parts[0]).stem}_regression.py"
    *dirs, fname = parts
    stem = Path(fname).stem
    return "/".join([*dirs, "tests", f"test_{stem}_regression.py"])


class StaticFixerStrategy:
    """Template-based fixer — no LLM, no actual file edits.

    Produces a FixerOutput whose `reasoning` describes WHAT to change and WHY,
    based on the top hypothesis's `suggested_fix_class`. `files_touched`
    enumerates the affected source files (from the diagnosis) plus a sketched
    regression-test path. The actual diff and the gh PR are out of scope —
    this is a structured work item the agent can hand to a human or to
    ClaudeFixerStrategy for code generation.
    """

    async def propose(
        self, *, diagnosis: DiagnosisReport, intended_action: FixAction
    ) -> FixerOutput:
        # The agent has already routed us here; trust the top hypothesis.
        top = diagnosis.hypotheses[0]
        fix_class = top.suggested_fix_class
        tmpl = _FIX_TEMPLATES.get(fix_class) or _FIX_TEMPLATES["code-patch"]

        files_touched: list[str] = list(top.affected_paths)
        if tmpl.suggests_regression_test and files_touched:
            test_path = _suggested_test_path(files_touched[0])
            if test_path not in files_touched:
                files_touched.append(test_path)

        evidence_lines = "\n".join(f"  - {e}" for e in top.evidence) or "  - (none)"
        reasoning = (
            f"{tmpl.summary}\n\n"
            f"{tmpl.detail}\n\n"
            f"Diagnosis context:\n"
            f"  hypothesis: {top.summary}\n"
            f"  fix class:  {fix_class}\n"
            f"  confidence: {top.confidence:.2f}\n"
            f"  evidence:\n{evidence_lines}\n"
        )

        return FixerOutput(
            reasoning=reasoning,
            files_touched=files_touched,
            regression_test_added=tmpl.suggests_regression_test and bool(top.affected_paths),
            pr_url=None,
        )


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
        api_base: str | None = None,
    ) -> None:
        self.model = model
        self.max_turns = max_turns
        self.max_budget_usd = max_budget_usd
        self._code = code
        # Where the proposal artifact (edits.json) is written. Defaults to the
        # repo's experiments/runs/<exp>/proposed/ when None — set explicitly in
        # tests to avoid polluting the real runs dir.
        self._artifact_root = artifact_root
        self.api_base = api_base

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

        from agents._llm import LLMTool, complete_with_tools

        code = self._code  # capture for closures

        async def _read_file(args: dict) -> str:
            try:
                return code.read_file(args["path"])
            except CodeReadError as e:
                return f"error: read_file: {e}"

        async def _list_files(args: dict) -> str:
            try:
                return "\n".join(code.list_files(args["glob"]))
            except CodeReadError as e:
                return f"error: list_files: {e}"

        async def _grep(args: dict) -> str:
            try:
                hits = code.grep(args["pattern"], glob=args["glob"])
            except CodeReadError as e:
                return f"error: grep: {e}"
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
        ]

        system_prompt = (Path(__file__).parent / "prompts" / "fix.md").read_text(encoding="utf-8")
        user_prompt = _build_user_prompt(diagnosis, intended_action)

        result = await complete_with_tools(
            model=self.model,
            system=system_prompt,
            user=user_prompt,
            tools=tools,
            max_turns=self.max_turns,
            max_budget_usd=self.max_budget_usd,
            api_base=self.api_base,
        )
        parsed = _parse_fix_proposal(result.final_text)
        if parsed is None:
            return FixerOutput(
                reasoning=(
                    "ClaudeFixerStrategy: model output did not parse as a fix proposal. "
                    f"Raw text length: {len(result.final_text)} chars."
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
