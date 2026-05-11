"""CLI for `python -m agents.diagnostician.tools <subcommand>`.

Currently exposes `loki` (real query against a configured Loki) and `tempo`
(milestone notice). Used by the dev scripts under agents/diagnostician/scripts/.
"""

from __future__ import annotations

import asyncio
import json

import typer

from agents._cli import notice
from agents.diagnostician.tools.loki import HttpxLokiBackend, LokiQueryError
from orchestrator.store import ExperimentStore

app = typer.Typer(help="Diagnostician tools.", no_args_is_help=True)


@app.command()
def loki(
    experiment_id: str = typer.Option(..., "--experiment-id"),
    query: str = typer.Option(..., "--query", help="LogQL"),
    loki_url: str | None = typer.Option(None, "--loki-url", envvar="LOKI_URL"),
    limit: int = typer.Option(200, "--limit"),
    db: str | None = typer.Option(None, "--db", help="SQLite path (auto if unset)"),
) -> None:
    """Run a LogQL query within an experiment's chaos window."""
    if not loki_url:
        typer.echo("error: --loki-url or $LOKI_URL required", err=True)
        raise typer.Exit(2)

    # Look up the experiment to pick the right time window.
    from pathlib import Path

    db_path = Path(db) if db else (Path.home() / ".local" / "share" / "chaos" / "experiments.sqlite")
    store = ExperimentStore(db_path)
    record = store.load(experiment_id)
    if record is None or record.chaos_timeline is None or not record.chaos_timeline.events:
        typer.echo(f"experiment {experiment_id} not found or has no chaos timeline", err=True)
        raise typer.Exit(1)
    events = record.chaos_timeline.events
    start = events[0].timestamp.timestamp() - 30
    end = events[-1].timestamp.timestamp() + 60

    backend = HttpxLokiBackend(loki_url)
    try:
        lines = asyncio.run(backend.query_range(query, start=start, end=end, limit=limit))
    except LokiQueryError as e:
        typer.echo(f"loki error: {e}", err=True)
        raise typer.Exit(1) from e

    payload = [
        {"ts_ns": ln.timestamp_ns, "line": ln.line, "labels": ln.labels}
        for ln in lines
    ]
    typer.echo(json.dumps(payload, indent=2))


@app.command()
def tempo(
    experiment_id: str = typer.Option(..., "--experiment-id"),
    query: str = typer.Option(..., "--query", help="TraceQL"),
) -> None:
    """TraceQL search within an experiment's chaos window."""
    notice(
        "diagnostician.tools",
        "tempo",
        "milestone-5.0+",
        hint=f"would query Tempo: {query!r} within window of {experiment_id}",
    )


if __name__ == "__main__":
    app()
