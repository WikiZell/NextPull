"""Diagnostics: turn the history and logs into "what is wrong and what to do", run a self-test, and package a report to send.

Pure helpers where possible (``hint_for``, ``group_problems``, ``Scrubber``); ``build_report`` writes a zip. Everything that leaves
this module passes :func:`applog.redact`; with ``include_names=False`` (the default) file/folder names, the server host and the
user name are replaced too, so the report can be sent without exposing what is being downloaded.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import platform
import re
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

import applog

BAD_STATES = ("failed", "partial", "interrupted")
HINTS: list[tuple[str, str]] = [
    (r"not connected|sign.?in|unauthori[sz]ed|\b401\b|app password|revoked", "Reconnect to Nextcloud: Settings > Disconnect, then Connect again."),
    (r"rclone was not found|cannot start rclone|rclone.*not found", "Put rclone.exe in the tools\\rclone folder next to NextPull, or set its path in Settings."),
    (r"\b403\b|forbidden|permission denied|access is denied|not deleting", "Nextcloud or Windows refused the action. Check that the account may delete in that folder and that the destination folder is writable."),
    (r"no space|disk full|not enough space|\b112\b", "The destination disk is full. Free some space or choose another destination."),
    (r"timed? ?out|timeout|connection (reset|refused|aborted)|no such host|dial tcp|network|unreachable|offline|name resolution", "Nextcloud could not be reached. Check the internet connection and that the server is up; NextPull retries on the next schedule."),
    (r"\b50[0-9]\b|server error|bad gateway|service unavailable|maintenance", "The Nextcloud server had a problem (or is in maintenance). Try again later."),
    (r"name clash|clashes with|invalid (file )?name|illegal", "Some names cannot exist on Windows or clash on this disk; they are skipped, nothing is overwritten. Rename them on Nextcloud."),
    (r"destination folder|cannot use the destination", "The destination folder cannot be used. Check that the path exists, the drive is connected and you may write there."),
    (r"not found|404|does not exist|directory not found", "The Nextcloud folder of this job no longer exists. Edit the job and pick the folder again."),
    (r"missed|was not running", "The PC was off or NextPull was closed at that time. Turn on 'catch up' in the job, or enable Start with Windows."),
    (r"interrupted|crash|unexpected|internal error", "NextPull stopped in the middle of this run (PC shut down, or a bug). Use 'Send report' if it keeps happening."),
]


def hint_for(message: str) -> str:
    text = (message or "").lower()
    for pattern, hint in HINTS:
        if re.search(pattern, text):
            return hint
    return "Open the run in History to read its log. If it is unclear, press 'Send report' and send the file."


def _norm(message: str) -> str:
    return re.sub(r"\d+", "#", message or "")[:140]


def group_problems(runs: Iterable[dict[str, Any]], since_iso: str = "") -> list[dict[str, Any]]:
    """Failed/partial/interrupted/missed runs grouped by job + reason, newest first, each with a plain-language hint."""
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for run in runs:
        state = run.get("state", "")
        if state not in (*BAD_STATES, "missed"):
            continue
        when = run.get("finished") or run.get("started") or ""
        if since_iso and when < since_iso:
            continue
        key = (run.get("job_name", ""), state, _norm(run.get("message", "")))
        group = groups.get(key)
        if group is None:
            groups[key] = {"job": key[0], "state": state, "message": run.get("message", ""), "count": 1, "last": when, "run_id": run.get("id"),
                           "hint": hint_for(run.get("message", "")), "severity": "warn" if state in ("partial", "missed") else "error"}
        else:
            group["count"] += 1
            if when > group["last"]:
                group.update(last=when, run_id=run.get("id"), message=run.get("message", ""))
    return sorted(groups.values(), key=lambda item: item["last"], reverse=True)


def unseen_problem_count(runs: Iterable[dict[str, Any]], seen_iso: str) -> int:
    return sum(1 for run in runs if run.get("state") in BAD_STATES and (run.get("finished") or run.get("started") or "") > seen_iso)


def app_log_problems(entries: Iterable[dict[str, str]], limit: int = 40) -> list[dict[str, Any]]:
    """WARNING+ lines of the application log, repeated messages folded into one row with a count."""
    rows: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if applog.LEVELS.get(entry["level"], 20) < 30:
            continue
        key = _norm(entry["message"].splitlines()[0] if entry["message"] else "")
        row = rows.get(key)
        if row is None:
            rows[key] = {"level": entry["level"], "source": entry["name"], "message": entry["message"], "count": 1, "last": entry["time"], "hint": hint_for(entry["message"])}
        else:
            row["count"] += 1
            row["last"] = entry["time"]
    return sorted(rows.values(), key=lambda item: item["last"], reverse=True)[:limit]


def system_info(version: str) -> dict[str, Any]:
    return {"app_version": version, "python": sys.version.split()[0], "frozen": bool(getattr(sys, "frozen", False)), "platform": platform.platform(),
            "machine": platform.machine(), "time": datetime.now().astimezone().isoformat(timespec="seconds")}


# ------------------------------------------------------------------------------------------------------- self-test

def check(check_id: str, title: str, status: str, detail: str = "", hint: str = "") -> dict[str, str]:
    return {"id": check_id, "title": title, "status": status, "detail": detail, "hint": hint}


def run_self_test(*, rclone: Callable[[], dict[str, Any]], session: Callable[[], Any] | None, jobs: list[dict[str, Any]], engine_last_tick: float | None,
                  tick_seconds: float, data_dir: Path | None, free_bytes: Callable[[Path], int | None], autostart: bool, watchdog: bool,
                  dest_free_warn: int = 2 * 1024 ** 3) -> list[dict[str, str]]:
    """Every check returns a result instead of raising, so one broken part never hides the others."""
    results: list[dict[str, str]] = []

    def guarded(check_id: str, title: str, body: Callable[[], list[dict[str, str]] | dict[str, str]]) -> None:
        try:
            out = body()
            results.extend(out if isinstance(out, list) else [out])
        except Exception as error:   # noqa: BLE001 - a check must never crash the page
            applog.get_logger("app").warning("self-test '%s' crashed: %s", check_id, error, exc_info=True)
            results.append(check(check_id, title, "error", f"The check itself failed: {type(error).__name__}: {error}"))

    def rclone_check() -> dict[str, str]:
        info = rclone()
        if info.get("found"):
            return check("rclone", "rclone", "ok", f"{info.get('version', '')} at {info.get('path', '')}")
        return check("rclone", "rclone", "error", info.get("message", "not found"), hint_for("rclone was not found"))

    def server_check() -> dict[str, str]:
        if session is None:
            return check("nextcloud", "Nextcloud connection", "error", "Not connected", hint_for("not connected"))
        started = time.monotonic()
        try:
            user = session().user_info()
        except Exception as error:   # noqa: BLE001 - NextcloudError and anything below it
            text = str(error)
            return check("nextcloud", "Nextcloud connection", "error", text, hint_for(text))
        return check("nextcloud", "Nextcloud connection", "ok", f"Signed in as {user.get('display_name') or user.get('id', '?')} ({time.monotonic() - started:.1f}s)")

    def job_checks() -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for job in jobs:
            title = f"Job '{job['name']}'"
            if not job.get("enabled"):
                out.append(check(f"job:{job['id']}", title, "skip", "Disabled"))
                continue
            problems: list[str] = []
            if session is not None:
                try:
                    if not session().check_folder(job["source"]):
                        problems.append(f"{job['source']} on Nextcloud is a file, not a folder")
                except Exception as error:   # noqa: BLE001
                    problems.append(f"Nextcloud folder {job['source']}: {error}")
            dest = Path(job["dest"])
            if not dest.is_dir():
                parent = next((p for p in dest.parents if p.is_dir()), None)
                if parent is None:
                    problems.append(f"Destination drive/folder {job['dest']} is not available")
            else:
                free = free_bytes(dest)
                if free is not None and free < dest_free_warn:
                    out.append(check(f"job:{job['id']}:space", f"{title}: disk space", "warn", f"Only {free / 1024 ** 3:.1f} GB free on the destination", hint_for("no space")))
            out.append(check(f"job:{job['id']}", title, "error" if problems else "ok", "; ".join(problems) or f"{job['source']} -> {job['dest']}",
                             "Edit the job and check the folders." if problems else ""))
        if not jobs:
            out.append(check("jobs", "Jobs", "warn", "No jobs yet", "Create a job on the Jobs page."))
        return out

    def scheduler_check() -> dict[str, str]:
        if engine_last_tick is None:
            return check("scheduler", "Scheduler", "error", "The scheduler has not completed a single check", "Restart NextPull. If it persists, send a report.")
        age = time.monotonic() - engine_last_tick
        if age > max(60.0, tick_seconds * 6):
            return check("scheduler", "Scheduler", "error", f"The scheduler has been silent for {age:.0f}s", "Restart NextPull. If it persists, send a report.")
        return check("scheduler", "Scheduler", "ok", f"Last check {age:.0f}s ago")

    def data_check() -> list[dict[str, str]]:
        if not data_dir:
            return [check("data", "Data folder", "skip", "In-memory (test) mode")]
        out = []
        probe = data_dir / ".write-test"
        try:
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            out.append(check("data", "Data folder", "ok", str(data_dir)))
        except OSError as error:
            out.append(check("data", "Data folder", "error", f"Cannot write to {data_dir}: {error}", "NextPull cannot save history or logs. Check the folder permissions."))
        free = free_bytes(data_dir)
        if free is not None and free < 500 * 1024 ** 2:
            out.append(check("data:space", "Data folder disk space", "warn", f"{free / 1024 ** 2:.0f} MB free", hint_for("no space")))
        return out

    def startup_check() -> list[dict[str, str]]:
        out = []
        if not autostart:
            out.append(check("autostart", "Start with Windows", "warn", "Off", "After a restart NextPull will not run until someone opens it. Turn it on in Settings."))
        else:
            out.append(check("autostart", "Start with Windows", "ok", "On"))
        out.append(check("watchdog", "Watchdog", "ok" if watchdog else "skip", "On" if watchdog else "Off (optional): restarts NextPull if it is closed"))
        return out

    guarded("rclone", "rclone", rclone_check)
    guarded("nextcloud", "Nextcloud connection", server_check)
    guarded("jobs", "Jobs", job_checks)
    guarded("scheduler", "Scheduler", scheduler_check)
    guarded("data", "Data folder", data_check)
    guarded("startup", "Startup", startup_check)
    return results


# ------------------------------------------------------------------------------------------------------- the report

class Scrubber:
    """Replaces names with stable placeholders (same name -> same placeholder, so the report is still readable) and applies redact()."""

    def __init__(self, include_names: bool, literals: Iterable[str] = ()) -> None:
        self.include_names = include_names
        self._map: dict[str, str] = {}
        self._literals = sorted({item for item in literals if item and len(item) >= 3}, key=len, reverse=True)

    def alias(self, value: str, kind: str = "name") -> str:
        if self.include_names or not value:
            return value
        return self._map.setdefault(value, f"<{kind}-{hashlib.sha1(value.encode('utf-8')).hexdigest()[:6]}>")

    def text(self, value: Any) -> str:
        out = applog.redact(value)
        if not self.include_names:
            for literal in self._literals:
                out = out.replace(literal, self.alias(literal, "path"))
        return out

    def log_line(self, raw: str) -> str:
        """A run-log JSON line: hide the ``object`` (file name) and any known literal inside ``msg``."""
        try:
            data = json.loads(raw)
        except ValueError:
            return self.text(raw)
        if not isinstance(data, dict):
            return self.text(raw)
        obj = str(data.get("object", ""))
        if obj and not self.include_names:
            data["object"] = self.alias(obj)
            data["msg"] = str(data.get("msg", "")).replace(obj, data["object"])
        data["msg"] = self.text(data.get("msg", ""))
        for key in ("object", "error"):
            if key in data:
                data[key] = self.text(data[key])
        return json.dumps(data, ensure_ascii=False)


README = """NextPull problem report
=======================

