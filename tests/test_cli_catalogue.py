"""Tests for the `chaos catalogue verify` Typer sub-app."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from orchestrator.main import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_catalogue_verify_exits_zero_against_simulator(runner: CliRunner) -> None:
    """All currently-shipping hardware faults must pass verification end-to-end
    against SimulatedHardwareIO. This is the gate CI runs on every PR."""
    result = runner.invoke(app, ["catalogue", "verify"])
    assert result.exit_code == 0, result.stdout
    # One row per verified fault; spot-check that wifi.deauth and the three
    # new categories all appear.
    for name in (
        "wifi.deauth",
        "power.brownout",
        "sensor.dropout",
        "time.ntp.cut",
    ):
        assert name in result.stdout


def test_catalogue_verify_only_filters_to_named_faults(runner: CliRunner) -> None:
    result = runner.invoke(app, ["catalogue", "verify", "--only", "power.cut,sensor.stuck"])
    assert result.exit_code == 0, result.stdout
    assert "power.cut" in result.stdout
    assert "sensor.stuck" in result.stdout
    # A fault NOT in the filter shouldn't appear in the table.
    assert "wifi.deauth" not in result.stdout


def test_catalogue_verify_rejects_unknown_fault(runner: CliRunner) -> None:
    result = runner.invoke(app, ["catalogue", "verify", "--only", "power.bogus"])
    assert result.exit_code == 1
    assert "unknown" in result.stdout.lower()
