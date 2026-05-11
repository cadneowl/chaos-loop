"""
Shared helpers for the per-agent CLIs.

Each agent has a typer app exposing subcommands that match its eventual capabilities.
Before those capabilities land in their target milestone, the subcommands print a clear
notice and exit with code 2 (so automated chains don't silently succeed).

Once a milestone lands, replace the `notice(...)` call with the real implementation.
"""

from __future__ import annotations

import sys

from rich.console import Console
from rich.panel import Panel

_console = Console(stderr=True)


def notice(agent: str, command: str, milestone: str, hint: str | None = None) -> None:
    """Print a structured milestone-notice and exit with code 2."""
    msg = f"[bold yellow]{agent}.{command}[/bold yellow] is not yet implemented.\n"
    msg += f"Planned for milestone: [cyan]{milestone}[/cyan]\n"
    msg += f"Plan:    [cyan]agents/{agent}/README.md[/cyan]"
    if hint:
        msg += f"\n\n{hint}"
    _console.print(Panel(msg, title="not yet wired", border_style="yellow"))
    sys.exit(2)
