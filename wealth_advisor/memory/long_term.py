"""Long-term memory: what the system remembers about a client across runs.

* run summaries (risk score history, finding fingerprints) -> trend vs last
  review and new-vs-recurring labelling
* reviewer feedback (dismissed findings) -> confirmed false positives are
  suppressed in future runs instead of re-alerting every time
* CRM snapshots -> fallback profile when the CRM is unavailable

`SQLiteLongTermMemory` is the default (stdlib, file-backed, transactional).
`InMemoryLongTermMemory` backs tests and the browser demo.
"""

from __future__ import annotations

import json
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:  # not bundled in every runtime (e.g. Pyodide loads it as a separate package)
    import sqlite3
except ImportError:  # pragma: no cover
    sqlite3 = None  # type: ignore[assignment]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LongTermMemory(ABC):
    @abstractmethod
    def save_run_summary(self, client_id: str, run_id: str, summary: dict) -> None: ...

    @abstractmethod
    def history(self, client_id: str, limit: int = 10) -> list[dict]: ...

    def last_run_summary(self, client_id: str) -> Optional[dict]:
        h = self.history(client_id, limit=1)
        return h[0] if h else None

    @abstractmethod
    def record_feedback(self, client_id: str, fingerprint: str, decision: str, reviewer: str,
                        note: Optional[str] = None) -> None: ...

    @abstractmethod
    def dismissed_fingerprints(self, client_id: str) -> dict[str, dict]: ...

    @abstractmethod
    def seen_fingerprints(self, client_id: str) -> set[str]: ...

    @abstractmethod
    def save_crm_snapshot(self, client_id: str, profile: dict) -> None: ...

    @abstractmethod
    def get_crm_snapshot(self, client_id: str) -> Optional[tuple[dict, str]]: ...


class InMemoryLongTermMemory(LongTermMemory):
    def __init__(self) -> None:
        self._runs: dict[str, list[dict]] = {}
        self._feedback: dict[str, dict[str, dict]] = {}
        self._crm: dict[str, tuple[dict, str]] = {}

    def save_run_summary(self, client_id, run_id, summary):
        self._runs.setdefault(client_id, []).append({"run_id": run_id, "saved_at": _now(), **summary})

    def history(self, client_id, limit=10):
        return list(reversed(self._runs.get(client_id, [])))[:limit]

    def record_feedback(self, client_id, fingerprint, decision, reviewer, note=None):
        fb = self._feedback.setdefault(client_id, {})
        if decision == "dismiss":
            fb[fingerprint] = {"reviewer": reviewer, "note": note, "at": _now()}
        else:
            fb.pop(fingerprint, None)

    def dismissed_fingerprints(self, client_id):
        return dict(self._feedback.get(client_id, {}))

    def seen_fingerprints(self, client_id):
        return {fp for r in self._runs.get(client_id, []) for fp in r.get("fingerprints", [])}

    def save_crm_snapshot(self, client_id, profile):
        self._crm[client_id] = (profile, _now())

    def get_crm_snapshot(self, client_id):
        return self._crm.get(client_id)


class SQLiteLongTermMemory(LongTermMemory):
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS run_summaries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id TEXT NOT NULL, run_id TEXT NOT NULL UNIQUE,
        saved_at TEXT NOT NULL, summary_json TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS ix_runs_client ON run_summaries(client_id, id);
    CREATE TABLE IF NOT EXISTS finding_feedback (
        client_id TEXT NOT NULL, fingerprint TEXT NOT NULL, decision TEXT NOT NULL,
        reviewer TEXT NOT NULL, note TEXT, decided_at TEXT NOT NULL,
        PRIMARY KEY (client_id, fingerprint));
    CREATE TABLE IF NOT EXISTS crm_snapshots (
        client_id TEXT PRIMARY KEY, profile_json TEXT NOT NULL, saved_at TEXT NOT NULL);
    """

    def __init__(self, path: Path | str):
        if sqlite3 is None:
            raise RuntimeError("sqlite3 is not available in this runtime")
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(self.SCHEMA)

    def save_run_summary(self, client_id, run_id, summary):
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO run_summaries(client_id, run_id, saved_at, summary_json) VALUES (?,?,?,?)",
                (client_id, run_id, _now(), json.dumps(summary, default=str)))

    def history(self, client_id, limit=10):
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, saved_at, summary_json FROM run_summaries WHERE client_id=? ORDER BY id DESC LIMIT ?",
                (client_id, limit)).fetchall()
        return [{"run_id": r["run_id"], "saved_at": r["saved_at"], **json.loads(r["summary_json"])} for r in rows]

    def record_feedback(self, client_id, fingerprint, decision, reviewer, note=None):
        with self._lock, self._conn:
            if decision == "dismiss":
                self._conn.execute(
                    "INSERT OR REPLACE INTO finding_feedback VALUES (?,?,?,?,?,?)",
                    (client_id, fingerprint, decision, reviewer, note, _now()))
            else:  # a later confirmation reverses an earlier dismissal
                self._conn.execute("DELETE FROM finding_feedback WHERE client_id=? AND fingerprint=?",
                                   (client_id, fingerprint))

    def dismissed_fingerprints(self, client_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT fingerprint, reviewer, note, decided_at FROM finding_feedback WHERE client_id=?",
                (client_id,)).fetchall()
        return {r["fingerprint"]: {"reviewer": r["reviewer"], "note": r["note"], "at": r["decided_at"]} for r in rows}

    def seen_fingerprints(self, client_id):
        return {fp for h in self.history(client_id, limit=50) for fp in h.get("fingerprints", [])}

    def save_crm_snapshot(self, client_id, profile):
        with self._lock, self._conn:
            self._conn.execute("INSERT OR REPLACE INTO crm_snapshots VALUES (?,?,?)",
                               (client_id, json.dumps(profile, default=str), _now()))

    def get_crm_snapshot(self, client_id):
        with self._lock:
            row = self._conn.execute("SELECT profile_json, saved_at FROM crm_snapshots WHERE client_id=?",
                                     (client_id,)).fetchone()
        return (json.loads(row["profile_json"]), row["saved_at"]) if row else None

    def close(self) -> None:
        self._conn.close()


def create_long_term_memory(path: Optional[Path]) -> LongTermMemory:
    """SQLite when available (absent in some runtimes, e.g. Pyodide without the sqlite3 package)."""
    if path is None or sqlite3 is None:
        return InMemoryLongTermMemory()
    try:
        return SQLiteLongTermMemory(path)
    except (sqlite3.Error, OSError):
        return InMemoryLongTermMemory()
