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
# Static (pattern-match detectors, no LLM)                                     #
# ---------------------------------------------------------------------------- #


# Each detector maps to a catalogue fault, base confidence, and templates that
# turn its Issues into Hypothesis objects. Kept here (not on the detector) so
# detectors stay focused on pattern-matching and the hypothesis "voice" lives
# in one place.
_DETECTOR_CONFIG: dict[str, dict] = {
    "missing-timeout": {
        "fault": "network.delay",
        "confidence": 0.7,
        "statement": (
            "{file}:{line} calls {detail} with no timeout — under network "
            "latency it will block indefinitely."
        ),
        "rationale": (
            "Detected by static scan: {file}:{line} matches an http or "
            "subprocess call without a `timeout=` kwarg. Under chaos that "
            "delays this dependency, the caller's hot path will hang."
        ),
        "success_criteria": [
            "Under network.delay, the call returns within a bounded time "
            "(rejected as timeout) rather than blocking",
            "Caller's request latency does not exceed (call_timeout + chaos_delay)",
        ],
    },
    "missing-retry": {
        "fault": "network.loss",
        "confidence": 0.6,
        "statement": (
            "{file}:{line} calls {detail} but the file has no retry / "
            "backoff primitive in scope."
        ),
        "rationale": (
            "Detected by static scan: {file}:{line} matches an external-dep "
            "call (Redis / HTTP / DB / queue / cloud SDK) and no retry, "
            "tenacity, backoff, or attempt-loop is referenced anywhere in "
            "the same file. Under transient packet loss, the call will fail "
            "fast on first attempt."
        ),
        "success_criteria": [
            "Under network.loss, the operation either succeeds (after "
            "internal retry) or fails with a clear error within bounded time",
            "Caller does not propagate a single transient failure as a "
            "user-visible 5xx",
        ],
    },
    "single-replica": {
        "fault": "pod.kill",
        "confidence": 0.85,
        "statement": (
            "{file}:{line} declares a Deployment with replicas: 1 — killing "
            "the single pod creates downtime."
        ),
        "rationale": (
            "Detected by static scan: {file} contains kind: Deployment and "
            "{file}:{line} sets replicas: 1. A single-replica Deployment "
            "cannot survive a pod kill without downtime."
        ),
        "success_criteria": [
            "After pod.kill, service is restored within the Deployment's "
            "configured restart window",
            "No request returns 5xx during the gap (or the fragility is "
            "documented as expected)",
        ],
    },
    "hard-pod-affinity": {
        "fault": "pod.kill",
        "confidence": 0.5,
        "statement": (
            "{file}:{line} uses requiredDuringSchedulingIgnoredDuringExecution"
            " — the pod can be pinned to a single node."
        ),
        "rationale": (
            "Detected by static scan: {file}:{line} declares a hard "
            "(required) affinity / anti-affinity rule. If only one node "
            "matches, killing the pod prevents reschedule until that node "
            "is healthy again."
        ),
        "success_criteria": [
            "After pod.kill, the pod is rescheduled within the expected window",
        ],
    },
    "hardcoded-secret": {
        "fault": "secret.rotate",
        "confidence": 0.4,
        "statement": (
            "{file}:{line} appears to assign a hardcoded secret literal."
        ),
        "rationale": (
            "Detected by static scan: {file}:{line} matches a "
            "secret-suggestive name (KEY/SECRET/TOKEN/PASSWORD) assigned "
            "to a string literal, with no env-loading helper on the same "
            "line. Heuristic; review for false positives. Match: {detail}."
        ),
        "success_criteria": [
            "Secret material is loaded from env / vault / config, not "
            "hardcoded in source",
            "Secret rotation does not require a code change + redeploy",
        ],
    },
}


class StaticHypothesizer:
    """Pure-Python hypothesizer driven by pattern-match detectors.

    Zero LLM cost, deterministic output, ~80% coverage of the common chaos
    targets. Use as a baseline; combine with ClaudeHypothesizer via
    HybridHypothesizer when the budget allows.
    """

    def __init__(
        self,
        detectors: list | None = None,  # avoid circular Detector type ref
    ) -> None:
        # Lazy import — keeps the test path light when only Fixture is used.
        from agents.tester.detectors import default_detectors

        self._detectors = detectors if detectors is not None else default_detectors()

    async def generate(
        self,
        *,
        target_app: str,
        target_repo: str | None,
        code: TargetCodeReader | None,
    ) -> list[Hypothesis]:
        if code is None:
            return []
        out: list[Hypothesis] = []
        for det in self._detectors:
            cfg = _DETECTOR_CONFIG.get(det.name)
            if cfg is None:
                # Detector with no template — skip rather than synthesize a
                # half-baked hypothesis.
                continue
            for issue in det.find(code):
                out.append(_issue_to_hypothesis(det.name, issue, cfg))
        return out