Send this file to the person who helps you with NextPull (it is a normal .zip).

Contents
  summary.json    app and Windows version, settings (never passwords), jobs
  problems.json   what NextPull thinks is wrong, in plain language
  self-test.json  result of the checkup (only if it was run)
  runs.csv        the most recent runs
  app.log         what the application did (secrets hidden)
  run-logs/       the logs of the latest failed or partial runs

Privacy
  Passwords and tokens are never included. {names}
  Open the zip and look before you send it if you like.
"""


def build_report(*, version: str, data_dir: Path | None, out_dir: Path, settings: dict[str, Any], jobs: list[dict[str, Any]], runs: list[dict[str, Any]],
                 files_for: Callable[[int], list[dict[str, Any]]], problems: list[dict[str, Any]], self_test: list[dict[str, str]] | None,
                 connection: dict[str, Any], include_names: bool = False, note: str = "") -> Path:
    """Write ``nextpull-report-<time>.zip`` into ``out_dir`` and return its path. Keeps only the 5 newest reports."""
    literals = [connection.get("server", ""), connection.get("user", ""), connection.get("display_name", "")]
    for job in jobs:
        literals += [job.get("source", ""), job.get("dest", "")]
    for run in runs:
        literals += [run.get("source", ""), run.get("dest", "")]
    scrub = Scrubber(include_names, literals)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"nextpull-report-{datetime.now():%Y%m%d-%H%M%S}.zip"
    summary = {"system": system_info(version), "connected": bool(connection.get("connected")), "server": scrub.alias(connection.get("server", ""), "server"),
               "settings": settings, "jobs": [{**job, "source": scrub.alias(job.get("source", ""), "path"), "dest": scrub.alias(job.get("dest", ""), "path"),
                                               "name": scrub.alias(job.get("name", ""), "job")} for job in jobs],
               "names_included": include_names, "user_note": scrub.text(note)}

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.txt", README.format(names="File and folder names, the server and the user name are replaced by codes." if not include_names
                                                      else "File and folder names ARE included."))
        archive.writestr("summary.json", json.dumps(summary, indent=2, ensure_ascii=False))
        archive.writestr("problems.json", json.dumps([{**item, "job": scrub.alias(item.get("job", ""), "job"), "message": scrub.text(item.get("message", ""))} for item in problems], indent=2, ensure_ascii=False))
        if self_test is not None:
            archive.writestr("self-test.json", json.dumps([{**item, "detail": scrub.text(item.get("detail", ""))} for item in self_test], indent=2, ensure_ascii=False))
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["id", "job", "trigger", "mode", "dry_run", "started", "finished", "state", "files", "bytes", "deleted", "errors", "message"])
        for run in runs:
            writer.writerow([run.get("id"), scrub.alias(run.get("job_name", ""), "job"), run.get("trigger"), run.get("mode"), run.get("dry_run"), run.get("started"), run.get("finished"),
                             run.get("state"), run.get("files"), run.get("bytes"), run.get("deleted"), run.get("errors"), scrub.text(run.get("message", ""))])
        archive.writestr("runs.csv", buffer.getvalue())
        if data_dir:
            for name in ("app.log.5", "app.log.4", "app.log.3", "app.log.2", "app.log.1", "app.log"):
                source = data_dir / name
                if source.is_file():
                    lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
                    archive.writestr(name, "\n".join(scrub.text(line) for line in lines) + "\n")
        else:
            handler = applog.ring()
            if handler:
                archive.writestr("app.log", "\n".join(scrub.text(f"{e['time']} | {e['level']} | {e['name']} | {e['message']}") for e in handler.buffer) + "\n")
        bad = [run for run in runs if run.get("state") in BAD_STATES][:10]
        for run in bad:
            log_file = run.get("log_file")
            if log_file and Path(log_file).is_file():
                lines = Path(log_file).read_text(encoding="utf-8", errors="replace").splitlines()[-1500:]
                archive.writestr(f"run-logs/run-{run['id']}.jsonl", "\n".join(scrub.log_line(line) for line in lines) + "\n")
            errors = [f for f in files_for(int(run["id"])) if f.get("status") in ("error", "skipped") or f.get("error")]
            if errors:
                archive.writestr(f"run-logs/run-{run['id']}-files.json", json.dumps([{"name": scrub.alias(f.get("name", "")), "status": f.get("status"), "size": f.get("size"),
                                                                                       "error": scrub.text(f.get("error", ""))} for f in errors[:300]], indent=2, ensure_ascii=False))
    reports = sorted(out_dir.glob("nextpull-report-*.zip"))
    for old in reports[:-5]:
        try:
            old.unlink()
        except OSError:
            pass
    return path
