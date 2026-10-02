"""NextPull's application log: one place that records what the app did and what went wrong, safe to send to someone else.

* A rotating file ``app.log`` (1 MB x 6) in the data folder plus an in-memory ring (so tests and mock runs need no files).
* **Redaction is built in**: every handler carries :class:`RedactingFilter`, so nothing is stored unredacted. Registered secrets (the Nextcloud app
  password, the per-run rclone control password) and common secret shapes (``password=``, ``Authorization: Basic ...``, credentials inside a
  URL, ``--rc-pass x``, ``RCLONE_CONFIG_NC_PASS=x``) become ``<hidden>``. Reports use the same :func:`redact`.
* Unhandled exceptions, also in background threads, are logged with their traceback (:func:`install_excepthooks`).

Logger names: ``nextpull.app`` (startup, bridge), ``nextpull.engine`` (scheduler, queue), ``nextpull.run`` (one rclone run), ``nextpull.win``
(Windows features), ``nextpull.ui`` (errors reported by the window's JavaScript).
"""
from __future__ import annotations

import collections
import logging
import logging.handlers
import re
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Iterable

LOGGER_NAME = "nextpull"
FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_secrets: set[str] = set()
_hooks_installed = False
_lock = threading.Lock()

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/=_-]{6,}"), "Basic <hidden>"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer <hidden>"),
    (re.compile(r"(?i)\b(pass(?:word)?|passwd|token|secret|api[_-]?key|authorization|app_password)\b(\s*[\"']?\s*[:=]\s*[\"']?)(?!<hidden>|Basic <hidden>|Bearer <hidden>)([^\s\"',;&]+)"), r"\1\2<hidden>"),
    (re.compile(r"(?i)(--rc-pass|--password|--pass)([\s=]+)(\S+)"), r"\1\2<hidden>"),
    (re.compile(r"(?i)(RCLONE_CONFIG_\w*PASS\w*)=\S+"), r"\1=<hidden>"),
    (re.compile(r"(://[^/\s:@]+:)([^@\s/]+)(@)"), r"\1<hidden>\3"),
]


def register_secret(value: str | None) -> None:
    """Make :func:`redact` hide this exact value wherever it appears (a password that does not look like one)."""
    if value and len(value) >= 4:
        with _lock:
            _secrets.add(value)


def redact(text: Any, extra: Iterable[str] = ()) -> str:
    out = str(text)
    with _lock:
        known = set(_secrets)
    for secret in sorted(known | {s for s in extra if s and len(s) >= 4}, key=len, reverse=True):
        out = out.replace(secret, "<hidden>")
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out


class RedactingFilter(logging.Filter):
    """Redacts the message and folds the traceback into it (exception text can hold secrets too). Idempotent per record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "_redacted", False):
            return True
        message = record.getMessage()
        if record.exc_info and record.exc_info[0] is not None:
            message += "\n" + "".join(traceback.format_exception(*record.exc_info)).rstrip()
        record.msg, record.args, record.exc_info, record.exc_text = redact(message), None, None, None
        record._redacted = True   # type: ignore[attr-defined]
        return True


class RingHandler(logging.Handler):
    """The last N records in memory, structured, for the Diagnostics page and for instances without a data folder."""

    def __init__(self, maxlen: int = 3000) -> None:
        super().__init__()
        self.buffer: collections.deque[dict[str, str]] = collections.deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        self.buffer.append({"time": self.formatTime(record), "level": record.levelname, "name": record.name, "message": record.getMessage()})

    @staticmethod
    def formatTime(record: logging.LogRecord) -> str:
        return logging.Formatter().formatTime(record, "%Y-%m-%d %H:%M:%S")


logging.getLogger(LOGGER_NAME).addHandler(logging.NullHandler())   # no 'last resort' stderr output when no handler is installed (tests)


def get_logger(name: str = "app") -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def _root() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def setup_logging(data_dir: Path | None, level: str = "INFO") -> logging.Logger:
    """Idempotent: replaces the handlers of an earlier call. ``data_dir=None`` keeps everything in memory."""
    root = _root()
    teardown_logging()
    root.setLevel(LEVELS.get(level.upper(), 20))
    root.propagate = False
    # A logger's own filters do not run for records of its children, so the filter sits on every handler: nothing is stored unredacted.
    ring = RingHandler()
    ring.addFilter(RedactingFilter())
    ring._nextpull = True   # type: ignore[attr-defined]
    root.addHandler(ring)
    if data_dir:
        data_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(data_dir / "app.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter(FORMAT))
        handler.addFilter(RedactingFilter())
        handler._nextpull = True   # type: ignore[attr-defined]
        root.addHandler(handler)
    return root


def teardown_logging() -> None:
    """Close and remove our handlers (important on Windows: an open log file blocks deleting its folder)."""
    root = _root()
    for handler in list(root.handlers):
        if getattr(handler, "_nextpull", False):
            root.removeHandler(handler)
            try:
                handler.close()
            except OSError:
                pass


def set_level(level: str) -> None:
    _root().setLevel(LEVELS.get(level.upper(), 20))


def ring() -> RingHandler | None:
    return next((h for h in _root().handlers if isinstance(h, RingHandler)), None)


def read_log(data_dir: Path | None, tail: int = 300, level: str = "", query: str = "") -> list[dict[str, str]]:
    """Recent records, newest last. Reads ``app.log`` (and the previous file if needed); falls back to the in-memory ring."""
    entries: list[dict[str, str]] = []
    if data_dir and (data_dir / "app.log").is_file():
        for name in ("app.log.1", "app.log"):
            path = data_dir / name
            if not path.is_file():
                continue
            current: dict[str, str] | None = None
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = line.split(" | ", 3)
                if len(parts) == 4 and re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", parts[0]):
                    current = {"time": parts[0][:19], "level": parts[1].strip(), "name": parts[2].strip(), "message": parts[3]}
                    entries.append(current)
                elif current is not None:   # continuation line (traceback)
                    current["message"] += "\n" + line
    else:
        handler = ring()
        entries = list(handler.buffer) if handler else []
    minimum = LEVELS.get(level.upper(), 0)
    needle = query.lower().strip()
    result = [e for e in entries if LEVELS.get(e["level"], 20) >= minimum and (not needle or needle in e["message"].lower() or needle in e["name"].lower())]
    return result[-max(1, min(5000, tail)):]


def install_excepthooks() -> None:
    """Log unhandled exceptions (main thread and background threads) with their traceback, then run the previous hook."""
    global _hooks_installed
    with _lock:
        if _hooks_installed:
            return
        _hooks_installed = True
    log = get_logger("app")
    previous_sys, previous_thread = sys.excepthook, threading.excepthook

    def sys_hook(exc_type: Any, exc: BaseException, tb: Any) -> None:
        if not issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
        previous_sys(exc_type, exc, tb)

    def thread_hook(args: Any) -> None:
        if args.exc_type is not SystemExit:
            log.critical("Unhandled exception in thread %s", getattr(args.thread, "name", "?"), exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        previous_thread(args)

    sys.excepthook, threading.excepthook = sys_hook, thread_hook
