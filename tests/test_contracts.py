"""Validate that every example experiment YAML parses cleanly into ExperimentPlan."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from shared.contracts import ExperimentPlan

EXAMPLES = Path(__file__).resolve().parents[1] / "experiments" / "examples"


@pytest.mark.parametrize("yaml_path", sorted(EXAMPLES.glob("*.yaml")), ids=lambda p: p.name)
def test_example_loads(yaml_path: Path) -> None:
    raw = yaml.safe_load(yaml_path.read_text())
    plan = ExperimentPlan.model_validate(raw)
    assert plan.faults, "plan must have at least one fault"
    for f in plan.faults:
        assert f.duration_seconds <= plan.safety.max_duration_seconds, (
            f"fault {f.name} exceeds safety.max_duration_seconds"
        )


def test_example_faults_all_in_catalogue() -> None:
    from agents.chaos.faults._meta import CATALOGUE

    for yaml_path in EXAMPLES.glob("*.yaml"):
        plan = ExperimentPlan.model_validate(yaml.safe_load(yaml_path.read_text()))
        for f in plan.faults:
            assert f.name in CATALOGUE, f"{yaml_path.name}: fault {f.name!r} not in catalogue"


def test_token_budget_validates() -> None:
    from shared.contracts import TokenBudget

    with pytest.raises(ValueError):
        TokenBudget(soft_cap_usd=5.0, hard_cap_usd=1.0)  # hard < soft


def test_fix_proposal_must_be_draft() -> None:
    from shared.contracts import FixAction, FixProposal

    with pytest.raises(ValueError):
        FixProposal(
            experiment_id="exp-aaaaaaaaaaaa",
            action=FixAction.CODE_PATCH,
            confidence=0.9,
            reasoning="test",
            is_draft=False,
        )