def _issue_to_hypothesis(detector_name: str, issue, cfg: dict) -> Hypothesis:
    from agents.tester.detectors._base import hypothesis_id

    fmt_args = {
        "file": issue.file.replace("\\", "/"),
        "line": issue.line,
        "snippet": issue.snippet,
        "detail": issue.detail,
    }
    return Hypothesis(
        id=hypothesis_id(detector_name, issue),
        statement=cfg["statement"].format(**fmt_args),
        rationale=cfg["rationale"].format(**fmt_args),
        proposed_fault=cfg["fault"],
        success_criteria=list(cfg["success_criteria"]),
        confidence=cfg["confidence"],
        code_references=[f"{fmt_args['file']}:{issue.line}"],
    )


# ---------------------------------------------------------------------------- #
# Hybrid (Static + optional LLM)                                               #
# ---------------------------------------------------------------------------- #


import logging  # noqa: E402 — import here keeps the LLM section above clean

_log = logging.getLogger(__name__)


class HybridHypothesizer:
    """Run Static (always) + an optional LLM hypothesizer; merge their output.

    The point: Static gives a free, reliable baseline; the LLM augments with
    novel patterns the rules don't catch. If the LLM is missing or fails, we
    silently degrade to Static-only — the loop never breaks because of an
    optional augmentation.

    Merge: a Hypothesis is "duplicate" if it shares ``proposed_fault`` AND any
    overlapping ``code_references`` with another. We keep the higher-confidence
    version of each duplicate group.
    """

    def __init__(
        self,
        *,
        static: Hypothesizer,
        llm: Hypothesizer | None = None,
    ) -> None:
        self._static = static
        self._llm = llm

    async def generate(
        self,
        *,
        target_app: str,
        target_repo: str | None,
        code: TargetCodeReader | None,
    ) -> list[Hypothesis]:
        static_hyps = await self._static.generate(
            target_app=target_app, target_repo=target_repo, code=code
        )
        if self._llm is None:
            return static_hyps
        try:
            llm_hyps = await self._llm.generate(
                target_app=target_app, target_repo=target_repo, code=code
            )
        except Exception as e:
            _log.warning(
                "HybridHypothesizer: LLM raised %r; returning %d static hypothesis(es)",
                e, len(static_hyps),
            )
            return static_hyps
        return _merge_hypotheses(static_hyps, llm_hyps)


def _merge_hypotheses(
    a: list[Hypothesis], b: list[Hypothesis]
) -> list[Hypothesis]:
    """Combine two hypothesis lists. Duplicates collapse to the higher-confidence
    version. Non-duplicates are all kept.

    Two hypotheses are duplicates iff they share ``proposed_fault`` AND have any
    overlap in ``code_references``. (Same fault on a different file -> distinct.)
    """
    merged: list[Hypothesis] = []
    for cand in [*a, *b]:
        replaced = False
        for i, existing in enumerate(merged):
            if not _are_duplicates(existing, cand):
                continue
            # Same finding from a different source — keep whichever is more confident.
            if cand.confidence > existing.confidence:
                merged[i] = cand
            replaced = True
            break
        if not replaced:
            merged.append(cand)
    merged.sort(key=lambda h: h.confidence, reverse=True)
    return merged


def _are_duplicates(x: Hypothesis, y: Hypothesis) -> bool:
    if x.proposed_fault != y.proposed_fault:
        return False
    xrefs = {_normalize_ref(r) for r in x.code_references}
    yrefs = {_normalize_ref(r) for r in y.code_references}
    return bool(xrefs & yrefs)


def _normalize_ref(ref: str) -> str:
    """Compare references like 'src/x.py:42-55' and 'src/x.py:42' as the same file."""
    return ref.replace("\\", "/").split(":", 1)[0]


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
