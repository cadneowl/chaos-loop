"""Playwright oracle: JSON parsing and the newly-failing delta.

The subprocess boundary (``_exec_playwright``) is monkeypatched with fixture
JSON, so these exercise the parse + delta logic without Node installed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from plugins.base import PluginContext
from regression.oracles.playwright import (
    PlaywrightOraclePlugin,
    _derive_grep,
    list_journeys,
    parse_playwright_json,
)
from shared.contracts import (
    ExperimentPlan,
    FaultCategory,
    FaultSpec,
    SafetyConstraints,
)


def _pw(specs: list[tuple[str, str, str]]) -> dict[str, Any]:
    """Build a playwright-json dump from (file, title, status) triples."""
    return {
        "suites": [
            {
                "file": file,
                "specs": [
                    {
                        "title": title,
                        "ok": status == "passed",
                        "tests": [{"results": [{"status": status}]}],
                    }
                ],
            }
            for (file, title, status) in specs
        ]
    }


def _ctx(config: dict[str, Any]) -> PluginContext:
    plan = ExperimentPlan(
        title="t",
        target_app="t",
        faults=[
            FaultSpec(
                category=FaultCategory.POD,
                name="pod.kill",
                target_selector={"app": "x"},
                duration_seconds=1,
                rationale="r",
            )
        ],
        safety=SafetyConstraints(cluster_context="kind-test", namespace="default"),
    )
    return PluginContext(experiment_id=plan.experiment_id, plan=plan, config=config)


def _stub_exec(
    plugin: PlaywrightOraclePlugin, responses: list[dict[str, Any]]
) -> None:
    queue = list(responses)

    async def fake(_config: dict[str, Any]) -> dict[str, Any]:
        return queue.pop(0)

    exec_fn: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] = fake
    plugin._exec_playwright = exec_fn  # type: ignore[method-assign]


# ----- parser ---------------------------------------------------------------


def test_parse_classifies_and_ignores_skipped() -> None:
    data = _pw(
        [
            ("a.spec", "x", "passed"),
            ("a.spec", "y", "failed"),
            ("a.spec", "z", "skipped"),
        ]
    )
    result = parse_playwright_json(data)
    assert result.passed_ids == ["a.spec:x"]
    assert result.failed_ids == ["a.spec:y"]
    assert result.all_ids == ["a.spec:x", "a.spec:y"]  # skipped excluded


def test_list_journeys_dedupes_and_sorts() -> None:
    data = _pw([("b.spec", "n", "passed"), ("a.spec", "m", "failed")])
    assert list_journeys(data) == ["a.spec:m", "b.spec:n"]


def test_list_journeys_includes_skipped_and_unrun() -> None:
    # `playwright --list` doesn't run tests, so specs are skipped / have no
    # results — they must still be enumerated (they're journeys that exist).
    data = {
        "suites": [
            {
                "file": "a.spec",
                "specs": [
                    {"title": "x", "tests": [{"results": [{"status": "skipped"}]}]},
                    {"title": "y", "tests": [{"results": []}]},
                ],
            }
        ]
    }
    assert list_journeys(data) == ["a.spec:x", "a.spec:y"]


# ----- delta ----------------------------------------------------------------


async def test_newly_failing_is_regression() -> None:
    plugin = PlaywrightOraclePlugin()
    _stub_exec(
        plugin,
        [
            _pw([("a.spec", "x", "passed"), ("a.spec", "y", "passed")]),  # baseline green
            _pw([("a.spec", "x", "passed"), ("a.spec", "y", "failed")]),  # y red under fault
        ],
    )
    ctx = _ctx({})
    await plugin.capture_baseline(ctx)
    result = await plugin.verify(ctx)
    assert result is not None
    assert result.passed is False
    assert result.evidence["newly_failing"] == ["a.spec:y"]
    assert [f.assertion for f in result.failures] == ["a.spec:y"]


async def test_all_asserted_red_at_baseline_is_unassessable() -> None:
    plugin = PlaywrightOraclePlugin()
    # Only ONE stubbed run: verify must short-circuit (not run the suite again).
    _stub_exec(plugin, [_pw([("a.spec", "x", "failed")])])
    ctx = _ctx({"journeys": ["a.spec:x"]})
    await plugin.capture_baseline(ctx)
    result = await plugin.verify(ctx)
    assert result is not None
    assert result.evidence["baseline_unassessable"] is True
    assert result.evidence["baseline_failed"] == ["a.spec:x"]


async def test_already_red_at_baseline_is_not_a_regression() -> None:
    plugin = PlaywrightOraclePlugin()
    _stub_exec(
        plugin,
        [
            _pw([("a.spec", "x", "failed")]),  # already red before the fault
            _pw([("a.spec", "x", "failed")]),  # still red — not *newly* failing
        ],
    )
    ctx = _ctx({})
    await plugin.capture_baseline(ctx)
    result = await plugin.verify(ctx)
    assert result is not None
    assert result.passed is True
    assert result.evidence["newly_failing"] == []


async def test_quarantine_suppresses_a_flaky_journey() -> None:
    plugin = PlaywrightOraclePlugin()
    _stub_exec(
        plugin,
        [
            _pw([("a.spec", "x", "passed"), ("a.spec", "y", "passed")]),
            _pw([("a.spec", "x", "passed"), ("a.spec", "y", "failed")]),
        ],
    )
    ctx = _ctx({"quarantine": ["a.spec:y"]})
    await plugin.capture_baseline(ctx)
    result = await plugin.verify(ctx)
    assert result is not None
    assert result.passed is True  # y quarantined out of the delta


async def test_dry_run_stub_passes_without_exec() -> None:
    plugin = PlaywrightOraclePlugin()
    # No _exec stub installed: if the stub path shelled out, this would error.
    ctx = _ctx({"_dry_run": True, "journeys": ["a.spec:x", "a.spec:y"]})
    await plugin.capture_baseline(ctx)
    result = await plugin.verify(ctx)
    assert result is not None
    assert result.passed is True


# ----- grep derivation ------------------------------------------------------


def test_derive_grep_ors_escaped_titles() -> None:
    # Titles are OR'd; regex-special chars are escaped (the '.' below), spaces are not.
    assert _derive_grep(["a.spec:pay v2.0", "a.spec:checkout"]) == "pay\\ v2\\.0|checkout"


def test_derive_grep_none_for_no_journeys() -> None:
    assert _derive_grep([]) is None
