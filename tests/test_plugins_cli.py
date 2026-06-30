"""CLI surface: `chaos plugins list` and `chaos run --plugin ...`."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from orchestrator.main import app

runner = CliRunner()


def test_plugins_list_shows_examples() -> None:
    result = runner.invoke(app, ["plugins", "list"])
    assert result.exit_code == 0
    assert "example-keyvalue" in result.stdout
    assert "example-web-service" in result.stdout


def test_run_unknown_plugin_errors() -> None:
    plan = Path("experiments/examples/04-plugin-keyvalue.yaml")
    result = runner.invoke(
        app, ["run", str(plan), "--dry-run", "--plugin", "does-not-exist"]
    )
    # typer.BadParameter -> click UsageError -> non-zero exit, and no record is
    # emitted to stdout. (The "unknown plugin" message text — which lands on
    # stdout or stderr depending on the click version — is asserted in
    # test_plugins_registry.py::test_load_unknown_raises_with_hint.)
    assert result.exit_code != 0
    assert not result.output.strip().startswith("{")


def test_run_with_plugin_dry_run(tmp_path: Path) -> None:
    plan = Path("experiments/examples/04-plugin-keyvalue.yaml")
    db = tmp_path / "exp.sqlite"
    result = runner.invoke(
        app,
        [
            "run",
            str(plan),
            "--dry-run",
            "--plugin",
            "example-keyvalue",
            "--db",
            str(db),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["plugin_name"] == "example-keyvalue"
    stages = {s["stage"] for s in payload["plugin_stage_results"]}
    # The full lifecycle ran.
    assert {"provision_env", "seed", "verify", "teardown_env"} <= stages


def test_run_plugin_from_plan_without_flag(tmp_path: Path) -> None:
    """plan.plugin is honored when --plugin is omitted."""
    db = tmp_path / "exp.sqlite"
    plan = Path("experiments/examples/05-plugin-web-service.yaml")
    result = runner.invoke(app, ["run", str(plan), "--dry-run", "--db", str(db)])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["plugin_name"] == "example-web-service"
