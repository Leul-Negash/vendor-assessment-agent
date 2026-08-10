"""Long-term memory: a SQLite store for runs, steps, evidence, and decisions.

Short-term task state lives in `state.TaskState` and dies with the run. This
module is what survives it, and it is where duplicate-action prevention is
actually enforced: `decisions.request_id` is the primary key, so a second
attempt to record a final action for the same request cannot insert. The agent
layer relies on that guarantee rather than reimplementing it.

The provided data directory is treated as read-only input. Decisions are
written to a run directory instead, seeded from the supplied decision_log.json.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from .schemas import FinalDecision

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    request_id  TEXT NOT NULL,
    vendor_name TEXT,
    product     TEXT,
    cost        REAL,
    data_type   TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    action      TEXT,
    stop_reason TEXT,
    steps_used  INTEGER,
    retries_used INTEGER
);

CREATE TABLE IF NOT EXISTS steps (
    run_id      TEXT NOT NULL,
    step_index  INTEGER NOT NULL,
    thought     TEXT,
    tool        TEXT,
    route       TEXT,
    attempt     INTEGER,
    status      TEXT,
    observation TEXT,
    is_retry    INTEGER DEFAULT 0,
    retry_rationale TEXT,
    PRIMARY KEY (run_id, step_index)
);

CREATE TABLE IF NOT EXISTS evidence (
    run_id      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    source_type TEXT,
    authority_tier INTEGER,
    document_date TEXT,
    age_days    INTEGER,
    is_current  INTEGER,
    quarantined INTEGER,
    payload     TEXT,
    PRIMARY KEY (run_id, source_id)
);

CREATE TABLE IF NOT EXISTS submissions (
    request_id  TEXT PRIMARY KEY,
    count       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    request_id  TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    action      TEXT NOT NULL,
    rationale   TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    decision    TEXT NOT NULL
);
"""

