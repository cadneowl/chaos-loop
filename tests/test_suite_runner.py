"""SuiteRunner: maps ExperimentRecords to verdicts and persists the run.

Uses a fake runner (no real agents / fault injection). The key behavior under
test is outcome classification, since the loop collapses STEADY/REGRESSED to a
terminal RECORDED state and BASELINE_FAIL to ABORTED — so the verdict must be
read from verify signals + abort reason, not from the final state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.store import ExperimentStore
from regression.suite_runner import SuiteRunner
from shared.contracts import (
    AbortReason,
    ExperimentPlan,
    ExperimentRecord,
    ExperimentState,
    FaultCategory,
    FaultSpec,
    OracleKind,
    RegressionOutcome,
    RegressionScenario,
    RegressionSuite,
    SafetyConstraints,
    VerifyResult,
)


def _fault(name: str, category: FaultCategory = FaultCategory.POD) -> FaultSpec:
    return FaultSpec(
        category=category,
        name=name,
        target_selector={"app": "x"},
        duration_seconds=1,
        rationale="r",
    )


def _scenario(
    title: str,
    fault_name: str,
    journeys: list[str],
    category: FaultCategory = FaultCategory.POD,
) -> RegressionScenario:
    return RegressionScenario(
        title=title,
        fault=_fault(fault_name, category),
        oracle=OracleKind.COMMAND,
        journeys=journeys,
    )


def _suite(scenarios: list[RegressionScenario]) -> RegressionSuite:
    return RegressionSuite(
        name="s",
        target_app="app",
        safety=SafetyConstraints(cluster_context="kind-test", namespace="default"),
        scenarios=scenarios,
        all_journeys=["a.spec:x", "a.spec:y", "a.spec:z"],
    )


def _record(state: ExperimentState, **kw: object) -> ExperimentRecord:
    plan = ExperimentPlan(title="t", target_app="app", faults=[_fault("pod.kill")],
                          safety=SafetyConstraints(cluster_context="kind-test", namespace="default"))
    return ExperimentRecord(experiment_id=plan.experiment_id, plan=plan, state=state, **kw)  # type: ignore[arg-type]


class _FakeRunner:
    def __init__(self, record: ExperimentRecord) -> None:
        self._record = record
        self.seen_plan: ExperimentPlan | None = None

    async def run(self, plan: ExperimentPlan) -> ExperimentRecord:
        self.seen_plan = plan
        return self._record


def _factory_over(records: list[ExperimentRecord]) -> object:
    queue = iter(records)

    def factory(_plugin: object) -> _FakeRunner:
        return _FakeRunner(next(queue))

    return factory


async def test_outcome_classification(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "db.sqlite")
    suite = _suite(
        [
            _scenario("passes", "pod.kill", ["a.spec:x"], FaultCategory.POD),
            _scenario("regresses", "network.delay", ["a.spec:y"], FaultCategory.NETWORK),
            _scenario("baseline broken", "io.latency", ["a.spec:z"], FaultCategory.IO),
            _scenario("errors", "dns.error", ["a.spec:x"], FaultCategory.DNS),
        ]
    )
    records = [
        _record(ExperimentState.RECORDED, verify_result=VerifyResult(passed=True)),
        _record(
            ExperimentState.RECORDED,
            verify_result=VerifyResult(
                passed=False, evidence={"newly_failing": ["a.spec:y"]}
            ),
        ),
        _record(ExperimentState.ABORTED, abort_reason=AbortReason.BASELINE_UNHEALTHY),
        _record(ExperimentState.ABORTED, abort_reason=AbortReason.AGENT_FAILURE),
    ]
    runner = SuiteRunner(store, _factory_over(records))  # type: ignore[arg-type]

    run = await runner.run(suite)

    assert [v.outcome for v in run.verdicts] == [
        RegressionOutcome.PASS,
        RegressionOutcome.REGRESSED,
        RegressionOutcome.BASELINE_FAIL,
        RegressionOutcome.ERROR,
    ]
    assert run.verdicts[1].newly_failing == ["a.spec:y"]
    # Verdicts carry human-readable title + fault for at-a-glance results.
    assert run.verdicts[0].title == "passes"
    assert run.verdicts[1].fault == "network.delay"
    # Coverage matrix rendered and attached.
    assert run.coverage is not None
    assert run.coverage.covered == 4  # one covered cell per scenario's (fault, journey)


async def test_suite_run_is_persisted(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "db.sqlite")
    suite = _suite([_scenario("passes", "pod.kill", ["a.spec:x"])])
    records = [_record(ExperimentState.RECORDED, verify_result=VerifyResult(passed=True))]
    runner = SuiteRunner(store, _factory_over(records))  # type: ignore[arg-type]

    run = await runner.run(suite)
    loaded = store.load_suite_run(run.suite_run_id)

    assert loaded is not None
    assert loaded.suite_run_id == run.suite_run_id
    assert [v.outcome for v in loaded.verdicts] == [RegressionOutcome.PASS]
    assert loaded.finished_at is not None


async def test_on_progress_fires_once_per_scenario(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "db.sqlite")
    suite = _suite(
        [
            _scenario("a", "pod.kill", ["a.spec:x"]),
            _scenario("b", "pod.failure", ["a.spec:y"]),
        ]
    )
    records = [
        _record(ExperimentState.RECORDED, verify_result=VerifyResult(passed=True))
        for _ in range(2)
    ]
    runner = SuiteRunner(store, _factory_over(records))  # type: ignore[arg-type]

    seen: list[tuple[int, int, str]] = []
    await runner.run(
        suite, on_progress=lambda done, total, v: seen.append((done, total, v.title))
    )
    assert seen == [(1, 2, "a"), (2, 2, "b")]


def test_baseline_unassessable_classifies_as_baseline_fail() -> None:
    from regression.suite_runner import _classify

    exp = _record(
        ExperimentState.RECORDED,
        verify_result=VerifyResult(
            passed=True, evidence={"baseline_unassessable": True, "newly_failing": []}
        ),
    )
    assert _classify(exp) == RegressionOutcome.BASELINE_FAIL


def test_oracle_verdict_overrides_builtin_tester() -> None:
    from regression.suite_runner import _is_regressed
    from shared.contracts import TesterReport

    # The oracle passed but the built-in tester flagged non-steady-state. In a
    # regression suite the customer's oracle wins -> not a regression.
    exp = _record(
        ExperimentState.RECORDED,
        verify_result=VerifyResult(passed=True),
        tester_verify=TesterReport(
            request_kind="verify",
            experiment_id="exp-000000000000",
            steady_state=False,
        ),
    )
    assert _is_regressed(exp) is False


def test_unimplemented_oracle_raises(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "db.sqlite")
    # NEGATIVE (must-not-happen assertions) is the remaining unimplemented kind.
    scenario = RegressionScenario(
        title="negative", fault=_fault("pod.kill"), oracle=OracleKind.NEGATIVE
    )
    runner = SuiteRunner(store, _factory_over([]))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not implemented in v1"):
        runner._oracle_for(scenario)
