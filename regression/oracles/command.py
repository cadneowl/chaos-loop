"""Command oracle: use any shell command's exit code as the resilience predicate.

For customers without a Playwright suite. ``capture_baseline`` runs the command
CLEAN and remembers whether it passed; ``verify`` runs it again UNDER FAULT.
Same *newly-failing* semantics as the Playwright oracle collapsed to a single
journey (the command): a regression is baseline-green → fault-red. A command
already failing at baseline can't be *newly* failing, so it isn't a regression.

``_exec`` is the single I/O boundary; tests monkeypatch it.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from plugins.base import ExperimentPlugin, PluginContext
from plugins.registry import register_plugin
from shared.contracts import (
    FindingSeverity,
    StatisticalSample,
    VerifyFailure,
    VerifyResult,
)


@register_plugin
class CommandOraclePlugin(ExperimentPlugin):
    """Regression oracle backed by an exit-code command (pytest, health check, ...)."""

    name = "regression-command"

    def _journey(self, ctx: PluginContext) -> str:
        return str(ctx.config.get("name", "command"))

    async def capture_baseline(self, ctx: PluginContext) -> list[StatisticalSample]:
        code = await self._exec(ctx.config)
        ctx.scratch["baseline_ok"] = code == 0
        ctx.scratch["baseline_code"] = code
        return []

    async def verify(self, ctx: PluginContext) -> VerifyResult | None:
        code = await self._exec(ctx.config)
        journey = self._journey(ctx)
        baseline_ok = bool(ctx.scratch.get("baseline_ok", False))
        newly_failing = baseline_ok and code != 0
        if not newly_failing:
            if not baseline_ok:
                # Command already failing before the fault -> can't assess.
                return VerifyResult(
                    passed=True,
                    summary=f"{journey} already failing at baseline "
                    f"(exit {ctx.scratch.get('baseline_code')}); cannot assess resilience",
                    evidence={"newly_failing": [], "baseline_unassessable": True},
                )
            return VerifyResult(
                passed=True,
                summary=f"{journey} exit={code} under fault",
                evidence={"newly_failing": [], "baseline_ok": baseline_ok},
            )
        return VerifyResult(
            passed=False,
            summary=f"{journey} regressed under fault (exit {code})",
            failures=[
                VerifyFailure(
                    assertion=journey,
                    expected="exit 0 under fault",
                    actual=f"exit {code}",
                    severity=FindingSeverity.HIGH,
                )
            ],
            evidence={"newly_failing": [journey]},
        )

    async def _exec(self, config: dict[str, Any]) -> int:
        command = config.get("command")
        if not command:
            raise ValueError("command oracle requires oracle_config['command']")
        cwd = str(config.get("cwd", "."))
        timeout_s = float(config.get("timeout_s", 600.0))
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (config.get("env") or {}).items()})

        if isinstance(command, str):
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *[str(part) for part in command],
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        return proc.returncode if proc.returncode is not None else -1
