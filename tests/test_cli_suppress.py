"""Tests for the `chaos suppress` Typer sub-app."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from orchestrator.main import app
from orchestrator.store import ExperimentStore
from shared.contracts import (
    DiagnosisReport,
    ExperimentPlan,
    ExperimentRecord,
    ExperimentState,
    FaultSpec,
    RootCauseHypothesis,
    SafetyConstraints,
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _record_with_hypotheses(
    experiment_id: str,
    hypotheses: list[RootCauseHypothesis],
) -> ExperimentRecord:
    plan = ExperimentPlan(
        experiment_id=experiment_id,
        title="t",
        target_app="otel-demo",
        faults=[
            FaultSpec(
                category="network",  # type: ignore[arg-type]
                name="network.loss",
                target_selector={"app": "x"},
                parameters={},
                duration_seconds=10,
                requires_approval=False,
                rationale="r",
            )
        ],
        safety=SafetyConstraints(
            cluster_context="kind-chaos-dev",
            namespace="otel-demo",
            require_namespace_annotation=False,
        ),
    )
    return ExperimentRecord(
        experiment_id=experiment_id,
        plan=plan,
        state=ExperimentState.RECORDED,
        started_at=datetime.now(tz=UTC),
        diagnosis=DiagnosisReport(
            experiment_id=experiment_id,
            hypotheses=hypotheses,
        ),
    )


def _hypothesis(summary: str = "cart Redis dep") -> RootCauseHypothesis:
    return RootCauseHypothesis(
        summary=summary,
        confidence=0.8,
        evidence=[],
        suggested_fix_class="missing-retry",  # type: ignore[arg-type]
        affected_paths=["services/cart/redis_client.py"],
    )


def _seed(db_path: Path, record: ExperimentRecord) -> ExperimentStore:
    store = ExperimentStore(db_path)
    store.save(record)
    return store


# ----------------------------------------------------------- suppress list


def test_list_empty_when_no_file(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["suppress", "list"], catch_exceptions=False)
    # Run from the tmp dir so CWD doesn't pick up the repo's .chaos/.
    # Typer's CliRunner doesn't support cwd directly — chdir via monkeypatch
    # by passing through `cwd` env, but easier: just hit the function with
    # a directory we control.
    import os

    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = runner.invoke(app, ["suppress", "list"], catch_exceptions=False)
    finally:
        os.chdir(cwd)
    assert result.exit_code == 0
    assert "no suppression rules" in result.stdout


def test_list_renders_rules(runner: CliRunner, tmp_path: Path) -> None:
    (tmp_path / ".chaos").mkdir()
    (tmp_path / ".chaos" / "suppress.yaml").write_text(
        "rules:\n  - fix_class: missing-retry\n    reason: tracked in JIRA-1234\n",
        encoding="utf-8",
    )
    import os

    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = runner.invoke(app, ["suppress", "list"], catch_exceptions=False)
    finally:
        os.chdir(cwd)
    assert result.exit_code == 0
    assert "missing-retry" in result.stdout
    assert "JIRA-1234" in result.stdout


# ----------------------------------------------------------- suppress add


def test_add_appends_rule_to_new_file(runner: CliRunner, tmp_path: Path) -> None:
    db = tmp_path / "experiments.sqlite"
    _seed(db, _record_with_hypotheses("exp-aaaaaaaaaaaa", [_hypothesis()]))

    import os

    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "suppress",
                "add",
                "exp-aaaaaaaaaaaa",
                "1",
                "--db",
                str(db),
                "--reason",
                "tracked in JIRA-1234",
            ],
            catch_exceptions=False,
        )
    finally:
        os.chdir(cwd)
    assert result.exit_code == 0, result.stdout

    suppress_file = tmp_path / ".chaos" / "suppress.yaml"
    assert suppress_file.exists()
    parsed = yaml.safe_load(suppress_file.read_text(encoding="utf-8"))
    assert len(parsed["rules"]) == 1
    rule = parsed["rules"][0]
    assert rule["hypothesis_id"] == _hypothesis().id
    assert rule["reason"] == "tracked in JIRA-1234"


def test_add_appends_to_existing_file(runner: CliRunner, tmp_path: Path) -> None:
    (tmp_path / ".chaos").mkdir()
    (tmp_path / ".chaos" / "suppress.yaml").write_text(
        "rules:\n  - fix_class: missing-timeout\n    reason: old rule\n",
        encoding="utf-8",
    )
    db = tmp_path / "experiments.sqlite"
    _seed(db, _record_with_hypotheses("exp-aaaaaaaaaaaa", [_hypothesis()]))

    import os

    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["suppress", "add", "exp-aaaaaaaaaaaa", "1", "--db", str(db)],
            catch_exceptions=False,
        )
    finally:
        os.chdir(cwd)
    assert result.exit_code == 0, result.stdout

    parsed = yaml.safe_load(
        (tmp_path / ".chaos" / "suppress.yaml").read_text(encoding="utf-8")
    )
    assert len(parsed["rules"]) == 2
    # Old rule preserved, new rule appended.
    assert parsed["rules"][0]["fix_class"] == "missing-timeout"
    assert parsed["rules"][1]["hypothesis_id"] == _hypothesis().id


def test_add_rejects_missing_experiment(runner: CliRunner, tmp_path: Path) -> None:
    db = tmp_path / "experiments.sqlite"
    ExperimentStore(db)  # create empty store

    import os

    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["suppress", "add", "exp-doesnotexist", "1", "--db", str(db)],
            catch_exceptions=False,
        )
    finally:
        os.chdir(cwd)
    assert result.exit_code == 1
    assert "no experiment" in result.stdout


def test_add_rejects_index_out_of_range(runner: CliRunner, tmp_path: Path) -> None:
    db = tmp_path / "experiments.sqlite"
    _seed(db, _record_with_hypotheses("exp-aaaaaaaaaaaa", [_hypothesis()]))

    import os

    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = runner.invoke(
            app,
            ["suppress", "add", "exp-aaaaaaaaaaaa", "9", "--db", str(db)],
            catch_exceptions=False,
        )
    finally:
        os.chdir(cwd)
    assert result.exit_code == 1
    assert "out of range" in result.stdout


def test_add_with_expires_serializes_datetime(runner: CliRunner, tmp_path: Path) -> None:
    db = tmp_path / "experiments.sqlite"
    _seed(db, _record_with_hypotheses("exp-aaaaaaaaaaaa", [_hypothesis()]))

    import os

    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "suppress",
                "add",
                "exp-aaaaaaaaaaaa",
                "1",
                "--db",
                str(db),
                "--expires",
                "2026-12-31T00:00:00Z",
            ],
            catch_exceptions=False,
        )
    finally:
        os.chdir(cwd)
    assert result.exit_code == 0, result.stdout

    parsed = yaml.safe_load(
        (tmp_path / ".chaos" / "suppress.yaml").read_text(encoding="utf-8")
    )
    assert "expires_at" in parsed["rules"][0]
    assert parsed["rules"][0]["expires_at"].startswith("2026-12-31")


def test_add_rejects_garbage_expires(runner: CliRunner, tmp_path: Path) -> None:
    db = tmp_path / "experiments.sqlite"
    _seed(db, _record_with_hypotheses("exp-aaaaaaaaaaaa", [_hypothesis()]))

    import os

    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = runner.invoke(
            app,
            [
                "suppress",
                "add",
                "exp-aaaaaaaaaaaa",
                "1",
                "--db",
                str(db),
                "--expires",
                "not-a-date",
            ],
            catch_exceptions=False,
        )
    finally:
        os.chdir(cwd)
    assert result.exit_code == 1
    assert "invalid --expires" in result.stdout
