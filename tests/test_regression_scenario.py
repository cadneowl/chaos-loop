"""Regression suite YAML load / dump and the ergonomic expansions.

Covers: the on-disk ``oracle`` default + ``oracle_defaults`` merge, a full
round-trip through the contract, and that the shipped example suite validates.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from regression.scenario import (
    SuiteValidationError,
    dump_suite,
    load_suite,
    validate_suite,
)
from shared.contracts import OracleKind, RegressionSuite

_SUITE_YAML = """
name: checkout-resilience
target_app: shop
safety:
  cluster_context: kind-test
  namespace: shop
oracle: playwright
oracle_defaults:
  suite_path: ./e2e
  retries: 3
all_journeys: ["a.spec:x", "a.spec:y"]
scenarios:
  - title: survives redis latency
    fault:
      category: network
      name: network.delay
      target_selector: {app: redis}
      duration_seconds: 30
      rationale: cart depends on redis
    journeys: ["a.spec:x"]
    oracle_config:
      retries: 5
  - title: survives pod kill
    fault:
      category: pod
      name: pod.kill
      target_selector: {app: web}
      duration_seconds: 10
      rationale: web should reschedule
    journeys: ["a.spec:y"]
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "suite.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_default_oracle_and_defaults_merge(tmp_path: Path) -> None:
    suite = load_suite(_write(tmp_path, _SUITE_YAML))
    assert isinstance(suite, RegressionSuite)
    assert [s.title for s in suite.scenarios] == [
        "survives redis latency",
        "survives pod kill",
    ]
    # Both scenarios inherit the top-level oracle default.
    assert all(s.oracle == OracleKind.PLAYWRIGHT for s in suite.scenarios)
    # oracle_defaults merges in; scenario-level keys win (retries 5 not 3).
    assert suite.scenarios[0].oracle_config == {"suite_path": "./e2e", "retries": 5}
    # Scenario without its own oracle_config still gets the shared defaults.
    assert suite.scenarios[1].oracle_config == {"suite_path": "./e2e", "retries": 3}


def test_round_trip(tmp_path: Path) -> None:
    suite = load_suite(_write(tmp_path, _SUITE_YAML))
    reloaded = load_suite(_write(tmp_path, dump_suite(suite)))
    assert reloaded.model_dump() == suite.model_dump()


def test_non_mapping_yaml_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be a YAML mapping"):
        load_suite(_write(tmp_path, "- just\n- a\n- list\n"))


def test_suite_id_is_stable_from_name(tmp_path: Path) -> None:
    y = (
        "name: my-suite\ntarget_app: app\n"
        "safety: {cluster_context: kind-test, namespace: app}\n"
        "scenarios: []\nall_journeys: []\n"
    )
    first = load_suite(_write(tmp_path, y))
    second = load_suite(_write(tmp_path, y))
    # Same name -> same id across loads, so `regression list` can group history.
    assert first.suite_id == second.suite_id
    assert first.suite_id.startswith("suite-")


def test_scenario_ids_are_stable_across_loads(tmp_path: Path) -> None:
    # Goldens (chronic drift) are keyed by scenario_id — a random id per load
    # would orphan every golden and silently report no drift. Same file, two
    # loads -> identical, title-derived ids.
    first = load_suite(_write(tmp_path, _SUITE_YAML))
    second = load_suite(_write(tmp_path, _SUITE_YAML))
    assert [s.scenario_id for s in first.scenarios] == [
        s.scenario_id for s in second.scenarios
    ]
    assert all(s.scenario_id.startswith("scn-") for s in first.scenarios)
    # Distinct titles -> distinct ids (no collision).
    assert len({s.scenario_id for s in first.scenarios}) == len(first.scenarios)


def test_duplicate_scenario_titles_rejected(tmp_path: Path) -> None:
    y = (
        "name: s\ntarget_app: shop\n"
        "safety: {cluster_context: kind-test, namespace: shop}\n"
        'all_journeys: ["a.spec:x"]\n'
        "scenarios:\n"
        + "".join(
            "  - title: same\n"
            "    fault: {category: pod, name: pod.kill, "
            "target_selector: {app: web}, duration_seconds: 10, rationale: r}\n"
            '    journeys: ["a.spec:x"]\n'
            for _ in range(2)
        )
    )
    with pytest.raises(SuiteValidationError, match="duplicate title"):
        load_suite(_write(tmp_path, y))


def test_pinned_suite_id_is_respected(tmp_path: Path) -> None:
    y = (
        "suite_id: suite-abcabcabcabc\nname: x\ntarget_app: app\n"
        "safety: {cluster_context: kind-test, namespace: app}\n"
        "scenarios: []\nall_journeys: []\n"
    )
    assert load_suite(_write(tmp_path, y)).suite_id == "suite-abcabcabcabc"


_BAD_FAULT = """
name: s
target_app: shop
safety: {cluster_context: kind-test, namespace: shop}
all_journeys: ["a.spec:x"]
scenarios:
  - title: typo'd fault
    fault:
      category: pod
      name: pod.killl
      target_selector: {app: web}
      duration_seconds: 10
      rationale: r
    journeys: ["a.spec:x"]
"""

_BAD_JOURNEY = """
name: s
target_app: shop
safety: {cluster_context: kind-test, namespace: shop}
all_journeys: ["a.spec:x"]
scenarios:
  - title: journey not in denominator
    fault:
      category: pod
      name: pod.kill
      target_selector: {app: web}
      duration_seconds: 10
      rationale: r
    journeys: ["a.spec:typo"]
"""


def test_validate_rejects_unknown_fault(tmp_path: Path) -> None:
    with pytest.raises(SuiteValidationError, match="not in the catalogue"):
        load_suite(_write(tmp_path, _BAD_FAULT))


def test_validate_rejects_journey_not_in_denominator(tmp_path: Path) -> None:
    with pytest.raises(SuiteValidationError, match="not in the suite's all_journeys"):
        load_suite(_write(tmp_path, _BAD_JOURNEY))


def test_validate_can_be_deferred(tmp_path: Path) -> None:
    # load(validate=False) parses even a broken suite; validate_suite reports it.
    suite = load_suite(_write(tmp_path, _BAD_FAULT), validate=False)
    problems = validate_suite(suite)
    assert any("pod.killl" in p for p in problems)


def test_shipped_example_validates() -> None:
    example = (
        Path(__file__).resolve().parents[1]
        / "experiments"
        / "examples"
        / "regression"
        / "checkout.yaml"
    )
    suite = load_suite(example)
    assert suite.name == "checkout-resilience"
    assert suite.scenarios[0].fault.name == "network.loss"
    # Playwright default + merged base_url from oracle_defaults.
    assert suite.scenarios[0].oracle == OracleKind.PLAYWRIGHT
    assert suite.scenarios[0].oracle_config["base_url"] == "http://localhost:8080"
