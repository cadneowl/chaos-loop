"""Fixer PR body assembly (milestone-6.3)."""

from __future__ import annotations

import typer

from agents._cli import notice

app = typer.Typer(help="Render the PR body for an experiment.", no_args_is_help=True)


@app.callback(invoke_without_command=True)
def main(experiment_id: str = typer.Option(..., "--experiment-id")) -> None:
    notice("fixer.pr_template", "render", "milestone-6.3",
           hint=f"would assemble PR body for {experiment_id}")


if __name__ == "__main__":
    app()
