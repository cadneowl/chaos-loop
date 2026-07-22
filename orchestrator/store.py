"""
Experiment record persistence.

SQLite for v1 — single-process, file-based, no service to run. Each
ExperimentRecord is stored as one row with the full Pydantic blob as JSON.
If we outgrow this, swap for Postgres without changing the call sites.

Two concerns share the table:
    1. **Audit / state**: the immutable history of an experiment's run, kept
       in the ``blob`` column. Read by the UI; written only by the orchestrator.
    2. **Control signals**: transient flags the operator (or the UI) sets to
       ask a running orchestrator to pause / resume / abort. Kept as their
       own columns so the orchestrator can poll them without rehydrating the
       full record. Cleared on resume / honored on abort.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from shared.contracts import (
    AbortReason,
    ExperimentId,
    ExperimentRecord,
    ExperimentState,
    Golden,
    SuiteRunId,
    SuiteRunRecord,
)

# How long a connection waits for a contended write lock before raising
# SQLITE_BUSY. The default 5s is fine for single-process use; we bump to 15s
# so a concurrent CLI / UI process can't trip it on a long save.
_CONNECT_TIMEOUT_S = 15.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    abort_reason TEXT,
    spend_usd REAL NOT NULL DEFAULT 0,
    blob TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_state ON experiments(state);
CREATE INDEX IF NOT EXISTS idx_started_at ON experiments(started_at);

CREATE TABLE IF NOT EXISTS suite_runs (
    suite_run_id TEXT PRIMARY KEY,
    suite_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    blob TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_suite_id ON suite_runs(suite_id);
CREATE INDEX IF NOT EXISTS idx_suite_started_at ON suite_runs(started_at);

CREATE TABLE IF NOT EXISTS goldens (
    suite_id TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    scenario_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    blob TEXT NOT NULL,
    PRIMARY KEY (suite_id, target_ref, scenario_id)
);

CREATE INDEX IF NOT EXISTS idx_goldens_suite ON goldens(suite_id);
"""

# Idempotent column adds. Each entry is (column-name, ALTER statement).
# We try them on every init; SQLite raises "duplicate column name" when the
# column already exists, which we swallow. Cheap and safe.
_MIGRATIONS = [
    ("pause_requested", "ALTER TABLE experiments ADD COLUMN pause_requested INTEGER NOT NULL DEFAULT 0"),
    ("abort_requested", "ALTER TABLE experiments ADD COLUMN abort_requested INTEGER NOT NULL DEFAULT 0"),
    ("abort_reason_requested", "ALTER TABLE experiments ADD COLUMN abort_reason_requested TEXT"),
]


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Return the column set for ``table``. Empty set if the table doesn't exist."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


@dataclass(frozen=True)
class ControlSignals:
    """Snapshot of the control flags for one experiment.

    Loaded by the orchestrator's loop between every state transition. The
    orchestrator never caches this — always re-reads, so the operator's
    signals take effect within ~1s of being written.
    """

    pause_requested: bool
    abort_requested: bool
    abort_reason: AbortReason | None


class ExperimentStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            # WAL allows the future UI reader to read the DB while the
            # orchestrator writes — without it, every reader blocks on
            # the writer's transaction.
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(_SCHEMA)
            self._migrate(c)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Apply every additive migration that hasn't already run.

        Uses ``PRAGMA table_info`` to introspect the current schema rather
        than relying on the SQLite error message string for duplicate-column
        detection. Version-proof.
        """
        existing = _existing_columns(conn, "experiments")
        for column, ddl in _MIGRATIONS:
            if column not in existing:
                conn.execute(ddl)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=_CONNECT_TIMEOUT_S)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def save(self, record: ExperimentRecord) -> None:
        """Upsert the record. Preserves any control flags already on the row —
        the orchestrator must not overwrite the operator's pause / abort
        signal mid-run."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO experiments "
                "(experiment_id, state, started_at, finished_at, abort_reason, spend_usd, blob) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(experiment_id) DO UPDATE SET "
                "  state=excluded.state, "
                "  started_at=excluded.started_at, "
                "  finished_at=excluded.finished_at, "
                "  abort_reason=excluded.abort_reason, "
                "  spend_usd=excluded.spend_usd, "
                "  blob=excluded.blob",
                (
                    record.experiment_id,
                    record.state.value,
                    record.started_at.isoformat(),
                    record.finished_at.isoformat() if record.finished_at else None,
                    record.abort_reason.value if record.abort_reason else None,
                    record.spend_usd,
                    record.model_dump_json(),
                ),
            )

    def load(self, experiment_id: ExperimentId) -> ExperimentRecord | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT blob FROM experiments WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
        if row is None:
            return None
        return ExperimentRecord.model_validate(json.loads(row[0]))

    def recent(self, limit: int = 20) -> list[ExperimentRecord]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT blob FROM experiments ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [ExperimentRecord.model_validate(json.loads(r[0])) for r in rows]

    # -- regression suite runs -------------------------------------------------

    def save_suite_run(self, record: SuiteRunRecord) -> None:
        """Upsert one regression suite run. Per-scenario ExperimentRecords are
        saved separately by the loop; a verdict links to them by experiment_id."""
        with self._conn() as c:
            c.execute(
                "INSERT INTO suite_runs "
                "(suite_run_id, suite_id, started_at, finished_at, blob) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(suite_run_id) DO UPDATE SET "
                "  suite_id=excluded.suite_id, "
                "  started_at=excluded.started_at, "
                "  finished_at=excluded.finished_at, "
                "  blob=excluded.blob",
                (
                    record.suite_run_id,
                    record.suite_id,
                    record.started_at.isoformat(),
                    record.finished_at.isoformat() if record.finished_at else None,
                    record.model_dump_json(),
                ),
            )

    def load_suite_run(self, suite_run_id: SuiteRunId) -> SuiteRunRecord | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT blob FROM suite_runs WHERE suite_run_id = ?",
                (suite_run_id,),
            ).fetchone()
        if row is None:
            return None
        return SuiteRunRecord.model_validate(json.loads(row[0]))

    def recent_suite_runs(self, limit: int = 20) -> list[SuiteRunRecord]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT blob FROM suite_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [SuiteRunRecord.model_validate(json.loads(r[0])) for r in rows]

    # -- goldens (chronic drift baselines) -------------------------------------

    def save_goldens(
        self, suite_id: str, target_ref: str, goldens: dict[str, Golden]
    ) -> None:
        """Store one golden per scenario for ``(suite_id, target_ref)``. Upserts."""
        with self._conn() as c:
            for scenario_id, golden in goldens.items():
                c.execute(
                    "INSERT INTO goldens "
                    "(suite_id, target_ref, scenario_id, captured_at, blob) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(suite_id, target_ref, scenario_id) DO UPDATE SET "
                    "  captured_at=excluded.captured_at, blob=excluded.blob",
                    (
                        suite_id,
                        target_ref,
                        scenario_id,
                        golden.captured_at.isoformat(),
                        golden.model_dump_json(),
                    ),
                )

    def load_goldens(self, suite_id: str, target_ref: str) -> dict[str, Golden]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT scenario_id, blob FROM goldens "
                "WHERE suite_id = ? AND target_ref = ?",
                (suite_id, target_ref),
            ).fetchall()
        return {sid: Golden.model_validate(json.loads(blob)) for sid, blob in rows}

    def golden_refs(self, suite_id: str) -> list[tuple[str, int, str]]:
        """``(target_ref, scenario_count, latest_captured_at)`` per stored ref."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT target_ref, COUNT(*), MAX(captured_at) FROM goldens "
                "WHERE suite_id = ? GROUP BY target_ref ORDER BY MAX(captured_at) DESC",
                (suite_id,),
            ).fetchall()
        return [(ref, int(count), captured) for ref, count, captured in rows]

    # -- control plane ---------------------------------------------------------

    def load_control(self, experiment_id: ExperimentId) -> ControlSignals:
        """Read the live control flags for a single experiment. Cheap (one row)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT pause_requested, abort_requested, abort_reason_requested "
                "FROM experiments WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
        if row is None:
            return ControlSignals(False, False, None)
        pause, abort, reason = row
        return ControlSignals(
            pause_requested=bool(pause),
            abort_requested=bool(abort),
            abort_reason=AbortReason(reason) if reason else None,
        )

    def set_pause(self, experiment_id: ExperimentId, paused: bool) -> bool:
        """Set or clear the pause flag. Returns True if a row was changed.

        Returns False when no record exists for ``experiment_id`` (e.g., the
        operator paused a typo). The CLI should surface that as a clear error.
        """
        with self._conn() as c:
            cursor = c.execute(
                "UPDATE experiments SET pause_requested = ? WHERE experiment_id = ?",
                (1 if paused else 0, experiment_id),
            )
            return cursor.rowcount > 0

    def request_abort(
        self, experiment_id: ExperimentId, reason: AbortReason
    ) -> bool:
        """Ask a running orchestrator to abort. Returns True if a row was changed.

        Idempotent: setting the same flag twice is a no-op (the orchestrator
        only acts on the *first* read of the flag after the request, then
        transitions to ABORTED).
        """
        with self._conn() as c:
            cursor = c.execute(
                "UPDATE experiments "
                "SET abort_requested = 1, abort_reason_requested = ? "
                "WHERE experiment_id = ?",
                (reason.value, experiment_id),
            )
            return cursor.rowcount > 0

    def clear_control(self, experiment_id: ExperimentId) -> None:
        """Clear all pending control flags for one experiment.

        The orchestrator calls this on terminal transitions (finish / abort)
        so a record never lands in a terminal state with stale signals on
        it. Idempotent — no-op if the row doesn't exist.
        """
        with self._conn() as c:
            c.execute(
                "UPDATE experiments "
                "SET pause_requested = 0, "
                "    abort_requested = 0, "
                "    abort_reason_requested = NULL "
                "WHERE experiment_id = ?",
                (experiment_id,),
            )

    def find_live(self, live_states: Iterable[ExperimentState]) -> list[ExperimentRecord]:
        """All records whose state is in ``live_states``. No row-count limit.

        Used by ``chaos abort --all``. The set of live states is small (~13)
        so the IN clause stays compact.
        """
        states = [s.value for s in live_states]
        if not states:
            return []
        placeholders = ",".join("?" for _ in states)
        with self._conn() as c:
            rows = c.execute(
                f"SELECT blob FROM experiments WHERE state IN ({placeholders}) "
                "ORDER BY started_at DESC",
                states,
            ).fetchall()
        return [ExperimentRecord.model_validate(json.loads(r[0])) for r in rows]
