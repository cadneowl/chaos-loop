"""Fixer tools — denylist/codeowners checks (milestone-6)."""

from __future__ import annotations

import typer

from agents._cli import notice

app = typer.Typer(help="Fixer tools.", no_args_is_help=True)


@app.command(name="check-denylist")
def check_denylist(path: str = typer.Option(..., "--path")) -> None:
    """Return exit 0 if path is allowed, 1 if denied."""
    notice("fixer.tools", "check-denylist", "milestone-6.0",
           hint=f"would check {path!r} against the configured denylist")


@app.command(name="check-codeowners")
def check_codeowners(path: str = typer.Option(..., "--path")) -> None:
    """Return the CODEOWNERS list for a path in the target repo."""
    notice("fixer.tools", "check-codeowners", "milestone-6.0",
           hint=f"would parse CODEOWNERS for {path!r}")


if __name__ == "__main__":
    app()
