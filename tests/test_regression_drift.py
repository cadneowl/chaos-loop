"""Chronic drift axis: baseline_passing extraction, drift computation, storage.

Drift = journeys green at baseline in the golden that are red at baseline now
(a steady-state regression the under-fault delta can't see).
"""

from __future__ import annotations

from pathlib import Path

from orchestrator.store import ExperimentStore
from regression.drift import (
    baseline_passing,
    baseline_trustworthy,
    compute_scenario_drift,
    drift_report,
    goldens_from_run,
)
from shared.contracts import (
    Golden,
    RegressionOutcome,
    RegressionVerdict,
    SuiteRunRecord,
    VerifyResult,
)


def _verdict(
    scenario_id: str,
    title: str,
    passing: list[str],
    outcome: RegressionOutcome = RegressionOutcome.PASS,
) -> RegressionVerdict:
    return RegressionVerdict(
        scenario_id=scenario_id,
        title=title,
        experiment_id="exp-000000000000",
        outcome=outcome,
        verify_result=VerifyResult(passed=True, evidence={"baseline_passing": passing}),
    )


# ----- extraction + computation ---------------------------------------------


def test_baseline_passing_reads_evidence() -> None:
    assert baseline_passing(_verdict("scn-000000000001", "t", ["a", "b"])) == ["a", "b"]
    v = RegressionVerdict(
        scenario_id="scn-000000000001",
        experiment_id="exp-000000000000",
        outcome=RegressionOutcome.ERROR,
    )
    assert baseline_passing(v) == []


def test_compute_scenario_drift_regressed_and_recovered() -> None:
    golden = Golden(target_ref="v1", passing_journeys=["a", "b", "c"])
    d = compute_scenario_drift(golden, ["a", "d"], scenario_id="scn-000000000001")
    assert d.regressed == ["b", "c"]  # green then, gone now
    assert d.recovered == ["d"]  # new green
    assert d.drifted is True


def test_compute_scenario_drift_stable() -> None:
    golden = Golden(target_ref="v1", passing_journeys=["a", "b"])
    d = compute_scenario_drift(golden, ["b", "a"], scenario_id="scn-000000000001")
    assert d.regressed == []
    assert d.drifted is False


def test_compute_scenario_drift_missing_golden() -> None:
    d = compute_scenario_drift(None, ["a"], scenario_id="scn-000000000001")
    assert d.missing_golden is True
    assert d.drifted is False


def test_goldens_from_run_and_drift_report() -> None:
    run = SuiteRunRecord(
        suite_id="suite-000000000001",
        verdicts=[
            _verdict("scn-000000000001", "cart", ["cart:add", "cart:pay"]),
            _verdict("scn-000000000002", "browse", ["browse:list"]),
        ],
    )
    goldens = goldens_from_run(run, "v1")
    assert goldens["scn-000000000001"].passing_journeys == ["cart:add", "cart:pay"]

    # A later run where cart:pay went red at baseline.
    later = SuiteRunRecord(
        suite_id="suite-000000000001",
        verdicts=[
            _verdict("scn-000000000001", "cart", ["cart:add"]),
            _verdict("scn-000000000002", "browse", ["browse:list"]),
        ],
    )
    report = drift_report(later, goldens, "v1")
    assert report.regressed_scenarios == 1
    drifted = {s.scenario_id: s for s in report.scenarios}
    assert drifted["scn-000000000001"].regressed == ["cart:pay"]
    assert drifted["scn-000000000002"].drifted is False


# ----- trustworthiness: don't freeze / compare an unmeasured baseline -------


def test_baseline_trustworthy_by_outcome() -> None:
    assert baseline_trustworthy(_verdict("scn-000000000001", "t", [])) is True
    assert (
        baseline_trustworthy(
            _verdict("scn-000000000001", "t", [], RegressionOutcome.REGRESSED)
        )
        is True
    )
    for bad in (RegressionOutcome.ERROR, RegressionOutcome.BASELINE_FAIL):
        assert baseline_trustworthy(_verdict("scn-000000000001", "t", [], bad)) is False


def test_goldens_from_run_skips_unclean_scenarios() -> None:
    run = SuiteRunRecord(
        suite_id="suite-000000000001",
        verdicts=[
            _verdict("scn-000000000001", "cart", ["cart:pay"]),
            _verdict("scn-000000000002", "browse", [], RegressionOutcome.BASELINE_FAIL),
            _verdict("scn-000000000003", "search", [], RegressionOutcome.ERROR),
        ],
    )
    goldens = goldens_from_run(run, "v1")
    # Only the cleanly-measured scenario is frozen — the empty/partial baselines
    # of the BASELINE_FAIL / ERROR scenarios are NOT poisoned into goldens.
    assert set(goldens) == {"scn-000000000001"}


def test_drift_report_flags_unassessed_instead_of_false_regression() -> None:
    goldens = {"scn-000000000001": Golden(target_ref="v1", passing_journeys=["a", "b"])}
    # Current run: the scenario errored, so its baseline_passing is empty. Without
    # the guard this would report a,b as falsely regressed.
    run = SuiteRunRecord(
        suite_id="suite-000000000001",
        verdicts=[_verdict("scn-000000000001", "cart", [], RegressionOutcome.ERROR)],
    )
    report = drift_report(run, goldens, "v1")
    assert report.regressed_scenarios == 0
    s = report.scenarios[0]
    assert s.unassessed is True
    assert s.regressed == []
    assert s.drifted is False


# ----- storage --------------------------------------------------------------


def test_golden_store_round_trip(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "db.sqlite")
    goldens = {
        "scn-000000000001": Golden(target_ref="v1", passing_journeys=["a", "b"]),
        "scn-000000000002": Golden(target_ref="v1", passing_journeys=["c"]),
    }
    store.save_goldens("suite-000000000001", "v1", goldens)

    loaded = store.load_goldens("suite-000000000001", "v1")
    assert loaded["scn-000000000001"].passing_journeys == ["a", "b"]
    assert store.load_goldens("suite-000000000001", "other") == {}

    refs = store.golden_refs("suite-000000000001")
    assert refs[0][0] == "v1"
    assert refs[0][1] == 2  # two scenarios


def test_save_goldens_upserts(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "db.sqlite")
    sid = "suite-000000000001"
    store.save_goldens(sid, "v1", {"scn-000000000001": Golden(target_ref="v1", passing_journeys=["a"])})
    store.save_goldens(sid, "v1", {"scn-000000000001": Golden(target_ref="v1", passing_journeys=["a", "b"])})
    loaded = store.load_goldens(sid, "v1")
    assert loaded["scn-000000000001"].passing_journeys == ["a", "b"]
    assert len(store.golden_refs(sid)) == 1  # still one ref, not duplicated
