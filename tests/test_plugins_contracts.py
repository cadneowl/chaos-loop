"""Contract invariants for the plugin types."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from shared.contracts import (
    ExperimentPlan,
    ExperimentRecord,
    ExperimentState,
    FaultCategory,
    FaultSpec,
    FindingSeverity,
    LifecycleStage,
    SafetyConstraints,
    StageResult,
    StageStatus,
    VerifyFailure,
    VerifyResult,
)


def _plan(**extra: object) -> ExperimentPlan:
    return ExperimentPlan(
        title="t",
        target_app="app",
        faults=[
            FaultSpec(
                category=FaultCategory.NETWORK,
                name="network.loss",
                target_selector={"app": "app"},
                duration_seconds=1,
                rationale="r",
            )
        ],
        safety=SafetyConstraints(cluster_context="kind-dev", namespace="demo"),
        **extra,
    )


def test_plan_plugin_fields_default_empty() -> None:
    plan = _plan()
    assert plan.plugin is None
    assert plan.plugin_config == {}


def test_plan_accepts_plugin_config() -> None:
    plan = _plan(plugin="example-keyvalue", plugin_config={"seed_keys": ["a"]})
    assert plan.plugin == "example-keyvalue"
    assert plan.plugin_config["seed_keys"] == ["a"]


def test_verify_result_passed_with_failures_rejected() -> None:
    with pytest.raises(ValidationError, match="cannot be True while failures"):
        VerifyResult(passed=True, failures=[VerifyFailure(assertion="x")])


def test_verify_result_failed_with_failures_ok() -> None:
    vr = VerifyResult(
        passed=False,
        summary="bad",
        failures=[VerifyFailure(assertion="x", expected="1", actual="2")],
    )
    assert vr.passed is False
    assert vr.failures[0].severity == FindingSeverity.MEDIUM  # default


def test_verify_result_passed_no_failures_ok() -> None:
    vr = VerifyResult(passed=True, summary="ok")
    assert vr.passed and not vr.failures


def test_verify_failure_requires_assertion() -> None:
    with pytest.raises(ValidationError):
        VerifyFailure(assertion="")


def test_stage_result_defaults() -> None:
    r = StageResult(stage=LifecycleStage.SEED, status=StageStatus.OK)
    assert r.error is None
    assert r.finished_at is None
    assert r.started_at is not None


def test_record_plugin_fields_default() -> None:
    record = ExperimentRecord(
        experiment_id="exp-000000000abc",
        plan=_plan(),
        state=ExperimentState.INITIALIZING,
    )
    assert record.plugin_name is None
    assert record.plugin_stage_results == []
    assert record.verify_result is None
    assert record.plugin_diagnostics == {}


def test_record_round_trips_plugin_data() -> None:
    """A record carrying plugin data survives model_dump -> model_validate."""
    record = ExperimentRecord(
        experiment_id="exp-000000000abc",
        plan=_plan(plugin="p"),
        state=ExperimentState.RECORDED,
        plugin_name="p",
        plugin_stage_results=[
            StageResult(stage=LifecycleStage.PROVISION_ENV, status=StageStatus.OK),
            StageResult(
                stage=LifecycleStage.VERIFY,
                status=StageStatus.FAILED,
                error="boom",
            ),
        ],
        verify_result=VerifyResult(
            passed=False,
            failures=[VerifyFailure(assertion="x", expected="1", actual="2")],
        ),
        plugin_diagnostics={"k": "v"},
    )
    dumped = record.model_dump(mode="json")
    restored = ExperimentRecord.model_validate(dumped)
    assert restored.plugin_name == "p"
    assert len(restored.plugin_stage_results) == 2
    assert restored.plugin_stage_results[1].status == StageStatus.FAILED
    assert restored.verify_result is not None and not restored.verify_result.passed
    assert restored.plugin_diagnostics == {"k": "v"}


def test_lifecycle_stage_covers_every_hook() -> None:
    """Every hook name maps to a LifecycleStage (guards against drift)."""
    from plugins.base import _HOOK_NAMES

    for hook in _HOOK_NAMES:
        assert LifecycleStage(hook).value == hook
