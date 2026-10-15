"""Short-term (session) memory.

A per-run blackboard: agents write intermediate artefacts (client context,
analysis), the orchestrator keeps step records and the run status there, and a
paused run (awaiting human review) is a checkpoint in this store.

`SessionMemory` is in-process. `FileSessionMemory` persists checkpoints as JSON
so a run paused by one CLI process can be resumed by another; production would
use Redis/Postgres with a TTL.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional

from ..errors import RunNotFoundError
from ..models import to_jsonable


class SessionMemory:
    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def create(self, run_id: str, **initial: Any) -> None:
        with self._lock:
            self._runs[run_id] = {"events": [], **initial}

    def exists(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._runs or self._load(run_id) is not None

    def _state(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            if run_id not in self._runs:
                loaded = self._load(run_id)
                if loaded is None:
                    raise RunNotFoundError(f"no session state for run '{run_id}'")
                self._runs[run_id] = loaded
            return self._runs[run_id]

    def put(self, run_id: str, key: str, value: Any) -> None:
        with self._lock:
            self._state(run_id)[key] = value

    def get(self, run_id: str, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._state(run_id).get(key, default)

    def append_event(self, run_id: str, event: dict) -> None:
        with self._lock:
            self._state(run_id)["events"].append(event)

    def snapshot(self, run_id: str) -> dict:
        with self._lock:
            return to_jsonable(self._state(run_id))

    def checkpoint(self, run_id: str) -> None:
        """Persist current state (no-op for the in-memory implementation)."""

    def discard(self, run_id: str) -> None:
        with self._lock:
            self._runs.pop(run_id, None)

    def list_runs(self) -> list[str]:
        with self._lock:
            return sorted(self._runs)

    def _load(self, run_id: str) -> Optional[dict]:
        return None


class FileSessionMemory(SessionMemory):
    def __init__(self, directory: Path):
        super().__init__()
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        safe = "".join(c for c in run_id if c.isalnum() or c in "-_")
        return self.directory / f"{safe}.json"

    def checkpoint(self, run_id: str) -> None:
        with self._lock:
            path = self._path(run_id)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.snapshot(run_id), indent=2, default=str))
            tmp.replace(path)  # atomic on POSIX

    def _load(self, run_id: str) -> Optional[dict]:
        path = self._path(run_id)
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def list_runs(self) -> list[str]:
        return sorted({p.stem for p in self.directory.glob("*.json")} | set(super().list_runs()))
