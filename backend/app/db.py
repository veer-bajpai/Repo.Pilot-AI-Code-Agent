"""SQLite persistence. Holds sessions, runs, events, approvals. Never holds secrets."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,              -- repo | demo | local
    status TEXT NOT NULL,            -- cloning | ready | error
    error TEXT,
    analysis TEXT,                   -- JSON
    base_commit TEXT,
    owner TEXT NOT NULL DEFAULT '',  -- anonymous per-browser id (cookie), scopes every session/run
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    task TEXT NOT NULL,
    approval_mode TEXT NOT NULL,
    llm_kind TEXT NOT NULL,          -- live | simulated
    model TEXT,
    status TEXT NOT NULL,            -- running | awaiting_approval | completed | failed | cancelled | budget_exceeded | interrupted
    summary TEXT,
    error TEXT,
    steps INTEGER NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    files_changed INTEGER NOT NULL DEFAULT 0,
    lines_changed INTEGER NOT NULL DEFAULT 0,
    tests_passed INTEGER,            -- NULL unknown, 0/1
    diff TEXT,
    created_at REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    preview TEXT,
    reason TEXT,
    status TEXT NOT NULL,            -- pending | approved | rejected | expired
    note TEXT,
    created_at REAL NOT NULL,
    decided_at REAL
);
"""


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            cols = [r["name"] for r in self._conn.execute("PRAGMA table_info(sessions)").fetchall()]
            if "owner" not in cols:   # database created by an earlier version
                self._conn.execute("ALTER TABLE sessions ADD COLUMN owner TEXT NOT NULL DEFAULT ''")

    # -- generic helpers ---------------------------------------------------
    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _update(self, table: str, id_: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE {table} SET {cols} WHERE id=?", (*fields.values(), id_))

    # -- sessions ----------------------------------------------------------
    def create_session(self, source: str, name: str, kind: str, owner: str = "") -> dict:
        sid = new_id()
        self._exec(
            "INSERT INTO sessions (id, source, name, kind, status, owner, created_at) VALUES (?,?,?,?,?,?,?)",
            (sid, source, name, kind, "cloning", owner, time.time()),
        )
        return self.get_session(sid)  # type: ignore[return-value]

    def get_session(self, sid: str) -> dict | None:
        row = self._one("SELECT * FROM sessions WHERE id=?", (sid,))
        if row:
            row.pop("owner", None)
        return _decode(row, "analysis")

    def list_sessions(self, owner: str | None = None) -> list[dict]:
        if owner is None:
            rows = self._all("SELECT * FROM sessions ORDER BY created_at DESC")
        else:
            rows = self._all("SELECT * FROM sessions WHERE owner=? ORDER BY created_at DESC", (owner,))
        for r in rows:
            r.pop("owner", None) if owner is not None else None   # never echo ids back to the browser
        return [_decode(r, "analysis") for r in rows]

    def count_sessions(self, owner: str | None = None) -> int:
        if owner is None:
            return self._one("SELECT COUNT(*) AS n FROM sessions")["n"]  # type: ignore[index]
        return self._one("SELECT COUNT(*) AS n FROM sessions WHERE owner=?", (owner,))["n"]  # type: ignore[index]

    def session_owner(self, sid: str) -> str | None:
        row = self._one("SELECT owner FROM sessions WHERE id=?", (sid,))
        return row["owner"] if row else None

    def owner_of_run(self, rid: str) -> str | None:
        row = self._one("SELECT s.owner AS owner FROM runs r JOIN sessions s ON s.id=r.session_id WHERE r.id=?", (rid,))
        return row["owner"] if row else None

    def owner_of_approval(self, aid: str) -> str | None:
        row = self._one("SELECT s.owner AS owner FROM approvals a JOIN runs r ON r.id=a.run_id "
                        "JOIN sessions s ON s.id=r.session_id WHERE a.id=?", (aid,))
        return row["owner"] if row else None

    def expired_session_ids(self, before: float) -> list[str]:
        return [r["id"] for r in self._all("SELECT id FROM sessions WHERE created_at < ?", (before,))]

    def live_cost_since(self, since: float) -> float:
        row = self._one("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM runs WHERE llm_kind='live' AND created_at >= ?", (since,))
        return float(row["c"]) if row else 0.0

    def update_session(self, sid: str, **fields: Any) -> None:
        if "analysis" in fields and not isinstance(fields["analysis"], str):
            fields["analysis"] = json.dumps(fields["analysis"])
        self._update("sessions", sid, **fields)

    def delete_session(self, sid: str) -> None:
        run_ids = [r["id"] for r in self._all("SELECT id FROM runs WHERE session_id=?", (sid,))]
        for rid in run_ids:
            self._exec("DELETE FROM events WHERE run_id=?", (rid,))
            self._exec("DELETE FROM approvals WHERE run_id=?", (rid,))
        self._exec("DELETE FROM runs WHERE session_id=?", (sid,))
        self._exec("DELETE FROM sessions WHERE id=?", (sid,))

    # -- runs --------------------------------------------------------------
    def create_run(self, session_id: str, task: str, approval_mode: str, llm_kind: str, model: str | None) -> dict:
        rid = new_id()
        self._exec(
            "INSERT INTO runs (id, session_id, task, approval_mode, llm_kind, model, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (rid, session_id, task, approval_mode, llm_kind, model, "running", time.time()),
        )
        return self.get_run(rid)  # type: ignore[return-value]

    def get_run(self, rid: str, include_diff: bool = False) -> dict | None:
        row = self._one("SELECT * FROM runs WHERE id=?", (rid,))
        if row and not include_diff:
            row["has_diff"] = bool(row.get("diff"))
            row.pop("diff", None)
        return row

    def list_runs(self, session_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM runs WHERE session_id=? ORDER BY created_at DESC", (session_id,))
        for r in rows:
            r["has_diff"] = bool(r.get("diff"))
            r.pop("diff", None)
        return rows

    def update_run(self, rid: str, **fields: Any) -> None:
        self._update("runs", rid, **fields)

    def active_runs(self) -> list[dict]:
        return self._all("SELECT id, session_id FROM runs WHERE status IN ('running','awaiting_approval')")

    def mark_interrupted(self) -> None:
        self._exec(
            "UPDATE runs SET status='interrupted', error='Server restarted while this run was active.', finished_at=? "
            "WHERE status IN ('running','awaiting_approval')",
            (time.time(),),
        )
        self._exec("UPDATE approvals SET status='expired' WHERE status='pending'")

    # -- events ------------------------------------------------------------
    def add_event(self, run_id: str, kind: str, data: dict) -> int:
        cur = self._exec(
            "INSERT INTO events (run_id, ts, kind, data) VALUES (?,?,?,?)",
            (run_id, time.time(), kind, json.dumps(data)),
        )
        return int(cur.lastrowid)

    def events_after(self, run_id: str, after: int = 0) -> list[dict]:
        rows = self._all("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id", (run_id, after))
        for r in rows:
            r["data"] = json.loads(r["data"])
        return rows

    # -- approvals ---------------------------------------------------------
    def create_approval(self, run_id: str, tool: str, args: dict, preview: str, reason: str) -> dict:
        aid = new_id()
        self._exec(
            "INSERT INTO approvals (id, run_id, tool, args, preview, reason, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (aid, run_id, tool, json.dumps(args), preview, reason, "pending", time.time()),
        )
        return self.get_approval(aid)  # type: ignore[return-value]

    def get_approval(self, aid: str) -> dict | None:
        return _decode(self._one("SELECT * FROM approvals WHERE id=?", (aid,)), "args")

    def list_approvals(self, run_id: str) -> list[dict]:
        return [_decode(r, "args") for r in self._all("SELECT * FROM approvals WHERE run_id=? ORDER BY created_at", (run_id,))]

    def update_approval(self, aid: str, **fields: Any) -> None:
        self._update("approvals", aid, **fields)


def _decode(row: dict | None, json_field: str) -> dict | None:
    if row is None:
        return None
    raw = row.get(json_field)
    if isinstance(raw, str) and raw:
        try:
            row[json_field] = json.loads(raw)
        except json.JSONDecodeError:
            row[json_field] = None
    return row
