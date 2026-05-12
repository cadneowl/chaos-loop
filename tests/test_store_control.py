"""Control plane: pause / abort signals on the store.

Tests cover schema migration of an existing DB, signal round-tripping, and
the no-op behavior when the targeted experiment doesn't exist.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from orchestrator.store import ControlSignals, ExperimentStore
from shared.contracts import (
    AbortReason,
    ExperimentPlan,
    ExperimentRecord,
    ExperimentState,
    FaultCategory,
    FaultSpec,
    SafetyConstraints,
)


def _plan() -> ExperimentPlan:
    return ExperimentPlan(
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
        safety=SafetyConstraints(
            cluster_context="kind-test", namespace="default",
            require_namespace_annotation=False,
        ),
    )


_id_counter = iter(range(10**6))


def _record(state: ExperimentState = ExperimentState.INITIALIZING) -> ExperimentRecord:
    # Generate a unique experiment_id per call so tests in the same DB don't collide.
    eid = f"exp-{next(_id_counter):012x}"
    return ExperimentRecord(experiment_id=eid, plan=_plan(), state=state)


# --------------------------------------------------------------------------- #
# Schema migration                                                            #
# --------------------------------------------------------------------------- #


def test_migration_adds_columns_to_existing_db(tmp_path: Path) -> None:
    """Open a DB created by the pre-control-plane schema and confirm the new
    columns get added on next ExperimentStore init."""
    db_path = tmp_path / "experiments.sqlite"
    # Hand-build the old schema.
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE experiments (
                experiment_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                abort_reason TEXT,
                spend_usd REAL NOT NULL DEFAULT 0,
                blob TEXT NOT NULL
            );
            """
        )
    # New ExperimentStore() should idempotently add the columns.
    ExperimentStore(db_path)
    with sqlite3.connect(db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(experiments)")}
    assert {"pause_requested", "abort_requested", "abort_reason_requested"} <= cols


def test_migration_is_idempotent(tmp_path: Path) -> None:
    """Re-initializing the store must not fail."""
    db_path = tmp_path / "experiments.sqlite"
    ExperimentStore(db_path)
    ExperimentStore(db_path)  # second init must be a no-op
    ExperimentStore(db_path)  # and a third


def test_fresh_init_has_control_columns_with_correct_defaults(tmp_path: Path) -> None:
    """A brand-new DB (no prior schema) must end up with the control columns
    present + their defaults set correctly."""
    db_path = tmp_path / "experiments.sqlite"
    ExperimentStore(db_path)
    with sqlite3.connect(db_path) as conn:
        cols = {row[1]: (row[3], row[4]) for row in conn.execute("PRAGMA table_info(experiments)")}
    # Format: column → (notnull-flag, default-value)
    assert cols["pause_requested"] == (1, "0")
    assert cols["abort_requested"] == (1, "0")
    # abort_reason_requested has no NOT NULL + no default explicitly set.
    assert cols["abort_reason_requested"] == (0, None)


def test_clear_control_clears_all_flags(tmp_path: Path) -> None:
    """clear_control must zero pause + abort + reason in one shot."""
    store = ExperimentStore(tmp_path / "experiments.sqlite")
    record = _record()
    store.save(record)
    store.set_pause(record.experiment_id, True)
    store.request_abort(record.experiment_id, AbortReason.USER_KILL)

    store.clear_control(record.experiment_id)

    ctrl = store.load_control(record.experiment_id)
    assert ctrl == ControlSignals(False, False, None)


def test_clear_control_on_unknown_id_is_noop(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "experiments.sqlite")
    store.clear_control("exp-doesnotexist0")  # must not raise


# --------------------------------------------------------------------------- #
# load_control                                                                #
# --------------------------------------------------------------------------- #


def test_load_control_default_for_unknown_id(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "experiments.sqlite")
    ctrl = store.load_control("exp-doesnotexist0")
    assert ctrl == ControlSignals(False, False, None)


def test_load_control_default_for_existing_record(tmp_path: Path) -> None:
    """A freshly-saved record reports no pending signals."""
    store = ExperimentStore(tmp_path / "experiments.sqlite")
    record = _record()
    store.save(record)
    ctrl = store.load_control(record.experiment_id)
    assert ctrl == ControlSignals(False, False, None)


# --------------------------------------------------------------------------- #
# set_pause                                                                   #
# --------------------------------------------------------------------------- #


def test_set_pause_sets_then_clears(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "experiments.sqlite")
    record = _record()
    store.save(record)

    assert store.set_pause(record.experiment_id, True) is True
    assert store.load_control(record.experiment_id).pause_requested is True

    assert store.set_pause(record.experiment_id, False) is True
    assert store.load_control(record.experiment_id).pause_requested is False


def test_set_pause_returns_false_for_unknown_id(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "experiments.sqlite")
    assert store.set_pause("exp-doesnotexist0", True) is False


def test_save_does_not_clobber_pause_flag(tmp_path: Path) -> None:
    """Critical correctness: the orchestrator's save() must preserve operator
    signals. Otherwise a state transition wipes pause_requested before the
    next poll sees it."""
    store = ExperimentStore(tmp_path / "experiments.sqlite")
    record = _record()
    store.save(record)

    store.set_pause(record.experiment_id, True)
    # Orchestrator advances state and saves.
    record.state = ExperimentState.BASELINE
    store.save(record)

    ctrl = store.load_control(record.experiment_id)
    assert ctrl.pause_requested is True, "save() clobbered the operator's pause flag"


# --------------------------------------------------------------------------- #
# request_abort                                                               #
# --------------------------------------------------------------------------- #


def test_request_abort_persists_reason(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "experiments.sqlite")
    record = _record()
    store.save(record)

    assert store.request_abort(record.experiment_id, AbortReason.USER_KILL) is True
    ctrl = store.load_control(record.experiment_id)
    assert ctrl.abort_requested is True
    assert ctrl.abort_reason == AbortReason.USER_KILL


def test_request_abort_returns_false_for_unknown_id(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path / "experiments.sqlite")
    assert store.request_abort("exp-doesnotexist0", AbortReason.USER_KILL) is False


@pytest.mark.parametrize(
    "reason",
    [
        AbortReason.USER_KILL,
        AbortReason.BUDGET_EXCEEDED,
        AbortReason.BLAST_RADIUS_VIOLATION,
    ],
)
def test_request_abort_with_various_reasons(tmp_path: Path, reason: AbortReason) -> None:
    store = ExperimentStore(tmp_path / "experiments.sqlite")
    record = _record()
    store.save(record)
    store.request_abort(record.experiment_id, reason)
    assert store.load_control(record.experiment_id).abort_reason == reason


def test_save_does_not_clobber_abort_flag(tmp_path: Path) -> None:
    """Same correctness property as pause: state transitions must not erase
    the operator's abort signal before the orchestrator polls it."""
    store = ExperimentStore(tmp_path / "experiments.sqlite")
    record = _record()
    store.save(record)

    store.request_abort(record.experiment_id, AbortReason.USER_KILL)
    record.state = ExperimentState.BASELINE
    store.save(record)

    ctrl = store.load_control(record.experiment_id)
    assert ctrl.abort_requested is True
    assert ctrl.abort_reason == AbortReason.USER_KILL
