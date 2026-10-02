"""The NextPull engine: a background thread that watches the clock, decides which jobs are due, queues them and runs them one
at a time through :class:`rclone_runner.RcloneRun`.

Rules (see docs/SCHEDULER.md):

* Every tick (default 10 s) the engine looks at the slots that passed since the last tick. A slot at most two ticks old runs
  normally; an older one runs as a *catch-up* if the job allows it and the delay is within its limit (the PC was asleep, the
  app was closed); otherwise it is recorded as *missed* so the History page shows the hole.
* Only one run at a time: two simultaneous downloads would just fight over the line. Others wait in a queue.
* A run that is already queued or running for the same job is never queued twice (recorded as *skipped*).
* ``stop_by`` ends a scheduled run at that time; a manual "Run now" ignores it.
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import applog
from rclone_runner import RcloneRun
from scheduler import classify_slot, due_slots, next_run, window_end
from store import ConfigStore, HistoryDb, SecretStore, now_iso

RunFactory = Callable[..., RcloneRun]
_log = applog.get_logger("engine")


class Engine:
    def __init__(self, config: ConfigStore, history: HistoryDb, secrets: SecretStore, *, logs_dir: Path | None = None, config_path: Path | None = None,
                 rclone_finder: Callable[[], list[str] | None], clock: Callable[[], datetime] = datetime.now, tick_seconds: float = 10.0,
                 run_factory: RunFactory = RcloneRun, notify: Callable[[str, str], None] | None = None) -> None:
        self.config, self.history, self.secrets = config, history, secrets
        self.logs_dir = logs_dir or Path(tempfile.mkdtemp(prefix="nextpull-logs-"))
        self.config_path = config_path or self.logs_dir / "rclone.empty.conf"
        self.rclone_finder, self.clock, self.tick_seconds, self.run_factory, self.notify = rclone_finder, clock, tick_seconds, run_factory, notify
        self._lock = threading.RLock()
        self._queue: list[dict[str, Any]] = []
        self._current: RcloneRun | None = None
        self._current_item: dict[str, Any] | None = None
        self._worker: threading.Thread | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_prune = ""
        self._events: list[dict[str, str]] = []
        self.last_tick: float | None = None   # time.monotonic() of the last finished tick; Diagnostics checks the scheduler is alive

    # lifecycle
    def start(self) -> None:
        self.history.recover_interrupted()
        if not self.config.state("last_checked"):
            self.config.set_state("last_checked", self.clock().isoformat(timespec="seconds"))
        self._stop.clear()
        _log.info("engine started (tick %ss)", self.tick_seconds)
        self._thread = threading.Thread(target=self._loop, name="nextpull-engine", daemon=True)
        self._thread.start()

    def stop(self, cancel_current: bool = True) -> None:
        self._stop.set()
        if cancel_current:
            with self._lock:
                run = self._current
                self._queue.clear()
            if run:
                run.cancel()
        for thread in (self._thread, self._worker):
            if thread and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=15)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as error:   # the engine must outlive any single bad tick
                _log.exception("scheduler tick failed")
                self._event("error", f"Scheduler error: {error}")
            self._stop.wait(self.tick_seconds)

    # scheduling
    def tick(self, now: datetime | None = None) -> None:
        now = now or self.clock()
        marker = self.config.state("last_checked")
        try:
            last = datetime.fromisoformat(marker) if marker else now
        except ValueError:
            last = now
        if last > now:   # the clock went backwards (manual change, DST): never replay the future
            last = now
        for job in self.config.jobs():
            if not job["enabled"]:
                continue
            slots = due_slots(job["schedule"], last, now)
            if not slots:
                continue
            if len(slots) > 1 or (now - last).total_seconds() > 3 * self.tick_seconds:
                _log.warning("%s: tick gap of %ds (app asleep or closed?), %d slot(s) due", job["name"], (now - last).total_seconds(), len(slots))
            for old in slots[:-1]:
                self.history.record_skipped(job, old, "missed", "Missed: the app was not running at that time")
            slot = slots[-1]
            decision = classify_slot(slot, now, tick_seconds=int(self.tick_seconds), catch_up=job["catch_up"], catch_up_hours=job["catch_up_hours"], window=window_end(job["schedule"], slot))
            if decision == "missed":
                self.history.record_skipped(job, slot, "missed", "Missed: the app was not running (catch-up is off or too late)")
                self._event("warn", f"{job['name']}: scheduled run at {slot:%H:%M} was missed")
            elif self._enqueue(job, "schedule" if decision == "run" else "catch-up", slot):
                _log.info("%s: queued (%s) for slot %s", job["name"], decision, slot.isoformat(timespec="minutes"))
            else:
                _log.warning("%s: slot %s skipped, previous run still going", job["name"], slot.isoformat(timespec="minutes"))
                self.history.record_skipped(job, slot, "skipped", "Skipped: the previous run of this job was still going")
        self.config.set_state("last_checked", now.isoformat(timespec="seconds"))
        self._housekeeping(now)
        self._dispatch()
        self.last_tick = time.monotonic()

    def _enqueue(self, job: dict[str, Any], trigger: str, slot: datetime, dry_run: bool | None = None) -> bool:
        with self._lock:
            if self._current_item and self._current_item["job_id"] == job["id"]:
                return False
            if any(item["job_id"] == job["id"] for item in self._queue):
                return False
            self._queue.append({"job_id": job["id"], "job_name": job["name"], "trigger": trigger, "slot": slot, "dry_run": dry_run})
            return True

    def run_now(self, job_id: str, dry_run: bool | None = None) -> dict[str, Any]:
        job = self.config.job(job_id)
        if not job:
            return {"ok": False, "error": "That job no longer exists"}
        if not self._enqueue(job, "manual", self.clock(), dry_run):
            return {"ok": False, "error": "This job is already running or waiting"}
        queued_behind = self._current_item["job_name"] if self._current_item else ""
        self._dispatch()
        return {"ok": True, "queued_behind": queued_behind}

    def _dispatch(self) -> None:
        with self._lock:
            if self._current_item or not self._queue or self._stop.is_set():
                return
            item = self._queue.pop(0)
            self._current_item = item
            self._worker = threading.Thread(target=self._execute, args=(item,), name="nextpull-run", daemon=True)
            self._worker.start()

    # execution
    def _execute(self, item: dict[str, Any]) -> None:
        job = self.config.job(item["job_id"])
        result: dict[str, Any] = {"state": "failed", "message": "", "files": 0, "bytes": 0}
        try:
            if not job:
                return
            if item.get("dry_run") is not None:
                job["dry_run"] = bool(item["dry_run"])
            creds, rclone = self.secrets.load(), self.rclone_finder()
            log_dir = self.logs_dir
            log_dir.mkdir(parents=True, exist_ok=True)
            run_id = self.history.start_run(job, item["trigger"])
            log_path = log_dir / f"run-{run_id}.jsonl"
            self.history.set_log_file(run_id, str(log_path))
            if not creds:
                _log.error("run %s cannot start: not connected to Nextcloud", run_id)
                self.history.finish_run(run_id, "failed", "Not connected to Nextcloud. Open Settings and connect.")
                result["message"] = "Not connected to Nextcloud"
                return
            if not rclone:
                _log.error("run %s cannot start: rclone not found", run_id)
                self.history.finish_run(run_id, "failed", "rclone was not found. Put rclone.exe in the tools folder or set its path in Settings.")
                result["message"] = "rclone was not found"
                return
            deadline = window_end(job["schedule"], item["slot"]) if item["trigger"] != "manual" else None
            run = self.run_factory(rclone=rclone, job=job, creds=creds, run_id=run_id, history=self.history, log_path=log_path, config_path=self.config_path,
                                   deadline=deadline, keep_awake=self.config.settings()["prevent_sleep"], clock=self.clock)
            with self._lock:
                self._current = run
            self._event("info", f"{job['name']}: started ({item['trigger']})")
            try:
                result = run.run()
            except Exception as error:   # a bug must not leave the run 'running' forever
                _log.exception("run %s crashed inside NextPull", run_id)
                self.history.finish_run(run_id, "failed", f"Internal error: {error}")
                result = {"state": "failed", "message": f"Internal error: {error}", "files": 0, "bytes": 0}
            self._event("info" if result["state"] in ("ok", "stopped", "cancelled") else "warn", f"{job['name']}: {result['state']} - {result['message']}")
        finally:
            with self._lock:
                self._current, self._current_item = None, None
            if job and self.notify and self.config.settings()["notifications"] and (result["state"] in ("failed", "partial") or (result["state"] in ("ok", "stopped") and result.get("files"))):
                try:
                    self.notify(f"NextPull: {job['name']}", f"{result['state'].capitalize()}: {result['message']}")
                except Exception:
                    _log.warning("could not show the desktop notification", exc_info=True)
            self._dispatch()

    def cancel(self, job_id: str | None = None) -> bool:
        with self._lock:
            run, item = self._current, self._current_item
            before = len(self._queue)
            self._queue = [entry for entry in self._queue if job_id and entry["job_id"] != job_id] if job_id else []
            removed = before != len(self._queue)
        if run and (job_id is None or (item and item["job_id"] == job_id)):
            run.cancel()
            return True
        return removed

    def set_bandwidth(self, rate: str) -> bool:
        with self._lock:
            run = self._current
        if not run:
            return False
        run.set_bandwidth(rate)
        return True

    # information for the UI
    def _event(self, level: str, text: str) -> None:
        _log.log({"error": 40, "warn": 30}.get(level, 20), "%s", text)
        with self._lock:
            self._events.append({"time": now_iso(), "level": level, "text": text})
            del self._events[:-80]

    def events(self) -> list[dict[str, str]]:
        with self._lock:
            return list(reversed(self._events))

    def is_busy(self) -> bool:
        with self._lock:
            return self._current_item is not None or bool(self._queue)

    def status(self) -> dict[str, Any]:
        now = self.clock()
        with self._lock:
            run, item = self._current, self._current_item
            queue = [{"job_id": entry["job_id"], "job_name": entry["job_name"], "trigger": entry["trigger"]} for entry in self._queue]
        jobs = []
        for job in self.config.jobs():
            upcoming = next_run(job["schedule"], now) if job["enabled"] else None
            last = self.history.last_run(job["id"])
            jobs.append({"id": job["id"], "name": job["name"], "enabled": job["enabled"], "next_run": upcoming.isoformat(timespec="seconds") if upcoming else None,
                         "last_run": {key: last[key] for key in ("id", "state", "started", "finished", "files", "bytes", "message")} if last else None,
                         "running": bool(item and item["job_id"] == job["id"])})
        return {"running": run is not None, "current": run.snapshot() if run else None, "starting": bool(item and not run), "queue": queue, "jobs": jobs,
                "last_checked": self.config.state("last_checked"), "now": now.isoformat(timespec="seconds")}

    def read_log(self, run_id: int, tail: int = 400) -> list[dict[str, Any]]:
        run = self.history.get_run(run_id)
        path = Path(run["log_file"]) if run and run.get("log_file") else None
        if not path or not path.is_file():
            return []
        lines: list[dict[str, Any]] = []
        try:
            raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        for line in raw[-max(1, min(5000, tail)):]:
            try:
                data = json.loads(line)
            except ValueError:
                data = {"msg": line, "level": "info"}
            lines.append({"time": str(data.get("time", ""))[:19].replace("T", " "), "level": str(data.get("level", "info")), "msg": str(data.get("msg", "")), "object": str(data.get("object", ""))})
        return lines

    def _housekeeping(self, now: datetime) -> None:
        """Once a day: drop old history rows and old log files."""
        today = now.date().isoformat()
        if self._last_prune == today:
            return
        self._last_prune = today
        settings = self.config.settings()
        self.history.prune(settings["history_days"])
        cutoff = time.time() - settings["log_days"] * 86400
        try:
            for path in self.logs_dir.glob("run-*.jsonl"):
                if path.stat().st_mtime < cutoff:
                    path.unlink()
        except OSError:
            pass
