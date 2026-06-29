"""
Pipeline state — tracks pipeline instances in an isolated SQLite database.

Each pipeline instance (hermes workflow run) creates a row in the
pipeline_instances table. The state DB lives alongside the plugin at
~/.hermes/workflow/state.db.

Kanban cards themselves carry workflow metadata in comments (JSON marker),
which the runner uses to discover cards belonging to a pipeline.

Step execution logs are stored in the step_logs table for debugging and
post-mortem analysis.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .schema import PipelineInstance, PipelineStatus

# State DB path — alongside the plugin installation
_HERMES_HOME = Path(os.getenv("HERMES_HOME", os.path.expanduser("~/.hermes")))
_STATE_DIR = _HERMES_HOME / "workflow"
_STATE_DB = _STATE_DIR / "state.db"


def _get_conn() -> sqlite3.Connection:
    """Get a connection to the state DB (auto-creates tables)."""
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_STATE_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_tables(conn)
    return conn


def _ensure_tables(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pipeline_instances (
            id              TEXT PRIMARY KEY,
            template_name   TEXT NOT NULL,
            template_version TEXT NOT NULL DEFAULT '1.0.0',
            status          TEXT NOT NULL DEFAULT 'running',
            current_step_id TEXT,
            current_cycle   INTEGER DEFAULT 1,
            max_cycles      INTEGER DEFAULT 1,
            vars            TEXT NOT NULL DEFAULT '{}',
            step_outputs    TEXT NOT NULL DEFAULT '{}',
            created_at      INTEGER NOT NULL,
            updated_at      INTEGER NOT NULL,
            error           TEXT
        );

        CREATE TABLE IF NOT EXISTS step_logs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            pipeline_id     TEXT NOT NULL,
            step_id         TEXT NOT NULL,
            cycle           INTEGER DEFAULT 1,
            step_type       TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'running',
            started_at      INTEGER NOT NULL,
            ended_at        INTEGER,
            exit_code       INTEGER,
            stdout          TEXT,
            stderr          TEXT,
            error_message   TEXT,
            card_ids        TEXT,
            worker_logs     TEXT,
            details         TEXT,
            created_at      INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_step_logs_pipeline
            ON step_logs(pipeline_id, step_id, cycle);
    """)


# =====================================================================
# Pipeline instance CRUD
# =====================================================================

def create_pipeline_instance(
    template_name: str,
    template_version: str,
    vars: dict,
) -> str:
    """Create a new pipeline instance and return its id."""
    pipeline_id = f"pipe_{uuid.uuid4().hex[:12]}"
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO pipeline_instances
               (id, template_name, template_version, status, vars, created_at, updated_at)
               VALUES (?, ?, ?, 'running', ?, ?, ?)""",
            (
                pipeline_id,
                template_name,
                template_version,
                json.dumps(vars),
                int(time.time()),
                int(time.time()),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return pipeline_id


def set_pipeline_step(pipeline_id: str, step_id: Optional[str]) -> None:
    """Update the current step for a pipeline."""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE pipeline_instances SET current_step_id=?, updated_at=? WHERE id=?",
            (step_id, int(time.time()), pipeline_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_pipeline_cycle(pipeline_id: str, cycle: int) -> None:
    """Update the current loop cycle for a pipeline."""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE pipeline_instances SET current_cycle=?, updated_at=? WHERE id=?",
            (cycle, int(time.time()), pipeline_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_pipeline_status(
    pipeline_id: str,
    status: PipelineStatus,
    error: Optional[str] = None,
) -> None:
    """Update pipeline status."""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE pipeline_instances SET status=?, updated_at=?, error=? WHERE id=?",
            (status.value, int(time.time()), error, pipeline_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_step_outputs(pipeline_id: str, step_outputs: dict) -> None:
    """Persist step outputs for a pipeline."""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE pipeline_instances SET step_outputs=?, updated_at=? WHERE id=?",
            (json.dumps(step_outputs), int(time.time()), pipeline_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_pipeline(pipeline_id: str) -> Optional[PipelineInstance]:
    """Get a single pipeline instance."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM pipeline_instances WHERE id=?", (pipeline_id,)
        ).fetchone()
        if row is None:
            return None
        return _row_to_pipeline(row)
    finally:
        conn.close()


def list_pipelines(
    status: Optional[str] = None,
    limit: int = 50,
) -> list[PipelineInstance]:
    """List pipeline instances, newest first."""
    conn = _get_conn()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM pipeline_instances WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM pipeline_instances ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_pipeline(r) for r in rows]
    finally:
        conn.close()


def delete_old_pipelines(older_than_days: int = 7) -> int:
    """Delete pipeline instances older than N days. Returns count deleted.

    Also deletes related step_logs.
    """
    cutoff = int(time.time()) - older_than_days * 86400
    conn = _get_conn()
    try:
        deleted_logs = conn.execute(
            "DELETE FROM step_logs WHERE pipeline_id IN "
            "(SELECT id FROM pipeline_instances WHERE created_at < ?)",
            (cutoff,),
        ).rowcount
        deleted = conn.execute(
            "DELETE FROM pipeline_instances WHERE created_at < ?",
            (cutoff,),
        ).rowcount
        conn.commit()
        return deleted
    finally:
        conn.close()


