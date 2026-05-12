"""Fixer agent. Produces a draft PR for the diagnosed regression. Never auto-merges."""

from __future__ import annotations

from pathlib import Path

import typer

from agents.fixer.policy import (
    DEFAULT_MIN_CONFIDENCE,
    PathDenylist,
    decide_action,
)
from agents.fixer.strategy import FixerStrategy
from shared.contracts import (
    DiagnosisReport,
    FixAction,
    FixProposal,
)

_PROMPT_DIR = Path(__file__).parent / "prompts"


class ClaudeFixerAgent:
    """Implements `orchestrator.loop.FixerAgent`.

    Routing is deterministic; the LLM-driven step is wrapped behind the
    FixerStrategy Protocol. Whatever the strategy returns is run through the
    safety policy (denylist) before being assembled into a FixProposal.
    """

    def __init__(
        self,
        *,
        strategy: FixerStrategy | None = None,
        denylist: PathDenylist | None = None,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        runs_dir: Path | None = None,
        model: str = "claude-opus-4-7",
    ) -> None:
        self._strategy = strategy
        self._denylist = denylist or PathDenylist()
        self._min_confidence = min_confidence
        self._runs_dir = runs_dir
        self.model = model

    async def propose_fix(self, diagnosis: DiagnosisReport) -> FixProposal:
        # DiagnosisReport.hypotheses is guaranteed non-empty by the schema; the
        # diagnostician sorts by confidence desc.
        top = diagnosis.hypotheses[0]
        action = decide_action(
            top.suggested_fix_class, top.confidence, min_confidence=self._min_confidence
        )

        if action == FixAction.NONE:
            return _none(
                diagnosis,
                top.confidence,
                reasoning=(
                    f"top hypothesis confidence {top.confidence:.2f} < "
                    f"min {self._min_confidence:.2f}; deferring to humans"
                ),
            )

        if action == FixAction.DOC_ONLY:
            doc_path = self._write_fragility_doc(diagnosis)
            return FixProposal(
                experiment_id=diagnosis.experiment_id,
                action=FixAction.DOC_ONLY,
                pr_url=None,
                confidence=top.confidence,
                reasoning=(
                    f"diagnosed as working-as-intended: {top.summary}. "
                    f"Fragility note written to {doc_path}."
                ),
                files_touched=[str(doc_path)],
                regression_test_added=False,
                is_draft=True,
            )

        # action ∈ {CODE_PATCH, CONFIG_CHANGE}: delegate to the cognitive strategy.
        if self._strategy is None:
            return _none(
                diagnosis,
                top.confidence,
                reasoning=(
                    f"no FixerStrategy configured; can't propose {action.value}. "
                    "Pass strategy=... when constructing ClaudeFixerAgent."
                ),
            )
        output = await self._strategy.propose(diagnosis=diagnosis, intended_action=action)

        # Denylist enforcement runs on the strategy's output — even a misbehaving
        # strategy can't push us past policy.
        denied = self._denylist.reasons(output.files_touched)
        if denied:
            return _none(
                diagnosis,
                top.confidence,
                reasoning="strategy proposed denylisted paths: " + "; ".join(denied),
            )

        return FixProposal(
            experiment_id=diagnosis.experiment_id,
            action=action,
            pr_url=output.pr_url,
            confidence=top.confidence,
            reasoning=output.reasoning,
            files_touched=list(output.files_touched),
            regression_test_added=output.regression_test_added,
            is_draft=True,
        )

    def _write_fragility_doc(self, diagnosis: DiagnosisReport) -> Path:
        """Write a markdown note for a working-as-intended diagnosis."""
        top = diagnosis.hypotheses[0]
        runs_dir = self._runs_dir or _default_runs_dir()
        out_dir = runs_dir / diagnosis.experiment_id / "proposed"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "working-as-intended.md"
        body = _fragility_doc(diagnosis, top.summary, top.evidence, top.affected_paths)
        path.write_text(body, encoding="utf-8")
        return path


# ---------- helpers ------------------------------------------------------------


def _none(diagnosis: DiagnosisReport, confidence: float, *, reasoning: str) -> FixProposal:
    """Build an action=NONE proposal with explanation. Schema-valid."""
    return FixProposal(
        experiment_id=diagnosis.experiment_id,
        action=FixAction.NONE,
        pr_url=None,
        confidence=confidence,
        reasoning=reasoning,
        files_touched=[],
        regression_test_added=False,
        is_draft=True,
    )


def _default_runs_dir() -> Path:
    # Repo-relative `experiments/runs/`. Tests should pass a tmp path instead.
    return Path(__file__).resolve().parents[2] / "experiments" / "runs"


def _fragility_doc(
    diagnosis: DiagnosisReport,
    summary: str,
    evidence: list[str],
    affected_paths: list[str],
) -> str:
    bullets = "\n".join(f"- {e}" for e in evidence) or "_(none)_"
    paths = "\n".join(f"- `{p}`" for p in affected_paths) or "_(none)_"
    return f"""# Chaos finding: working-as-intended

**Experiment:** `{diagnosis.experiment_id}`
**Diagnosis:** {summary}

This regression is consistent with the system's documented design, not a bug.
No fix is appropriate; this note records the fragility for future reviewers.

## Evidence

{bullets}

## Affected paths (for reference)

{paths}

## Reviewer note

If you believe this finding *is* a bug, update the diagnostician's prompts or
catalogue so future runs classify it correctly, then re-run the experiment.
"""


# ---------- CLI ----------------------------------------------------------------

app = typer.Typer(help="Fixer — draft PRs, never auto-merges.", no_args_is_help=True)


@app.command(name="gh-status")
def gh_status() -> None:
    """Verify gh CLI is installed and authenticated. Required before --open-pr."""
    import shutil
    import subprocess

    if shutil.which("gh") is None:
        typer.echo("gh CLI not found on PATH", err=True)
        raise typer.Exit(1)
    try:
        # 5s is plenty: gh auth status is a local credential check, no network.
        result = subprocess.run(
            ["gh", "auth", "status"], check=False, timeout=5
        )
    except subprocess.TimeoutExpired:
        typer.echo("gh auth status timed out after 5s", err=True)
        raise typer.Exit(1) from None
    raise typer.Exit(result.returncode)


if __name__ == "__main__":
    app()
