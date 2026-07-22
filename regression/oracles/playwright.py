"""Playwright oracle: inherit a customer's Playwright suite as a regression check.

Implements the *double baseline* on the plugin lifecycle: ``capture_baseline``
runs the suite CLEAN (pre-fault) and records which journeys pass; ``verify``
runs it again UNDER FAULT and reports the *newly-failing* set (green at
baseline, red now). Only newly-failing journeys count as a resilience
regression — a journey already red at baseline is a pre-existing failure, which
the orchestrator's built-in baseline check routes to BASELINE_FAIL, not here.

The single I/O boundary is ``_exec_playwright`` (shells ``npx playwright test
--reporter=json``). Unit tests monkeypatch it with fixture JSON, so the parse
and delta logic run without Node installed.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from plugins.base import ExperimentPlugin, PluginContext
from plugins.registry import register_plugin
from shared.contracts import (
    FindingSeverity,
    StatisticalSample,
    VerifyFailure,
    VerifyResult,
)

_FAIL_STATUSES = frozenset({"failed", "timedOut", "interrupted"})


@dataclass
class SuiteResult:
    """Normalized outcome of one Playwright run."""

    passed_ids: list[str] = field(default_factory=list)
    failed_ids: list[str] = field(default_factory=list)
    all_ids: list[str] = field(default_factory=list)
    screenshots: dict[str, str] = field(default_factory=dict)


def _spec_id(spec: dict[str, Any]) -> str:
    """Stable journey id: ``<file>:<spec title>`` (v1; rename-tolerant hashing is v2)."""
    return f"{spec.get('file', '')}:{spec.get('title', '')}"


def _walk_specs(suite: dict[str, Any], acc: list[dict[str, Any]]) -> None:
    """Flatten Playwright's nested ``suites`` tree into a flat list of specs.

    Each spec inherits its enclosing suite's ``file`` when it doesn't carry one.
    """
    file = suite.get("file", "")
    for spec in suite.get("specs", []):
        merged = dict(spec)
        merged.setdefault("file", file)
        acc.append(merged)
    for nested in suite.get("suites", []):
        _walk_specs(nested, acc)


def _first_screenshot(spec: dict[str, Any]) -> str:
    for test in spec.get("tests", []):
        for result in test.get("results", []):
            for att in result.get("attachments", []):
                if att.get("name") == "screenshot" and att.get("path"):
                    return str(att["path"])
    return ""


def parse_playwright_json(data: dict[str, Any]) -> SuiteResult:
    """Parse ``playwright test --reporter=json`` output into a ``SuiteResult``.

    A spec is *failed* if its ``ok`` flag is False or any result status is in
    ``_FAIL_STATUSES``. A spec whose only results are ``skipped`` is excluded
    from every set (it neither passed nor failed).
    """
    result = SuiteResult()
    specs: list[dict[str, Any]] = []
    for suite in data.get("suites", []):
        _walk_specs(suite, specs)

    for spec in specs:
        sid = _spec_id(spec)
        statuses = [
            r.get("status")
            for test in spec.get("tests", [])
            for r in test.get("results", [])
        ]
        if statuses and all(s == "skipped" for s in statuses):
            continue
        result.all_ids.append(sid)
        failed = spec.get("ok") is False or any(s in _FAIL_STATUSES for s in statuses)
        if failed:
            result.failed_ids.append(sid)
            shot = _first_screenshot(spec)
            if shot:
                result.screenshots[sid] = shot
        else:
            result.passed_ids.append(sid)
    return result


@register_plugin
class PlaywrightOraclePlugin(ExperimentPlugin):
    """Regression oracle backed by a customer's Playwright suite."""

    name = "regression-playwright"

    async def capture_baseline(self, ctx: PluginContext) -> list[StatisticalSample]:
        result = await self._run_suite(ctx)
        # Boolean oracle — stash the pass/fail sets on scratch (ctx.baseline is
        # typed to StatisticalSample and reserved for the metric/drift axis).
        ctx.scratch["baseline_pass_ids"] = result.passed_ids
        ctx.scratch["baseline_failed_ids"] = result.failed_ids
        ctx.scratch["baseline_all_ids"] = result.all_ids
        return []

    async def verify(self, ctx: PluginContext) -> VerifyResult | None:
        green = set(ctx.scratch.get("baseline_pass_ids", []))
        baseline_failed = set(ctx.scratch.get("baseline_failed_ids", []))
        asserted = {str(j) for j in ctx.config.get("journeys", [])}

        # If every asserted journey was already red at baseline, the customer's
        # suite is broken — we can't assess resilience. Report it as unassessable
        # (-> BASELINE_FAIL) instead of a misleading PASS over an empty delta, and
        # skip the redundant under-fault run.
        # ``baseline_passing`` is recorded on every branch so a golden (the
        # chronic drift axis) can be captured from any run's verify_result.
        baseline_passing = sorted(green)

        if asserted and not (asserted & green) and (asserted & baseline_failed):
            return VerifyResult(
                passed=True,
                summary="baseline already failing for all asserted journeys "
                "(broken suite, not a resilience regression)",
                evidence={
                    "baseline_unassessable": True,
                    "baseline_failed": sorted(asserted & baseline_failed),
                    "baseline_passing": baseline_passing,
                    "newly_failing": [],
                },
            )

        result = await self._run_suite(ctx)
        newly_failing = sorted(green & set(result.failed_ids))
        if not newly_failing:
            return VerifyResult(
                passed=True,
                summary="all baseline-green journeys survived the fault",
                evidence={"newly_failing": [], "baseline_passing": baseline_passing},
            )
        return VerifyResult(
            passed=False,
            summary=f"{len(newly_failing)} journey(s) regressed under fault",
            failures=[
                VerifyFailure(
                    assertion=sid,
                    expected="pass under fault",
                    actual="fail",
                    severity=FindingSeverity.HIGH,
                    evidence={"screenshot": result.screenshots.get(sid, "")},
                )
                for sid in newly_failing
            ],
            evidence={"newly_failing": newly_failing, "baseline_passing": baseline_passing},
        )

    async def collect_diagnostics(self, ctx: PluginContext) -> dict[str, Any]:
        return {
            "baseline_pass_ids": ctx.scratch.get("baseline_pass_ids", []),
            "baseline_failed_ids": ctx.scratch.get("baseline_failed_ids", []),
        }

    # ----- I/O boundary (monkeypatched in tests) --------------------------
    async def _run_suite(self, ctx: PluginContext) -> SuiteResult:
        # Dry run: exercise the whole flow without Node / a live target. Every
        # journey the scenario asserts is reported as passing, so the run
        # completes as a clean PASS end-to-end (used by `regression run --dry-run`).
        if ctx.config.get("_dry_run"):
            journeys = [str(j) for j in ctx.config.get("journeys", [])]
            return SuiteResult(passed_ids=list(journeys), all_ids=list(journeys))

        data = await self._exec_playwright(ctx.config)
        result = parse_playwright_json(data)
        quarantine = {str(q) for q in ctx.config.get("quarantine", [])}
        if quarantine:
            result.passed_ids = [s for s in result.passed_ids if s not in quarantine]
            result.failed_ids = [s for s in result.failed_ids if s not in quarantine]
            result.all_ids = [s for s in result.all_ids if s not in quarantine]
        return result

    async def _exec_playwright(self, config: dict[str, Any]) -> dict[str, Any]:
        suite_path = str(config.get("suite_path", "."))
        if not os.path.isdir(suite_path):
            raise RuntimeError(
                f"playwright suite_path {suite_path!r} does not exist "
                f"(cwd={os.getcwd()!r}). Set oracle_config.suite_path to your "
                f"Playwright project directory."
            )
        retries = int(config.get("retries", 2))
        base_url = config.get("base_url")
        timeout_s = float(config.get("timeout_s", 600.0))

        # Scope the run to just this scenario's journeys (huge speedup vs. running
        # the whole project twice per scenario). An explicit grep overrides.
        grep = config.get("grep") or _derive_grep(
            [str(j) for j in config.get("journeys", [])]
        )

        cmd = ["npx", "playwright", "test", "--reporter=json", f"--retries={retries}"]
        if grep:
            cmd += ["--grep", str(grep)]
        env = dict(os.environ)
        if base_url:
            env["PLAYWRIGHT_BASE_URL"] = str(base_url)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=suite_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        text = out.decode("utf-8", "replace").strip()
        if not text:
            raise RuntimeError(
                f"playwright produced no JSON (exit={proc.returncode}): "
                f"{err.decode('utf-8', 'replace')[:500]}"
            )
        parsed: dict[str, Any] = json.loads(text)
        return parsed


def _derive_grep(journeys: list[str]) -> str | None:
    """Build a ``--grep`` regex matching just these journeys' titles.

    A journey id is ``<file>:<title>``; Playwright's ``--grep`` matches the test
    title, so we OR the (regex-escaped) title parts. Returns None for no journeys
    (run everything).
    """
    titles = [j.split(":", 1)[1] for j in journeys if ":" in j]
    if not titles:
        return None
    return "|".join(re.escape(t) for t in titles)


def list_journeys(data: dict[str, Any]) -> list[str]:
    """Enumerate every journey id in a ``playwright test --list --reporter=json`` dump.

    Unlike ``parse_playwright_json`` (which classifies a *run* and drops skipped
    specs), this enumerates *all* specs regardless of status — a skipped test is
    still a journey that exists and belongs in the coverage denominator.
    """
    specs: list[dict[str, Any]] = []
    for suite in data.get("suites", []):
        _walk_specs(suite, specs)
    return sorted({_spec_id(s) for s in specs})
