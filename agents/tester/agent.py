"""
Tester agent. Claude-backed implementation of the TesterAgent protocol.

This file is the integration surface. The Claude Agent SDK wiring (system prompt,
tool registration, session management) lives here. The cognitive work — what
probes to run, what hypotheses to emit — lives in the prompts and the model.
"""

from __future__ import annotations

from pathlib import Path

import typer

from agents._cli import notice
from shared.contracts import TesterReport, TesterRequest

_PROMPT_DIR = Path(__file__).parent / "prompts"


class ClaudeTesterAgent:
    """Implements `orchestrator.loop.TesterAgent`."""

    def __init__(self, *, model: str = "claude-opus-4-7") -> None:
        self.model = model

    async def baseline(self, req: TesterRequest) -> TesterReport:
        _prompt = (_PROMPT_DIR / "baseline.md").read_text()
        # TODO(milestone-2): wire Claude Agent SDK session with `prompt`,
        # tools = [run_unit_tests, run_playwright, query_prometheus, query_loki,
        #          read_target_code, list_target_code, record_sample],
        # input = req.model_dump_json(),
        # output = parse to TesterReport
        raise NotImplementedError(
            "ClaudeTesterAgent.baseline is a milestone-2 task; use --dry-run for now"
        )

    async def verify(self, req: TesterRequest) -> TesterReport:
        _prompt = (_PROMPT_DIR / "verify.md").read_text()
        raise NotImplementedError("milestone-2")


# ---------- CLI ----------------------------------------------------------------

app = typer.Typer(help="Tester agent — baseline, verify, and hypothesize.", no_args_is_help=True)


@app.command()
def baseline(
    target: str = typer.Option(..., "--target", help="target_app identifier"),
    runs: int = typer.Option(5, "--runs", help="number of probe runs"),
) -> None:
    """Establish a statistical baseline of healthy behavior."""
    notice("tester", "baseline", "milestone-2.0",
           hint=f"would call ClaudeTesterAgent.baseline(target={target!r}, runs={runs})")


@app.command()
def verify(target: str = typer.Option(..., "--target")) -> None:
    """Verify post-chaos behavior against the most recent baseline."""
    notice("tester", "verify", "milestone-2.3",
           hint=f"would call ClaudeTesterAgent.verify(target={target!r})")


@app.command()
def hypothesize(
    target_repo: str = typer.Option(..., "--target-repo", help="git URL of the target"),
) -> None:
    """Generate hypotheses by reading the target source code."""
    notice("tester", "hypothesize", "milestone-2.4",
           hint=f"would read {target_repo} and emit Hypothesis objects")


@app.command()
def replay(fixture: Path = typer.Argument(..., exists=True, readable=True)) -> None:
    """Replay against a recorded fixture (CI use)."""
    notice("tester", "replay", "milestone-2.0",
           hint=f"would replay against {fixture}")


if __name__ == "__main__":
    app()