# =====================================================================
# Step log CRUD
# =====================================================================

def start_step_log(
    pipeline_id: str,
    step_id: str,
    step_type: str,
    cycle: int = 1,
    details: Optional[dict] = None,
) -> int:
    """Record the start of a step execution. Returns log id."""
    conn = _get_conn()
    now = int(time.time())
    try:
        cur = conn.execute(
            """INSERT INTO step_logs
               (pipeline_id, step_id, cycle, step_type, status,
                started_at, details, created_at)
               VALUES (?, ?, ?, ?, 'running', ?, ?, ?)""",
            (pipeline_id, step_id, cycle, step_type, now,
             json.dumps(details or {}), now),
        )
        conn.commit()
        return cur.lastrowid or 0
    finally:
        conn.close()


def complete_step_log(
    log_id: int,
    status: str = "done",
    exit_code: Optional[int] = None,
    stdout: Optional[str] = None,
    stderr: Optional[str] = None,
    error_message: Optional[str] = None,
    card_ids: Optional[list[str]] = None,
) -> None:
    """Record the completion of a step execution."""
    conn = _get_conn()
    try:
        conn.execute(
            """UPDATE step_logs SET
               status=?, ended_at=?, exit_code=?, stdout=?,
               stderr=?, error_message=?, card_ids=?
               WHERE id=?""",
            (
                status,
                int(time.time()),
                exit_code,
                (stdout or "")[:10000],
                (stderr or "")[:10000],
                error_message,
                json.dumps(card_ids or []),
                log_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def append_worker_logs(log_id: int, card_id: str, run_info: dict) -> None:
    """Append a worker run entry to a step log's worker_logs JSON field."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT worker_logs FROM step_logs WHERE id=?", (log_id,)
        ).fetchone()
        if row is None:
            return
        existing = json.loads(row["worker_logs"]) if row["worker_logs"] else []
        existing.append(run_info)
        conn.execute(
            "UPDATE step_logs SET worker_logs=? WHERE id=?",
            (json.dumps(existing), log_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_step_logs(pipeline_id: str) -> list[dict]:
    """Get all step logs for a pipeline, ordered by started_at."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT * FROM step_logs
               WHERE pipeline_id=? ORDER BY started_at ASC""",
            (pipeline_id,),
        ).fetchall()
        return [_row_to_log(r) for r in rows]
    finally:
        conn.close()


def get_step_log(
    pipeline_id: str,
    step_id: str,
    cycle: int = 1,
) -> Optional[dict]:
    """Get the log for a specific step/cycle."""
    conn = _get_conn()
    try:
        row = conn.execute(
            """SELECT * FROM step_logs
               WHERE pipeline_id=? AND step_id=? AND cycle=?
               ORDER BY started_at DESC LIMIT 1""",
            (pipeline_id, step_id, cycle),
        ).fetchone()
        if row is None:
            return None
        return _row_to_log(row)
    finally:
        conn.close()


def get_log_by_id(log_id: int) -> Optional[dict]:
    """Get a step log by its id."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM step_logs WHERE id=?", (log_id,)
        ).fetchone()
        if row is None:
            return None
        return _row_to_log(row)
    finally:
        conn.close()


def get_worker_log(
    pipeline_id: str,
    step_id: str,
    card_id: str,
    cycle: int = 1,
) -> Optional[dict]:
    """Get the worker log entry for a specific card within a step."""
    log = get_step_log(pipeline_id, step_id, cycle)
    if log is None:
        return None
    worker_logs = log.get("worker_logs", [])
    for entry in worker_logs:
        if entry.get("card_id") == card_id:
            return entry
    return None


def _row_to_log(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "pipeline_id": row["pipeline_id"],
        "step_id": row["step_id"],
        "cycle": row["cycle"],
        "step_type": row["step_type"],
        "status": row["status"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "exit_code": row["exit_code"],
        "stdout": row["stdout"],
        "stderr": row["stderr"],
        "error_message": row["error_message"],
        "card_ids": json.loads(row["card_ids"]) if row["card_ids"] else [],
        "worker_logs": json.loads(row["worker_logs"]) if row["worker_logs"] else [],
        "details": json.loads(row["details"]) if row["details"] else {},
        "created_at": row["created_at"],
    }


def _row_to_pipeline(row: sqlite3.Row) -> PipelineInstance:
    return PipelineInstance(
        id=row["id"],
        template_name=row["template_name"],
        template_version=row["template_version"],
        status=PipelineStatus(row["status"]),
        current_step_id=row["current_step_id"],
        current_cycle=row["current_cycle"],
        max_cycles=row["max_cycles"],
        vars=json.loads(row["vars"]),
        step_outputs=json.loads(row["step_outputs"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        error=row["error"],
    )
