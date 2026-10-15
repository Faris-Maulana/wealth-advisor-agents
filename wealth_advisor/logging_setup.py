"""Structured logging with run/agent/step correlation.

* Every log record is enriched with the current `run_id`, `client_id`, `agent`
  and `step` from a contextvar, so a single run can be reconstructed from a
  shared log stream (grep by run_id, or load the JSONL into any log store).
* Events are emitted as `event` names plus structured fields rather than
  free-text messages, which keeps them queryable.
* `TraceRecorder` keeps an in-memory copy per run so the report and the demo UI
  can show the execution timeline without parsing files.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import sys
import threading
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

ROOT_LOGGER = "wealth_advisor"
_context: contextvars.ContextVar[dict] = contextvars.ContextVar("wa_log_context", default={})


@contextlib.contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    token = _context.set({**_context.get(), **{k: v for k, v in fields.items() if v is not None}})
    try:
        yield
    finally:
        _context.reset(token)


def current_context() -> dict:
    return dict(_context.get())


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.ctx = current_context()
        if not hasattr(record, "fields"):
            record.fields = {}
        return True


def _record_to_dict(record: logging.LogRecord) -> dict:
    out = {
        "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
        "level": record.levelname,
        "logger": record.name,
        "event": record.getMessage(),
        **getattr(record, "ctx", {}),
        **getattr(record, "fields", {}),
    }
    if record.exc_info:
        out["exception"] = logging.Formatter().formatException(record.exc_info)
    return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(_record_to_dict(record), default=str)


class ConsoleFormatter(logging.Formatter):
    COLORS = {"DEBUG": "\033[37m", "INFO": "\033[36m", "WARNING": "\033[33m", "ERROR": "\033[31m",
              "CRITICAL": "\033[41m"}
    RESET = "\033[0m"

    def __init__(self, color: bool = True):
        super().__init__()
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        d = _record_to_dict(record)
        ctx = getattr(record, "ctx", {})
        who = ctx.get("agent") or record.name.removeprefix(ROOT_LOGGER + ".")
        fields = {k: v for k, v in getattr(record, "fields", {}).items()}
        tail = " ".join(f"{k}={_short(v)}" for k, v in fields.items())
        level = record.levelname
        if self.color:
            level = f"{self.COLORS.get(level, '')}{level:<7}{self.RESET}"
        else:
            level = f"{level:<7}"
        line = f"{d['ts'][11:23]} {level} [{who}] {d['event']} {tail}".rstrip()
        if record.exc_info:
            line += "\n" + logging.Formatter().formatException(record.exc_info)
        return line


def _short(v: Any, limit: int = 120) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s if len(s) <= limit else s[: limit - 3] + "..."


class TraceRecorder(logging.Handler):
    """Bounded in-memory sink of structured events, grouped by run_id."""

    def __init__(self, max_events_per_run: int = 2000, max_runs: int = 200):
        super().__init__(level=logging.DEBUG)
        self._runs: dict[str, deque] = defaultdict(lambda: deque(maxlen=max_events_per_run))
        self._order: deque[str] = deque()
        self._max_runs = max_runs
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        run_id = getattr(record, "ctx", {}).get("run_id")
        if not run_id:
            return
        with self._lock:
            if run_id not in self._runs:
                self._order.append(run_id)
                if len(self._order) > self._max_runs:
                    self._runs.pop(self._order.popleft(), None)
            self._runs[run_id].append(_record_to_dict(record))

    def events(self, run_id: str) -> list[dict]:
        with self._lock:
            return list(self._runs.get(run_id, ()))


_recorder = TraceRecorder()
_configured = False


def trace_recorder() -> TraceRecorder:
    return _recorder


def configure_logging(level: str = "INFO", log_file: Optional[Path] = None, console: bool = True,
                      json_console: bool = False, color: Optional[bool] = None) -> None:
    """Idempotent logging setup. Safe to call from CLI, tests, API and the browser demo."""
    global _configured
    root = logging.getLogger(ROOT_LOGGER)
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.DEBUG)
    root.propagate = False
    ctx_filter = _ContextFilter()

    if not _recorder.filters:
        _recorder.addFilter(ctx_filter)
    root.addHandler(_recorder)

    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(level.upper())
        use_color = sys.stderr.isatty() if color is None else color
        ch.setFormatter(JsonFormatter() if json_console else ConsoleFormatter(color=use_color))
        ch.addFilter(ctx_filter)
        root.addHandler(ch)
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(JsonFormatter())
        fh.addFilter(ctx_filter)
        root.addHandler(fh)
    _configured = True


def get_logger(name: str) -> "EventLogger":
    if not _configured:
        configure_logging(console=False)
    return EventLogger(logging.getLogger(f"{ROOT_LOGGER}.{name}"))


class EventLogger:
    """Thin wrapper: `log.info("tool.call", tool="crm", attempt=2)`."""

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    def _log(self, level: int, event: str, exc_info: bool = False, **fields: Any) -> None:
        self._logger.log(level, event, extra={"fields": fields}, exc_info=exc_info)

    def debug(self, event: str, **fields: Any) -> None:
        self._log(logging.DEBUG, event, **fields)

    def info(self, event: str, **fields: Any) -> None:
        self._log(logging.INFO, event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._log(logging.WARNING, event, **fields)

    def error(self, event: str, exc_info: bool = False, **fields: Any) -> None:
        self._log(logging.ERROR, event, exc_info=exc_info, **fields)
