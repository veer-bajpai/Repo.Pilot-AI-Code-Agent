"""Runtime persistence for sessions, runs, events, and approvals."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, error TEXT, analysis TEXT, base_commit TEXT, owner TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, task TEXT NOT NULL, approval_mode TEXT NOT NULL, llm_kind TEXT NOT NULL, model TEXT, status TEXT NOT NULL, summary TEXT, error TEXT, steps INTEGER NOT NULL DEFAULT 0, tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0, files_changed INTEGER NOT NULL DEFAULT 0, lines_changed INTEGER NOT NULL DEFAULT 0, tests_passed INTEGER, diff TEXT, created_at REAL NOT NULL, finished_at REAL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, ts REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, tool TEXT NOT NULL, args TEXT NOT NULL, preview TEXT, reason TEXT, status TEXT NOT NULL, note TEXT, created_at REAL NOT NULL, decided_at REAL);
"""

PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, error TEXT, analysis TEXT, base_commit TEXT, owner TEXT NOT NULL DEFAULT '', created_at DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, task TEXT NOT NULL, approval_mode TEXT NOT NULL, llm_kind TEXT NOT NULL, model TEXT, status TEXT NOT NULL, summary TEXT, error TEXT, steps INTEGER NOT NULL DEFAULT 0, tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0, cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0, files_changed INTEGER NOT NULL DEFAULT 0, lines_changed INTEGER NOT NULL DEFAULT 0, tests_passed INTEGER, diff TEXT, created_at DOUBLE PRECISION NOT NULL, finished_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS events (id BIGSERIAL PRIMARY KEY, run_id TEXT NOT NULL, ts DOUBLE PRECISION NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, tool TEXT NOT NULL, args TEXT NOT NULL, preview TEXT, reason TEXT, status TEXT NOT NULL, note TEXT, created_at DOUBLE PRECISION NOT NULL, decided_at DOUBLE PRECISION);
"""


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _decode(row: dict | None, field: str) -> dict | None:
    if row is None:
        return None
    raw = row.get(field)
    if isinstance(raw, str) and raw:
        try:
            row[field] = json.loads(raw)
        except json.JSONDecodeError:
            row[field] = None
    return row


class Database:
    def __init__(self, path: Path | str):
        target = str(path)
        self._postgres = target.startswith(("postgresql://", "postgresql+psycopg://"))
        if self._postgres:
            if psycopg is None:
                raise RuntimeError("psycopg is required when DATABASE_URL uses PostgreSQL.")
            self._conn = psycopg.connect(target.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True, row_factory=dict_row)
        else:
            if target != ":memory:":
                Path(target).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(target, check_same_thread=False, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            if self._postgres:
                for statement in PG_SCHEMA.split(";"):
                    if statement.strip():
                        self._conn.execute(statement)
            else:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.executescript(SCHEMA)
                cols = [r["name"] for r in self._conn.execute("PRAGMA table_info(sessions)").fetchall()]
                if "owner" not in cols:
                    self._conn.execute("ALTER TABLE sessions ADD COLUMN owner TEXT NOT NULL DEFAULT ''")

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self._postgres else sql

    def _exec(self, sql: str, params: tuple = ()):
        with self._lock:
            return self._conn.execute(self._sql(sql), params)

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(self._sql(sql), params).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(self._sql(sql), params).fetchall()]

    def _update(self, table: str, id_: str, **fields: Any) -> None:
        if fields:
            columns = ", ".join(f"{key}=?" for key in fields)
            self._exec(f"UPDATE {table} SET {columns} WHERE id=?", (*fields.values(), id_))

    def create_session(self, source: str, name: str, kind: str, owner: str = "") -> dict:
        sid = new_id()
        self._exec("INSERT INTO sessions (id, source, name, kind, status, owner, created_at) VALUES (?,?,?,?,?,?,?)", (sid, source, name, kind, "cloning", owner, time.time()))
        return self.get_session(sid)  # type: ignore[return-value]

    def get_session(self, sid: str) -> dict | None:
        row = self._one("SELECT * FROM sessions WHERE id=?", (sid,))
        if row:
            row.pop("owner", None)
        return _decode(row, "analysis")

    def list_sessions(self, owner: str | None = None) -> list[dict]:
        rows = self._all("SELECT * FROM sessions" + (" WHERE owner=?" if owner is not None else "") + " ORDER BY created_at DESC", (owner,) if owner is not None else ())
        for row in rows:
            if owner is not None:
                row.pop("owner", None)
        return [_decode(row, "analysis") for row in rows]

    def count_sessions(self, owner: str | None = None) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM sessions" + (" WHERE owner=?" if owner is not None else ""), (owner,) if owner is not None else ())
        return int(row["n"])  # type: ignore[index]

    def session_owner(self, sid: str) -> str | None:
        row = self._one("SELECT owner FROM sessions WHERE id=?", (sid,))
        return row["owner"] if row else None

    def owner_of_run(self, rid: str) -> str | None:
        row = self._one("SELECT s.owner AS owner FROM runs r JOIN sessions s ON s.id=r.session_id WHERE r.id=?", (rid,))
        return row["owner"] if row else None

    def owner_of_approval(self, aid: str) -> str | None:
        row = self._one("SELECT s.owner AS owner FROM approvals a JOIN runs r ON r.id=a.run_id JOIN sessions s ON s.id=r.session_id WHERE a.id=?", (aid,))
        return row["owner"] if row else None

    def expired_session_ids(self, before: float) -> list[str]:
        return [row["id"] for row in self._all("SELECT id FROM sessions WHERE created_at < ?", (before,))]

    def live_cost_since(self, since: float) -> float:
        row = self._one("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM runs WHERE llm_kind='live' AND created_at >= ?", (since,))
        return float(row["c"]) if row else 0.0

    def update_session(self, sid: str, **fields: Any) -> None:
        if "analysis" in fields and not isinstance(fields["analysis"], str):
            fields["analysis"] = json.dumps(fields["analysis"])
        self._update("sessions", sid, **fields)

    def delete_session(self, sid: str) -> None:
        run_ids = [row["id"] for row in self._all("SELECT id FROM runs WHERE session_id=?", (sid,))]
        for rid in run_ids:
            self._exec("DELETE FROM events WHERE run_id=?", (rid,))
            self._exec("DELETE FROM approvals WHERE run_id=?", (rid,))
        self._exec("DELETE FROM runs WHERE session_id=?", (sid,))
        self._exec("DELETE FROM sessions WHERE id=?", (sid,))

    def create_run(self, session_id: str, task: str, approval_mode: str, llm_kind: str, model: str | None) -> dict:
        rid = new_id()
        self._exec("INSERT INTO runs (id, session_id, task, approval_mode, llm_kind, model, status, created_at) VALUES (?,?,?,?,?,?,?,?)", (rid, session_id, task, approval_mode, llm_kind, model, "running", time.time()))
        return self.get_run(rid)  # type: ignore[return-value]

    def get_run(self, rid: str, include_diff: bool = False) -> dict | None:
        row = self._one("SELECT * FROM runs WHERE id=?", (rid,))
        if row and not include_diff:
            row["has_diff"] = bool(row.get("diff"))
            row.pop("diff", None)
        return row

    def list_runs(self, session_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM runs WHERE session_id=? ORDER BY created_at DESC", (session_id,))
        for row in rows:
            row["has_diff"] = bool(row.get("diff"))
            row.pop("diff", None)
        return rows

    def update_run(self, rid: str, **fields: Any) -> None:
        self._update("runs", rid, **fields)

    def active_runs(self) -> list[dict]:
        return self._all("SELECT id, session_id FROM runs WHERE status IN ('running','awaiting_approval')")

    def active_runs_for_owner(self, owner: str) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM runs r JOIN sessions s ON s.id=r.session_id WHERE s.owner=? AND r.status IN ('running','awaiting_approval')", (owner,))
        return int(row["n"]) if row else 0

    def mark_interrupted(self) -> None:
        self._exec("UPDATE runs SET status='interrupted', error='Server restarted while this run was active.', finished_at=? WHERE status IN ('running','awaiting_approval')", (time.time(),))
        self._exec("UPDATE approvals SET status='expired' WHERE status='pending'")

    def add_event(self, run_id: str, kind: str, data: dict) -> int:
        cur = self._exec("INSERT INTO events (run_id, ts, kind, data) VALUES (?,?,?,?)", (run_id, time.time(), kind, json.dumps(data)))
        return int(cur.lastrowid)

    def events_after(self, run_id: str, after: int = 0) -> list[dict]:
        rows = self._all("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id", (run_id, after))
        for row in rows:
            row["data"] = json.loads(row["data"])
        return rows

    def create_approval(self, run_id: str, tool: str, args: dict, preview: str, reason: str) -> dict:
        aid = new_id()
        self._exec("INSERT INTO approvals (id, run_id, tool, args, preview, reason, status, created_at) VALUES (?,?,?,?,?,?,?,?)", (aid, run_id, tool, json.dumps(args), preview, reason, "pending", time.time()))
        return self.get_approval(aid)  # type: ignore[return-value]

    def get_approval(self, aid: str) -> dict | None:
        return _decode(self._one("SELECT * FROM approvals WHERE id=?", (aid,)), "args")

    def list_approvals(self, run_id: str) -> list[dict]:
        return [_decode(row, "args") for row in self._all("SELECT * FROM approvals WHERE run_id=? ORDER BY created_at", (run_id,))]

    def update_approval(self, aid: str, **fields: Any) -> None:
        self._update("approvals", aid, **fields)
