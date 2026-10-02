"""Persistence for NextPull: validated job/settings models, atomic JSON files, DPAPI-protected credentials and the
SQLite history/statistics database.

Persistence is opt-in per instance: every store takes ``data_dir=None`` and then keeps everything in memory, so tests
and mock runs never touch the user's real data. Secrets (the Nextcloud app password) never go into ``jobs.json``,
``settings.json``, the history database or any log: only :class:`SecretStore` holds them.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from rclone_logic import valid_bwlimit
from scheduler import ALL_DAYS, clean_schedule

APP_NAME = "NextPull"
MAX_NAME = 60
RUN_STATES = ("running", "ok", "partial", "failed", "cancelled", "stopped", "missed", "skipped", "interrupted")


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(timespec="seconds")


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------------------------------------------------- models

def _int(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _float(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _patterns(value: Any) -> list[str]:
    if isinstance(value, str):
        value = value.splitlines()
    result: list[str] = []
    for item in value or []:
        text = str(item).strip()
        if text and text not in result:
            result.append(text[:200])
    return result[:100]


def clean_remote_path(raw: Any) -> str:
    """Normalise a path inside the Nextcloud 'Files' root: forward slashes, no leading/trailing slash, no '..'."""
    text = str(raw or "").replace("\\", "/").strip().strip("/")
    parts = [part for part in text.split("/") if part and part != "."]
    if any(part == ".." for part in parts):
        raise ValueError("The Nextcloud folder cannot contain '..'")
    return "/".join(parts)


def new_job_id() -> str:
    return uuid4().hex[:8]


def default_job() -> dict[str, Any]:
    return {
        "id": "", "name": "", "enabled": True, "source": "", "mode": "copy", "dest": "",
        "schedule": {"days": list(ALL_DAYS), "time": "02:00", "stop_by": ""},
        "catch_up": True, "catch_up_hours": 12,
        "speed": {"limit_mbps": 0, "timetable": ""},
        "transfers": 1, "streams": 4, "min_age_minutes": 5,
        "excludes": [], "includes": [],
        "checksum": False, "dry_run": False, "delete_empty_dirs": True, "retries": 3,
    }


def clean_job(raw: Any) -> dict[str, Any]:
    """Whitelist, coerce and validate a job. Raises ValueError with a message fit for the UI."""
    if not isinstance(raw, dict):
        raise ValueError("Invalid job")
    base = default_job()
    name = str(raw.get("name") or "").strip()[:MAX_NAME]
    if not name:
        raise ValueError("Give the job a name")
    source = clean_remote_path(raw.get("source"))
    if not source:
        raise ValueError("Pick the Nextcloud folder to download")
    dest = str(raw.get("dest") or "").strip()
    if not dest:
        raise ValueError("Pick the destination folder on this PC")
    mode = str(raw.get("mode") or "copy").lower()
    if mode not in ("copy", "move"):
        raise ValueError("Mode must be copy or move")
    speed = raw.get("speed") if isinstance(raw.get("speed"), dict) else {}
    timetable = str(speed.get("timetable") or "").strip()[:300]
    if timetable and not valid_bwlimit(timetable):
        raise ValueError("The speed timetable is not valid. Examples: 2M, off, or 08:00,2M 23:00,off")
    job = base | {
        "id": str(raw.get("id") or "").strip() or new_job_id(), "name": name, "enabled": bool(raw.get("enabled", True)),
        "source": source, "mode": mode, "dest": dest,
        "schedule": clean_schedule(raw.get("schedule")),
        "catch_up": bool(raw.get("catch_up", True)), "catch_up_hours": _float(raw.get("catch_up_hours"), 0.5, 72, 12),
        "speed": {"limit_mbps": _float(speed.get("limit_mbps"), 0, 100000, 0), "timetable": timetable},
        "transfers": _int(raw.get("transfers"), 1, 8, 1), "streams": _int(raw.get("streams"), 1, 16, 4),
        "min_age_minutes": _int(raw.get("min_age_minutes"), 0, 1440, 5),
        "excludes": _patterns(raw.get("excludes")), "includes": _patterns(raw.get("includes")),
        "checksum": bool(raw.get("checksum", False)), "dry_run": bool(raw.get("dry_run", False)),
        "delete_empty_dirs": bool(raw.get("delete_empty_dirs", True)), "retries": _int(raw.get("retries"), 0, 10, 3),
    }
    return job


def default_settings() -> dict[str, Any]:
    return {"notifications": True, "close_to_tray": True, "prevent_sleep": True, "rclone_path": "", "history_days": 365, "log_days": 90}


def clean_settings(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    base = default_settings()
    return {
        "notifications": bool(raw.get("notifications", base["notifications"])),
        "close_to_tray": bool(raw.get("close_to_tray", base["close_to_tray"])),
        "prevent_sleep": bool(raw.get("prevent_sleep", base["prevent_sleep"])),
        "rclone_path": str(raw.get("rclone_path") or "").strip()[:500],
        "history_days": _int(raw.get("history_days"), 7, 3650, base["history_days"]),
        "log_days": _int(raw.get("log_days"), 1, 3650, base["log_days"]),
    }


# --------------------------------------------------------------------------------------------------- config store

class ConfigStore:
    """jobs.json + settings.json + the scheduler's ``last_checked`` marker. Thread-safe."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir
        self._lock = threading.RLock()
        self._jobs: list[dict[str, Any]] = []
        self._settings = default_settings()
        self._state: dict[str, Any] = {}
        if data_dir:
            self._jobs = [job for job in (self._safe_job(item) for item in read_json(data_dir / "jobs.json", [])) if job]
            self._settings = clean_settings(read_json(data_dir / "settings.json", {}))
            self._state = read_json(data_dir / "state.json", {})

    @staticmethod
    def _safe_job(item: Any) -> dict[str, Any] | None:
        try:
            return clean_job(item)
        except ValueError:
            return None

    def _save(self, name: str, data: Any) -> None:
        if self.data_dir:
            write_json_atomic(self.data_dir / name, data)

    def jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(self._jobs))

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            return next((json.loads(json.dumps(job)) for job in self._jobs if job["id"] == job_id), None)

    def save_job(self, raw: Any) -> dict[str, Any]:
        job = clean_job(raw)
        with self._lock:
            for index, existing in enumerate(self._jobs):
                if existing["id"] == job["id"]:
                    self._jobs[index] = job
                    break
            else:
                self._jobs.append(job)
            self._save("jobs.json", self._jobs)
        return job

    def delete_job(self, job_id: str) -> bool:
        with self._lock:
            before = len(self._jobs)
            self._jobs = [job for job in self._jobs if job["id"] != job_id]
            self._save("jobs.json", self._jobs)
            return len(self._jobs) != before

    def settings(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._settings)

    def save_settings(self, raw: Any) -> dict[str, Any]:
        with self._lock:
            self._settings = clean_settings(raw)
            self._save("settings.json", self._settings)
            return dict(self._settings)

    def state(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._state.get(key, default)

    def set_state(self, key: str, value: Any) -> None:
        with self._lock:
            self._state[key] = value
            self._save("state.json", self._state)

    def export_jobs(self) -> str:
        """Jobs as JSON text for backup/sharing. Contains no credentials."""
        return json.dumps({"app": APP_NAME, "version": 1, "jobs": self.jobs()}, ensure_ascii=False, indent=2)

    def import_jobs(self, text: str) -> int:
        try:
            data = json.loads(text)
        except ValueError as error:
            raise ValueError(f"Not valid JSON: {error}") from error
        items = data.get("jobs") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ValueError("No jobs found in the file")
        count = 0
        for item in items:
            item = dict(item) if isinstance(item, dict) else {}
            item["id"] = ""   # imported jobs always get a fresh id: never overwrite an existing job
            self.save_job(item)
            count += 1
        return count


# --------------------------------------------------------------------------------------------------------- secrets

class SecretStore:
    """The Nextcloud login (server, user, app password). On disk it is a single DPAPI-protected blob that only this
    Windows user can decrypt; with ``data_dir=None`` it lives in memory only."""

    def __init__(self, data_dir: Path | None = None, protect: Callable[[bytes], bytes] | None = None, unprotect: Callable[[bytes], bytes] | None = None) -> None:
        self.data_dir = data_dir
        self._protect, self._unprotect = protect, unprotect
        self._memory: dict[str, str] | None = None
        self._lock = threading.Lock()

    def _codec(self) -> tuple[Callable[[bytes], bytes], Callable[[bytes], bytes]]:
        if self._protect and self._unprotect:
            return self._protect, self._unprotect
        import winutil
        return winutil.dpapi_protect, winutil.dpapi_unprotect

    def save(self, server: str, user: str, app_password: str) -> None:
        data = {"server": server, "user": user, "app_password": app_password}
        with self._lock:
            self._memory = data
            if self.data_dir:
                protect, _ = self._codec()
                path = self.data_dir / "credentials.bin"
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(f".credentials.{uuid4().hex}.tmp")
                temporary.write_bytes(protect(json.dumps(data).encode("utf-8")))
                os.replace(temporary, path)

    def load(self) -> dict[str, str] | None:
        with self._lock:
            if self._memory is not None:
                return dict(self._memory)
            if self.data_dir and (self.data_dir / "credentials.bin").is_file():
                try:
                    _, unprotect = self._codec()
                    data = json.loads(unprotect((self.data_dir / "credentials.bin").read_bytes()).decode("utf-8"))
                except Exception:   # corrupt file or another Windows user: treat as not connected
                    return None
                if all(isinstance(data.get(key), str) for key in ("server", "user", "app_password")):
                    self._memory = data
                    return dict(data)
            return None

    def clear(self) -> None:
        with self._lock:
            self._memory = None
            if self.data_dir:
                try:
                    (self.data_dir / "credentials.bin").unlink()
                except OSError:
                    pass

    def connected(self) -> bool:
        return self.load() is not None


# ------------------------------------------------------------------------------------------------------- history

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, job_name TEXT, trigger TEXT, mode TEXT, source TEXT, dest TEXT,
  dry_run INTEGER DEFAULT 0, started TEXT, finished TEXT, state TEXT, bytes INTEGER DEFAULT 0, files INTEGER DEFAULT 0,
  deleted INTEGER DEFAULT 0, errors INTEGER DEFAULT 0, avg_speed REAL DEFAULT 0, message TEXT DEFAULT '', log_file TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, name TEXT, size INTEGER DEFAULT 0, started TEXT, finished TEXT,
  duration REAL DEFAULT 0, speed REAL DEFAULT 0, status TEXT, error TEXT DEFAULT '', deleted INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started);