INSERT_RUN = (
    "INSERT OR REPLACE INTO runs "
    "(run_id, request_id, vendor_name, product, cost, data_type, started_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)
UPDATE_RUN = (
    "UPDATE runs SET finished_at = ?, action = ?, stop_reason = ?, steps_used = ?, "
    "retries_used = ? WHERE run_id = ?"
)
INSERT_STEP = (
    "INSERT OR REPLACE INTO steps "
    "(run_id, step_index, thought, tool, route, attempt, status, observation, is_retry, "
    "retry_rationale) VALUES (?,?,?,?,?,?,?,?,?,?)"
)
INSERT_EVIDENCE = (
    "INSERT OR REPLACE INTO evidence "
    "(run_id, source_id, source_type, authority_tier, document_date, age_days, is_current, "
    "quarantined, payload) VALUES (?,?,?,?,?,?,?,?,?)"
)
INSERT_DECISION = (
    "INSERT INTO decisions (request_id, run_id, action, rationale, recorded_at, decision) "
    "VALUES (?,?,?,?,?,?)"
)
COUNT_SUBMISSION = (
    "INSERT INTO submissions (request_id, count) VALUES (?, 1) "
    "ON CONFLICT(request_id) DO UPDATE SET count = count + 1"
)


class MemoryStore:
    """One connection, guarded by a lock.

    The web layer answers requests from a thread pool, so the connection is
    shared rather than opened per thread. The lock does double duty: it makes
    that sharing safe, and it makes the duplicate check and the insert that
    follows it a single atomic step.
    """

    def __init__(self, run_dir: Path, seed_decision_log: Path | None = None):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.run_dir / "agent_memory.db"
        self.decision_log_path = self.run_dir / "decision_log.json"

        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

        if not self.decision_log_path.exists():
            seeded: list = []
            if seed_decision_log and seed_decision_log.exists():
                seeded = json.loads(seed_decision_log.read_text(encoding="utf-8") or "[]")
            self.decision_log_path.write_text(json.dumps(seeded, indent=2) + "\n", encoding="utf-8")

    # --- connection helpers -------------------------------------------------

    def _write(self, statements: Iterable[tuple[str, Sequence]]) -> None:
        with self._lock:
            for sql, params in statements:
                self._conn.execute(sql, params)
            self._conn.commit()

    def _rows(self, sql: str, params: Sequence = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- runs ---------------------------------------------------------------

    def open_run(self, state) -> None:
        request = state.request
        self._write(
            [
                (
                    INSERT_RUN,
                    (
                        state.run_id,
                        request.request_id,
                        request.vendor_name,
                        request.product,
                        request.cost,
                        request.data_type,
                        state.started_at.isoformat(),
                    ),
                )
            ]
        )

    def close_run(self, state, decision: FinalDecision) -> None:
        statements: list[tuple[str, Sequence]] = [
            (
                UPDATE_RUN,
                (
                    datetime.now(timezone.utc).isoformat(),
                    decision.action.value,
                    decision.stop_reason.value,
                    decision.steps_used,
                    decision.retries_used,
                    state.run_id,
                ),
            )
        ]
        for step in state.steps:
            statements.append(
                (
                    INSERT_STEP,
                    (
                        state.run_id,
                        step.index,
                        step.thought,
                        step.call.tool if step.call else None,
                        step.call.route if step.call else None,
                        step.call.attempt if step.call else None,
                        step.status.value if step.status else None,
                        step.observation,
                        int(step.is_retry),
                        step.retry_rationale,
                    ),
                )
            )
        for item in state.evidence:
            statements.append(
                (
                    INSERT_EVIDENCE,
                    (
                        state.run_id,
                        item.source_id,
                        item.source_type,
                        item.authority_tier,
                        item.document_date.isoformat() if item.document_date else None,
                        item.age_days,
                        int(item.is_current),
                        int(item.quarantined),
                        json.dumps(item.payload, default=str),
                    ),
                )
            )
        self._write(statements)

    # --- decisions ----------------------------------------------------------

    def next_submission_number(self, request_id: str) -> int:
        """Count submissions for a request across every run, not just this one.

        Kept separate from `decisions` because a submission refused as a
        duplicate still happened and still needs counting.
        """
        with self._lock:
            self._write([(COUNT_SUBMISSION, (request_id,))])
            rows = self._rows("SELECT count FROM submissions WHERE request_id = ?", (request_id,))
        return int(rows[0]["count"])

    def find_decision(self, request_id: str) -> FinalDecision | None:
        rows = self._rows("SELECT decision FROM decisions WHERE request_id = ?", (request_id,))
        if not rows:
            return None
        return FinalDecision.model_validate_json(rows[0]["decision"])

    def record_decision(self, decision: FinalDecision, run_id: str) -> tuple[FinalDecision, bool]:
        """Persist a final action once.

        Returns the stored decision and whether it already existed. The primary
        key is the real guard, so two runs racing on the same request cannot
        both write; the loser is told what the winner recorded.
        """
        with self._lock:
            existing = self.find_decision(decision.request_id)
            if existing is not None:
                return existing, True
            try:
                self._write(
                    [
                        (
                            INSERT_DECISION,
                            (
                                decision.request_id,
                                run_id,
                                decision.action.value,
                                decision.rationale,
                                datetime.now(timezone.utc).isoformat(),
                                decision.model_dump_json(),
                            ),
                        )
                    ]
                )
            except sqlite3.IntegrityError:
                stored = self.find_decision(decision.request_id)
                if stored is None:
                    raise
                return stored, True

            self._append_decision_log(decision)
            return decision, False

    def _append_decision_log(self, decision: FinalDecision) -> None:
        entries = json.loads(self.decision_log_path.read_text(encoding="utf-8") or "[]")
        if any(entry.get("request_id") == decision.request_id for entry in entries):
            return
        entries.append(decision.model_dump(mode="json"))
        self.decision_log_path.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")

    def all_decisions(self) -> list[FinalDecision]:
        rows = self._rows("SELECT decision FROM decisions ORDER BY recorded_at")
        return [FinalDecision.model_validate_json(row["decision"]) for row in rows]

    def run_summaries(self) -> list[dict]:
        rows = self._rows(
            "SELECT run_id, request_id, vendor_name, action, stop_reason, steps_used, retries_used "
            "FROM runs ORDER BY started_at"
        )
        return [dict(row) for row in rows]
