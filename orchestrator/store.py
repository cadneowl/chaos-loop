"""
Experiment record persistence.

SQLite for v1 — single-process, file-based, no service to run. Each ExperimentRecord
is stored as one row with the full Pydantic blob as JSON. If we outgrow this, swap
for Postgres without changing the call sites.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from shared.contracts import ExperimentId, ExperimentRecord

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
"""


class ExperimentStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def save(self, record: ExperimentRecord) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO experiments "
                "(experiment_id, state, started_at, finished_at, abort_reason, spend_usd, blob) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
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
