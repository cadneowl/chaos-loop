"""Load / dump regression suites from YAML.

The on-disk format mirrors the ``RegressionSuite`` contract with two
conveniences applied before validation:

* a top-level ``oracle`` sets the default kind for every scenario that doesn't
  set its own;
* ``oracle_defaults`` is shallow-merged into each scenario's ``oracle_config``
  (scenario-level keys win) — so shared settings like ``suite_path`` /
  ``base_url`` are written once.

Everything else validates straight through Pydantic.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from agents.chaos.faults._meta import CATALOGUE
from shared.contracts import RegressionSuite


def _stable_suite_id(name: str) -> str:
    """Deterministic suite id derived from the suite name.

    So the same suite file keeps one identity across loads — otherwise every run
    would get a fresh random id and ``chaos regression list`` couldn't group a
    suite's history (the "does it stay fixed across releases?" view).
    """
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
    return f"suite-{digest}"


class SuiteValidationError(ValueError):
    """A suite that loaded but is internally inconsistent (bad fault / journey refs)."""


def validate_suite(suite: RegressionSuite) -> list[str]:
    """Return a list of human-readable problems; empty means the suite is sound.

    Catches the silent-misreporting traps: a fault name that isn't in the
    catalogue (contributes no coverage) and a scenario journey missing from
    ``all_journeys`` (a typo that would masquerade as an uncovered gap).
    """
    problems: list[str] = []
    all_journeys = set(suite.all_journeys)
    for scn in suite.scenarios:
        where = f"scenario {scn.title!r}"
        if scn.fault.name not in CATALOGUE:
            problems.append(
                f"{where}: fault {scn.fault.name!r} is not in the catalogue "
                f"(see `chaos list-faults`); it would contribute no coverage."
            )
        if not scn.journeys:
            problems.append(f"{where}: no journeys listed — it asserts nothing.")
        if all_journeys:
            missing = [j for j in scn.journeys if j not in all_journeys]
            if missing:
                problems.append(
                    f"{where}: journeys {missing} are not in the suite's "
                    f"all_journeys — likely a typo or a missing entry."
                )
    return problems


def _expand(raw: dict[str, Any]) -> dict[str, Any]:
    data = dict(raw)
    # Give the suite a stable identity from its name unless one is pinned, so a
    # file's runs share a suite_id across loads (see _stable_suite_id).
    if "suite_id" not in data and isinstance(data.get("name"), str):
        data["suite_id"] = _stable_suite_id(data["name"])
    default_oracle = data.pop("oracle", None)
    defaults: dict[str, Any] = data.pop("oracle_defaults", None) or {}
    scenarios: list[dict[str, Any]] = list(data.get("scenarios") or [])

    expanded: list[dict[str, Any]] = []
    for scn in scenarios:
        merged = dict(scn)
        if default_oracle is not None and "oracle" not in merged:
            merged["oracle"] = default_oracle
        oracle_config = {**defaults, **(merged.get("oracle_config") or {})}
        if oracle_config:
            merged["oracle_config"] = oracle_config
        expanded.append(merged)

    data["scenarios"] = expanded
    return data


def load_suite(path: Path, *, validate: bool = True) -> RegressionSuite:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"suite file {path} must be a YAML mapping, got {type(raw).__name__}")
    suite = RegressionSuite.model_validate(_expand(raw))
    if validate:
        problems = validate_suite(suite)
        if problems:
            raise SuiteValidationError(
                f"{path.name} has {len(problems)} problem(s):\n  - "
                + "\n  - ".join(problems)
            )
    return suite


def dump_suite(suite: RegressionSuite) -> str:
    return yaml.safe_dump(suite.model_dump(mode="json"), sort_keys=False)
