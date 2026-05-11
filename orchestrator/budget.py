"""Token / wall-clock budget tracking. Wired into agent invocations."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from shared.contracts import TokenBudget


@dataclass
class BudgetTracker:
    budget: TokenBudget
    spent_usd: float = 0.0
    started_at: float = field(default_factory=time.monotonic)
    soft_warned: bool = False

    def record_spend(self, usd: float) -> None:
        self.spent_usd += usd

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    def soft_warn_due(self) -> bool:
        if self.soft_warned:
            return False
        if self.spent_usd >= self.budget.soft_cap_usd:
            self.soft_warned = True
            return True
        return False

    def hard_exceeded(self) -> bool:
        return (
            self.spent_usd >= self.budget.hard_cap_usd
            or self.elapsed_seconds() >= self.budget.wall_clock_seconds
        )
