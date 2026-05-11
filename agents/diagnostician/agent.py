"""Diagnostician agent. Correlates failed reports + chaos timeline + code to produce ranked hypotheses."""

from __future__ import annotations

from pathlib import Path

import typer

from agents._cli import notice
from shared.contracts import DiagnosisReport, DiagnosisRequest

_PROMPT_DIR = Path(__file__).parent / "prompts"


class ClaudeDiagnosticianAgent:
    """Implements `orchestrator.loop.DiagnosticianAgent`."""

    def __init__(self, *, model: str = "claude-opus-4-7") -> None:
        self.model = model

    async def diagnose(self, req: DiagnosisRequest) -> DiagnosisReport:
        _prompt = (_PROMPT_DIR / "diagnose.md").read_text()
        # TODO(milestone-5): Claude Agent SDK session with tools:
        # query_loki, query_prometheus, fetch_trace, query_tempo,
        # read_target_code, grep_target_code, list_target_code, prior_records.
        raise NotImplementedError(
            "ClaudeDiagnosticianAgent.diagnose is a milestone-5 task; use --dry-run for now"
        )


# ---------- CLI ----------------------------------------------------------------

app = typer.Typer(help="Diagnostician (debugger) — RCA from logs + traces + code.", no_args_is_help=True)


@app.command()
def diagnose(
    experiment_id: str = typer.Option(..., "--experiment-id"),
) -> None:
    """Diagnose a specific past experiment by ID."""
    notice("diagnostician", "diagnose", "milestone-5.0",
           hint=f"would load record {experiment_id} from store and run diagnose()")


@app.command()
def replay(fixture: Path = typer.Argument(..., exists=True, readable=True)) -> None:
    """Replay diagnosis against a recorded fixture."""
    notice("diagnostician", "replay", "milestone-5.0",
           hint=f"would replay against {fixture}")


if __name__ == "__main__":
    app()
