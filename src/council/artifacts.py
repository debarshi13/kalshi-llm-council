"""Run persistence: per-run directory on disk + a SQLite history index.

Layout:  runs/<run_id>/
            workspace/        # the agents' sandbox (code lands here)
            transcript.json   # every emitted Event
            result.json       # the RunResult
A row per run also goes into council.db for fast history listing.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .council import RunResult
from .events import Event


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


class RunStore:
    def __init__(self, base_dir: str | Path = "runs", db_path: str | Path = "council.db") -> None:
        self.base = Path(base_dir)
        self.base.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id      TEXT PRIMARY KEY,
                    task        TEXT NOT NULL,
                    status      TEXT,
                    rounds      INTEGER,
                    cost_usd    REAL,
                    total_tokens INTEGER,
                    created_at  TEXT NOT NULL,
                    finished_at TEXT
                )
                """
            )

    # ---- directory helpers -------------------------------------------------
    def run_dir(self, run_id: str) -> Path:
        return self.base / run_id

    def workspace_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "workspace"

    def create(self, run_id: str, task: str) -> Path:
        d = self.run_dir(run_id)
        (d / "workspace").mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT OR REPLACE INTO runs(run_id, task, status, created_at) VALUES (?,?,?,?)",
                (run_id, task, "running", datetime.now(timezone.utc).isoformat()),
            )
        return d

    # ---- writes ------------------------------------------------------------
    def save_transcript(self, run_id: str, events: list[Event]) -> None:
        path = self.run_dir(run_id) / "transcript.json"
        path.write_text(json.dumps([e.to_dict() for e in events], indent=2))

    def finalize(self, result: RunResult) -> None:
        (self.run_dir(result.run_id) / "result.json").write_text(
            json.dumps(result.to_dict(), indent=2)
        )
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                """UPDATE runs SET status=?, rounds=?, cost_usd=?, total_tokens=?, finished_at=?
                   WHERE run_id=?""",
                (
                    result.status.value,
                    result.rounds,
                    result.usage.cost_usd,
                    result.usage.total_tokens,
                    datetime.now(timezone.utc).isoformat(),
                    result.run_id,
                ),
            )

    # ---- reads -------------------------------------------------------------
    def list_runs(self, limit: int = 100) -> list[dict]:
        with sqlite3.connect(self.db_path) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_run(self, run_id: str) -> dict | None:
        with sqlite3.connect(self.db_path) as con:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        result_path = self.run_dir(run_id) / "result.json"
        if result_path.exists():
            data["result"] = json.loads(result_path.read_text())
        return data
