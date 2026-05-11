"""Diagnostician tools — Loki / Tempo / Prometheus / code-reading helpers (milestone-5)."""

from __future__ import annotations

import typer

from agents._cli import notice

app = typer.Typer(help="Diagnostician tools.", no_args_is_help=True)


@app.command()
def loki(
    experiment_id: str = typer.Option(..., "--experiment-id"),
    query: str = typer.Option(..., "--query", help="LogQL"),
) -> None:
    """Run a LogQL query within an experiment's chaos window."""
    notice("diagnostician.tools", "loki", "milestone-5.0",
           hint=f"would query Loki: {query!r} within window of {experiment_id}")


@app.command()
def tempo(
    experiment_id: str = typer.Option(..., "--experiment-id"),
    query: str = typer.Option(..., "--query", help="TraceQL"),
) -> None:
    """TraceQL search within an experiment's chaos window."""
    notice("diagnostician.tools", "tempo", "milestone-5.0",
           hint=f"would query Tempo: {query!r} within window of {experiment_id}")


if __name__ == "__main__":
    app()