CREATE INDEX IF NOT EXISTS idx_files_run ON files(run_id);
"""


class HistoryDb:
    """Runs and per-file results. One connection guarded by a lock (the engine thread and the UI thread both use it)."""

    def __init__(self, path: Path | None = None) -> None:
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path) if path else ":memory:", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _rows(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._db.execute(sql, args).fetchall()]

    def _exec(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cursor = self._db.execute(sql, args)
            self._db.commit()
            return cursor.lastrowid or 0

    # runs
    def start_run(self, job: dict[str, Any], trigger: str, log_file: str = "", started: str | None = None) -> int:
        return self._exec(
            "INSERT INTO runs(job_id, job_name, trigger, mode, source, dest, dry_run, started, state, log_file) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (job["id"], job["name"], trigger, job["mode"], job["source"], job["dest"], int(job.get("dry_run", False)), started or now_iso(), "running", log_file))

    def set_log_file(self, run_id: int, log_file: str) -> None:
        self._exec("UPDATE runs SET log_file=? WHERE id=?", (log_file, run_id))

    def record_skipped(self, job: dict[str, Any], slot: datetime, state: str, message: str) -> int:
        stamp = slot.replace(microsecond=0).isoformat(timespec="seconds")
        return self._exec(
            "INSERT INTO runs(job_id, job_name, trigger, mode, source, dest, dry_run, started, finished, state, message) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (job["id"], job["name"], "schedule", job["mode"], job["source"], job["dest"], int(job.get("dry_run", False)), stamp, stamp, state, message))

    def add_file(self, run_id: int, name: str, size: int, status: str, *, started: str = "", finished: str = "", duration: float = 0.0, error: str = "", deleted: bool = False) -> int:
        speed = size / duration if duration > 0 and size > 0 else 0.0
        return self._exec(
            "INSERT INTO files(run_id, name, size, started, finished, duration, speed, status, error, deleted) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, name, int(size), started, finished or now_iso(), float(duration), speed, status, error[:500], int(deleted)))

    def set_file_note(self, run_id: int, name: str, note: str) -> None:
        """Attach a note to a file row (e.g. 'downloaded, but could not be deleted on Nextcloud')."""
        self._exec("UPDATE files SET error=? WHERE run_id=? AND name=? AND status IN ('ok','present')", (note[:500], run_id, name))

    def mark_file_deleted(self, run_id: int, name: str) -> None:
        self._exec("UPDATE files SET deleted=1 WHERE run_id=? AND name=?", (run_id, name))

    def finish_run(self, run_id: int, state: str, message: str = "", finished: str | None = None, errors: int | None = None) -> None:
        with self._lock:
            totals = self._db.execute(
                "SELECT COALESCE(SUM(CASE WHEN status='ok' THEN size END),0) AS bytes, COALESCE(SUM(status='ok'),0) AS files, "
                "COALESCE(SUM(deleted),0) AS deleted, COALESCE(SUM(status='error'),0) AS errors, "
                "COALESCE(SUM(CASE WHEN status='ok' THEN duration END),0) AS seconds FROM files WHERE run_id=?", (run_id,)).fetchone()
            speed = totals["bytes"] / totals["seconds"] if totals["seconds"] > 0 else 0.0
            self._db.execute(
                "UPDATE runs SET state=?, message=?, finished=?, bytes=?, files=?, deleted=?, errors=?, avg_speed=? WHERE id=?",
                (state, message[:500], finished or now_iso(), totals["bytes"], totals["files"], totals["deleted"],
                 totals["errors"] if errors is None else max(errors, totals["errors"]), speed, run_id))
            self._db.commit()

    def recover_interrupted(self) -> int:
        """Runs still 'running' at startup were cut off by a crash, a reboot or a power loss."""
        with self._lock:
            cursor = self._db.execute("UPDATE runs SET state='interrupted', finished=?, message='The app stopped while this run was in progress' WHERE state='running'", (now_iso(),))
            self._db.commit()
            return cursor.rowcount

    def list_runs(self, limit: int = 50, offset: int = 0, job_id: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM runs", ()
        if job_id:
            sql, args = sql + " WHERE job_id=?", (job_id,)
        return self._rows(sql + " ORDER BY started DESC, id DESC LIMIT ? OFFSET ?", args + (max(1, min(500, limit)), max(0, offset)))

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM runs WHERE id=?", (run_id,))
        return rows[0] if rows else None

    def list_files(self, run_id: int) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM files WHERE run_id=? ORDER BY id", (run_id,))

    def last_run(self, job_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM runs WHERE job_id=? AND state NOT IN ('missed','skipped') ORDER BY started DESC, id DESC LIMIT 1", (job_id,))
        return rows[0] if rows else None

    def prune(self, days: int) -> int:
        cutoff = (datetime.now() - timedelta(days=days)).replace(microsecond=0).isoformat(timespec="seconds")
        with self._lock:
            self._db.execute("DELETE FROM files WHERE run_id IN (SELECT id FROM runs WHERE started < ? AND state != 'running')", (cutoff,))
            cursor = self._db.execute("DELETE FROM runs WHERE started < ? AND state != 'running'", (cutoff,))
            self._db.commit()
            return cursor.rowcount

    def clear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM files")
            self._db.execute("DELETE FROM runs WHERE state != 'running'")
            self._db.commit()

    # statistics
    def stats(self, days: int = 30, today: datetime | None = None) -> dict[str, Any]:
        today = today or datetime.now()
        days = max(1, min(3650, days))
        start_day = (today - timedelta(days=days - 1)).date().isoformat()
        end_day = today.date().isoformat()
        window = (start_day, end_day + "T23:59:59")
        totals = self._rows(
            "SELECT COUNT(*) AS runs, COALESCE(SUM(state IN ('ok')),0) AS ok_runs, COALESCE(SUM(state IN ('failed','interrupted')),0) AS failed_runs, "
            "COALESCE(SUM(state='partial'),0) AS partial_runs, COALESCE(SUM(state='missed'),0) AS missed_runs, COALESCE(SUM(bytes),0) AS bytes, "
            "COALESCE(SUM(files),0) AS files, COALESCE(SUM(deleted),0) AS deleted, COALESCE(SUM(errors),0) AS errors "
            "FROM runs WHERE started >= ? AND started <= ? AND state NOT IN ('running','skipped')", window)[0]
        speed = self._rows(
            "SELECT COALESCE(SUM(f.size),0) AS bytes, COALESCE(SUM(f.duration),0) AS seconds FROM files f JOIN runs r ON r.id=f.run_id "
            "WHERE f.status='ok' AND f.duration>0 AND r.started >= ? AND r.started <= ?", window)[0]
        totals["avg_speed"] = speed["bytes"] / speed["seconds"] if speed["seconds"] > 0 else 0.0
        done = totals["ok_runs"] + totals["failed_runs"] + totals["partial_runs"]
        totals["success_rate"] = round(100.0 * totals["ok_runs"] / done, 1) if done else None
        by_day = {row["day"]: row for row in self._rows(
            "SELECT substr(started,1,10) AS day, COUNT(*) AS runs, COALESCE(SUM(bytes),0) AS bytes, COALESCE(SUM(files),0) AS files, "
            "COALESCE(SUM(state IN ('failed','interrupted')),0) AS failed, COALESCE(SUM(state='missed'),0) AS missed FROM runs "
            "WHERE started >= ? AND started <= ? AND state NOT IN ('running','skipped') GROUP BY day", window)}
        speed_by_day = {row["day"]: row for row in self._rows(
            "SELECT substr(r.started,1,10) AS day, SUM(f.size) AS bytes, SUM(f.duration) AS seconds FROM files f JOIN runs r ON r.id=f.run_id "
            "WHERE f.status='ok' AND f.duration>0 AND r.started >= ? AND r.started <= ? GROUP BY day", window)}
        daily = []
        for offset in range(days):
            day = (today - timedelta(days=days - 1 - offset)).date().isoformat()
            row, spd = by_day.get(day), speed_by_day.get(day)
            daily.append({"date": day, "runs": row["runs"] if row else 0, "bytes": row["bytes"] if row else 0, "files": row["files"] if row else 0,
                          "failed": row["failed"] if row else 0, "missed": row["missed"] if row else 0,
                          "speed": spd["bytes"] / spd["seconds"] if spd and spd["seconds"] else 0.0})
        top_files = self._rows(
            "SELECT f.name, f.size, f.duration, f.speed, r.started FROM files f JOIN runs r ON r.id=f.run_id WHERE f.status='ok' AND r.started >= ? AND r.started <= ? "
            "ORDER BY f.size DESC LIMIT 10", window)
        by_job = self._rows(
            "SELECT job_name, COUNT(*) AS runs, COALESCE(SUM(bytes),0) AS bytes, COALESCE(SUM(files),0) AS files FROM runs "
            "WHERE started >= ? AND started <= ? AND state NOT IN ('running','skipped','missed') GROUP BY job_id, job_name ORDER BY bytes DESC", window)
        return {"days": days, "totals": totals, "daily": daily, "top_files": top_files, "by_job": by_job}
