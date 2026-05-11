"""Fixer agent. Produces a draft PR for the diagnosed regression. Never auto-merges."""

from __future__ import annotations

from pathlib import Path

import typer

from agents._cli import notice
from shared.contracts import DiagnosisReport, FixProposal

_PROMPT_DIR = Path(__file__).parent / "prompts"


class ClaudeFixerAgent:
    """Implements `orchestrator.loop.FixerAgent`."""

    def __init__(
        self,
        *,
        model: str = "claude-opus-4-7",
        path_denylist: tuple[str, ...] = (".github/", "infra/", "secrets/"),
        max_open_prs_per_repo: int = 3,
    ) -> None:
        self.model = model
        self.path_denylist = path_denylist
        self.max_open_prs_per_repo = max_open_prs_per_repo

    async def propose_fix(self, diagnosis: DiagnosisReport) -> FixProposal:
        _prompt = (_PROMPT_DIR / "fix.md").read_text()
        # TODO(milestone-6): Claude Agent SDK session with tools:
        # read_target_code, edit_target_code, write_test, run_tests,
        # git_branch, gh_pr_create, record_doc.
        # The returned FixProposal MUST have is_draft=True (contract validates).
        raise NotImplementedError(
            "ClaudeFixerAgent.propose_fix is a milestone-6 task; use --dry-run for now"
        )


# ---------- CLI ----------------------------------------------------------------

app = typer.Typer(help="Fixer — draft PRs, never auto-merges.", no_args_is_help=True)


@app.command()
def propose(
    experiment_id: str = typer.Option(..., "--experiment-id"),
    open_pr: bool = typer.Option(False, "--open-pr/--no-pr"),
) -> None:
    """Generate a fix proposal (patch + test + PR body). Optionally open the draft PR."""
    notice(
        "fixer",
        "propose",
        "milestone-6.0" if not open_pr else "milestone-6.3",
        hint=(
            f"would load record {experiment_id}, run ClaudeFixerAgent.propose_fix()"
            + (", then `gh pr create --draft`" if open_pr else ", emit artifact only")
        ),
    )


@app.command(name="gh-status")
def gh_status() -> None:
    """Verify gh CLI is installed and authenticated. Required before --open-pr."""
    import shutil
    import subprocess

    if shutil.which("gh") is None:
        typer.echo("gh CLI not found on PATH", err=True)
        raise typer.Exit(1)
    rc = subprocess.run(["gh", "auth", "status"], check=False).returncode
    raise typer.Exit(rc)


if __name__ == "__main__":
    app()

