"""Diagnostician prior-records similarity (milestone-5.3)."""

from __future__ import annotations

import typer

from agents._cli import notice

app = typer.Typer(help="Find past experiments similar to a given one.", no_args_is_help=True)


@app.callback(invoke_without_command=True)
def main(experiment_id: str = typer.Option(..., "--experiment-id")) -> None:
    notice("diagnostician.similarity", "find", "milestone-5.3",
           hint=f"would search store for past records similar to {experiment_id}")


if __name__ == "__main__":
    app()
