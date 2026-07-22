"""Replay a ``RegressionSuite`` through the existing orchestrator loop.

Each scenario becomes an ``ExperimentPlan`` (its frozen fault + the oracle
plugin) and runs through ``ExperimentRunner`` unchanged. The resulting
``ExperimentRecord`` is mapped to a ``RegressionVerdict`` and the whole run is
summarized as a ``SuiteRunRecord`` with a coverage matrix.

The runner is injected as a factory ``(ExperimentPlugin) -> ScenarioRunner`` so
tests can drive the suite with a fake runner (no real agents / fault injection).
``SuiteRunner.with_agents`` wires the production ``ExperimentRunner``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from plugins.base import ExperimentPlugin
from regression.coverage import CoverageReporter
from regression.oracles.command import CommandOraclePlugin
from regression.oracles.metric import MetricOraclePlugin
from regression.oracles.playwright import PlaywrightOraclePlugin
from shared.contracts import (
    AbortReason,
    ExperimentPlan,
    ExperimentRecord,
    ExperimentState,
    OracleKind,
    RegressionOutcome,
    RegressionScenario,
    RegressionSuite,
    RegressionVerdict,
    SuiteRunRecord,
)

if TYPE_CHECKING:
    from orchestrator.loop import Agents
    from orchestrator.store import ExperimentStore

# Oracle kind -> plugin class. NEGATIVE (v3) is intentionally absent.
_ORACLE_PLUGINS: dict[OracleKind, type[ExperimentPlugin]] = {
    OracleKind.PLAYWRIGHT: PlaywrightOraclePlugin,
    OracleKind.COMMAND: CommandOraclePlugin,
    OracleKind.METRIC: MetricOraclePlugin,
}


class ScenarioRunner(Protocol):
    async def run(self, plan: ExperimentPlan) -> ExperimentRecord: ...


RunnerFactory = Callable[[ExperimentPlugin], ScenarioRunner]


class SuiteRunner:
    def __init__(self, store: ExperimentStore, runner_factory: RunnerFactory) -> None:
        self.store = store
        self.runner_factory = runner_factory

    @classmethod
    def with_agents(
        cls,
        agents: Agents,
        store: ExperimentStore,
        *,
        harness: object | None = None,
    ) -> SuiteRunner:
        from orchestrator.loop import ExperimentRunner

        def factory(plugin: ExperimentPlugin) -> ScenarioRunner:
            return ExperimentRunner(agents, store, harness=harness, plugin=plugin)

        return cls(store, factory)

    async def run(
        self,
        suite: RegressionSuite,
        *,
        plugin_config_overrides: dict[str, object] | None = None,
        on_progress: Callable[[int, int, RegressionVerdict], None] | None = None,
    ) -> SuiteRunRecord:
        """Replay every scenario. ``on_progress(done, total, verdict)`` fires as
        each scenario completes so a long run can report live rather than going
        silent until the end."""
        overrides = plugin_config_overrides or {}
        total = len(suite.scenarios)
        record = SuiteRunRecord(suite_id=suite.suite_id)
        verdicts: list[RegressionVerdict] = []
        for index, scenario in enumerate(suite.scenarios, start=1):
            plugin = self._oracle_for(scenario)
            runner = self.runner_factory(plugin)
            plan = self._plan_for(suite, scenario, plugin.name, overrides)
            exp = await runner.run(plan)
            verdict = self._verdict(scenario, exp)
            verdicts.append(verdict)
            if on_progress is not None:
                on_progress(index, total, verdict)
        record.verdicts = verdicts
        record.coverage = CoverageReporter().render(suite)
        record.finished_at = datetime.now(tz=UTC)
        self.store.save_suite_run(record)
        return record

    # ----- helpers ---------------------------------------------------------
    def _oracle_for(self, scenario: RegressionScenario) -> ExperimentPlugin:
        plugin_cls = _ORACLE_PLUGINS.get(scenario.oracle)
        if plugin_cls is None:
            raise ValueError(
                f"oracle {scenario.oracle.value!r} is not implemented in v1 "
                f"(available: {sorted(k.value for k in _ORACLE_PLUGINS)})"
            )
        return plugin_cls()

    def _plan_for(
        self,
        suite: RegressionSuite,
        scenario: RegressionScenario,
        plugin_name: str,
        overrides: dict[str, object],
    ) -> ExperimentPlan:
        # The oracle needs the scenario's journeys (to scope its `--grep`); the
        # suite budget applies per scenario. Overrides carry run-time flags
        # (e.g. `_dry_run`) the CLI injects.
        plugin_config: dict[str, object] = {
            **scenario.oracle_config,
            "journeys": list(scenario.journeys),
            **overrides,
        }
        return ExperimentPlan(
            title=scenario.title,
            target_app=suite.target_app,
            target_repo=suite.target_repo,
            faults=[scenario.fault],
            safety=suite.safety,
            budget=suite.budget,
            plugin=plugin_name,
            plugin_config=plugin_config,
        )

    def _verdict(
        self, scenario: RegressionScenario, exp: ExperimentRecord
    ) -> RegressionVerdict:
        outcome = _classify(exp)
        newly = _newly_failing(exp)
        return RegressionVerdict(
            scenario_id=scenario.scenario_id,
            title=scenario.title,
            fault=scenario.fault.name,
            experiment_id=exp.experiment_id,
            outcome=outcome,
            newly_failing=newly,
            verify_result=exp.verify_result,
            detail=exp.abort_detail,
        )


def _classify(exp: ExperimentRecord) -> RegressionOutcome:
    """Derive the regression outcome from a completed ExperimentRecord.

    The loop collapses both STEADY and REGRESSED to a terminal RECORDED state
    (and BASELINE_FAIL to ABORTED), so the outcome is read from the abort reason
    and the verify signals, never from ``exp.state`` alone.
    """
    if exp.state == ExperimentState.ABORTED:
        if exp.abort_reason == AbortReason.BASELINE_UNHEALTHY:
            return RegressionOutcome.BASELINE_FAIL
        return RegressionOutcome.ERROR
    # The oracle can report that its own baseline was already broken (the
    # customer's suite was red before the fault) — that's BASELINE_FAIL, not a
    # PASS over an empty delta. (The built-in tester/security baseline is gated
    # separately inside the loop and surfaces as ABORTED above.)
    vr = exp.verify_result
    if vr is not None and vr.evidence.get("baseline_unassessable"):
        return RegressionOutcome.BASELINE_FAIL
    return RegressionOutcome.REGRESSED if _is_regressed(exp) else RegressionOutcome.PASS


def _is_regressed(exp: ExperimentRecord) -> bool:
    """Whether the scenario regressed.

    In a regression suite the **oracle is authoritative** — the customer's suite
    defines what "working" means — so its ``verify_result`` decides the verdict
    when present. The built-in tester/security signals are only a fallback for a
    scenario that somehow ran without an oracle verdict. (This deliberately
    diverges from the loop's own OR-of-all-signals predicate, which is right for
    discovery but would let the built-in tester override the customer's oracle
    here.)
    """
    vr = exp.verify_result
    if vr is not None:
        return not vr.passed
    tv = exp.tester_verify
    if tv is not None and not tv.steady_state:
        return True
    sv = exp.security_verify
    return sv is not None and (sv.has_critical_or_high or sv.sbom_drift_from_baseline)


def _newly_failing(exp: ExperimentRecord) -> list[str]:
    vr = exp.verify_result
    if vr is None:
        return []
    return [str(x) for x in vr.evidence.get("newly_failing", [])]
