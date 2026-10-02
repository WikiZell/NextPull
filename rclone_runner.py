"""rclone integration for NextPull.

One job run is one ``rclone copy`` / ``rclone move`` process:

* The Nextcloud remote is defined **through the environment** (``RCLONE_CONFIG_NC_*``) so no config file with a password
  ever exists, and the password is rclone-obscured through stdin, never on a command line.
* Progress comes from rclone's **remote-control (rc) API** on a random localhost port with a random user/password
  (``core/stats``); the speed limit can be changed live (``core/bwlimit``) and a run is stopped with ``core/quit``.
* What happened to each file comes from rclone's **JSON log** (``--use-json-log --log-file``), the source of truth for
  "copied", "deleted" and per-file errors. The raw log is kept per run for the History page.
* ``move`` is rclone's own: a source file is deleted only after rclone copied and verified it.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from base64 import b64encode
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

import applog
from nc_client import NextcloudError, NextcloudSession, dav_files_url
from rclone_logic import (LOW_LEVEL_RETRIES, classify_event, exit_message, find_collisions, glob_escape, humanize_error, local_relpath)  # noqa: F401  (re-exported)
from store import HistoryDb, now_iso
from winutil import IS_WINDOWS, CREATE_NO_WINDOW, prevent_sleep

_log = applog.get_logger("run")

COLLISION_GUARD = IS_WINDOWS   # case-insensitive destination: remote names that differ only by case/characters would overwrite each other
REMOTE = "NC"
RC_USER = "nextpull"
STATES_WITH_FILES_OK = ("ok", "partial")


class RcloneError(Exception):
    """A problem with rclone itself (missing, cannot start, bad answer). The message is fit for the UI."""


# ---------------------------------------------------------------------------------------------------- discovery

def find_rclone(configured: str = "", extra_dirs: Iterable[Path] = ()) -> list[str] | None:
    """Command prefix that runs rclone, or None. Order: the path configured in Settings, ``tools/rclone/rclone.exe`` next to
    the program, then PATH. A list so tests can use ``[python, fake_rclone.py]`` as "rclone"."""
    if configured:
        path = Path(configured)
        return [str(path)] if path.is_file() else None
    name = "rclone.exe" if IS_WINDOWS else "rclone"
    for base in extra_dirs:
        for candidate in (Path(base) / "tools" / "rclone" / name, Path(base) / name):
            if candidate.is_file():
                return [str(candidate)]
    found = shutil.which("rclone")
    return [found] if found else None


def _run(command: list[str], *, stdin: str | None = None, timeout: float = 20.0, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(command, input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env,
                          creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0)


def rclone_version(rclone: list[str]) -> str:
    try:
        result = _run([*rclone, "version"], timeout=15)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RcloneError(f"Cannot run rclone: {error}") from error
    first = (result.stdout or "").splitlines()[0].strip() if result.stdout else ""
    if result.returncode != 0 or not first.lower().startswith("rclone"):
        raise RcloneError("That file does not look like rclone")
    return first


def obscure_password(rclone: list[str], password: str) -> str:
    """``rclone obscure -`` with the password on stdin (never on the command line)."""
    try:
        result = _run([*rclone, "obscure", "-"], stdin=password, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RcloneError(f"Cannot run rclone: {error}") from error
    value = (result.stdout or "").strip()
    if result.returncode != 0 or not value:
        raise RcloneError("rclone could not prepare the login")
    return value


# ------------------------------------------------------------------------------------------ command construction

def bwlimit_value(speed: dict[str, Any]) -> str:
    """rclone ``--bwlimit`` value: a timetable if given, else a flat MB/s limit, else '' (unlimited)."""
    timetable = str(speed.get("timetable") or "").strip()
    if timetable:
        return timetable
    limit = float(speed.get("limit_mbps") or 0)
    if limit <= 0:
        return ""
    return f"{limit:g}M"


def filter_args(job: dict[str, Any], extra_excludes: list[str] | None = None) -> list[str]:
    """Filter flags as explicit, ordered ``--filter`` rules (first matching rule wins): guard excludes, the user's excludes, the includes,
    and a final ``- **`` when includes exist. Measured with the real rclone: plain ``--include`` flags are evaluated before ``--exclude``
    flags whatever their command-line order, so ``--include *.mkv --exclude Skip/**`` still copied ``Skip/d.mkv``."""
    args: list[str] = []
    if job["min_age_minutes"] > 0:
        args += ["--min-age", f"{job['min_age_minutes']}m"]
    for pattern in [*(extra_excludes or []), *job["excludes"]]:
        args += ["--filter", f"- {pattern}"]
    for pattern in job["includes"]:
        args += ["--filter", f"+ {pattern}"]
    if job["includes"]:
        args += ["--filter", "- **"]
    return args


def build_args(job: dict[str, Any], *, config_path: Path, rc_port: int, rc_pass: str, log_file: Path, extra_excludes: list[str] | None = None) -> list[str]:
    """Arguments after the rclone executable. No secrets here: the login is in the environment, the rc password is
    random and only valid for this one process on localhost."""
    args = [
        job["mode"], f"{REMOTE}:{job['source']}", job["dest"],
        "--config", str(config_path),
        "--transfers", str(job["transfers"]), "--checkers", "4",
        "--multi-thread-streams", str(job["streams"]),
        "--retries", str(job["retries"] + 1), "--low-level-retries", str(LOW_LEVEL_RETRIES),
        "--timeout", "60s", "--contimeout", "30s",
        "--use-json-log", "--log-level", "INFO", "--log-file", str(log_file), "--stats", "0",
        "--rc", "--rc-addr", f"127.0.0.1:{rc_port}", "--rc-user", RC_USER, "--rc-pass", rc_pass,
    ]
    args += filter_args(job, extra_excludes)
    limit = bwlimit_value(job["speed"])
    if limit:
        args += ["--bwlimit", limit]
    if job["mode"] == "move" and job["delete_empty_dirs"]:
        args.append("--delete-empty-src-dirs")
    if job["checksum"]:
        args.append("--checksum")
    if job["dry_run"]:
        args.append("--dry-run")
    return args


def build_env(creds: dict[str, str], obscured: str, base: dict[str, str] | None = None) -> dict[str, str]:
    """Process environment: the parent's, minus any RCLONE_* the user may have set, plus the NC remote definition."""
    env = {key: value for key, value in (base if base is not None else os.environ).items() if not key.upper().startswith("RCLONE_")}
    env.update({
        f"RCLONE_CONFIG_{REMOTE}_TYPE": "webdav",
        f"RCLONE_CONFIG_{REMOTE}_URL": dav_files_url(creds["server"], creds.get("uid") or creds["user"]),
        f"RCLONE_CONFIG_{REMOTE}_VENDOR": "nextcloud",
        f"RCLONE_CONFIG_{REMOTE}_USER": creds["user"],
        f"RCLONE_CONFIG_{REMOTE}_PASS": obscured,
    })
    return env


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ---------------------------------------------------------------------------------------------------- rc client

class RcClient:
    """Tiny client for rclone's remote-control HTTP API (localhost only)."""

    def __init__(self, port: int, password: str, user: str = RC_USER, timeout: float = 2.5) -> None:
        self.base = f"http://127.0.0.1:{port}/"
        self.auth = "Basic " + b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        self.timeout = timeout

    def call(self, command: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request = urllib.request.Request(self.base + command, data=json.dumps(params or {}).encode("utf-8"), method="POST",
                                         headers={"Content-Type": "application/json", "Authorization": self.auth})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise RcloneError(f"rclone control call failed: {error}") from error


# ---------------------------------------------------------------------------------------------------- log events

def parse_log_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        data = json.loads(line)
    except ValueError:
        return None
    return data if isinstance(data, dict) and "msg" in data else None


class LogTail:
    """Reads new complete lines of a growing rclone log file."""

    def __init__(self, path: Path) -> None:
        self.path, self._offset, self._partial = path, 0, b""

    def read_new(self) -> list[dict[str, Any]]:
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
        except OSError:
            return []
        self._offset += len(chunk)
        data = self._partial + chunk
        *lines, self._partial = data.split(b"\n")
        events = [event for event in (parse_log_line(line.decode("utf-8", "replace")) for line in lines) if event]
        return events


# ------------------------------------------------------------------------------------------------------ the run

class RcloneRun:
    """One blocking run. ``run()`` returns the result dict; ``cancel()`` and ``set_bandwidth()`` may be called from other
    threads while it runs; ``snapshot()`` is what the UI shows.

    Phases: destination check -> **server pre-check** (is it really Nextcloud WebDAV, does the folder exist) -> **name-clash guard**
    (leave alone files that would overwrite each other on a case-insensitive disk) -> rclone -> conclusion."""

    POLL = 0.5
    PRECHECK_ATTEMPTS = 3
    PRECHECK_DELAY = 5.0

    def __init__(self, *, rclone: list[str], job: dict[str, Any], creds: dict[str, str], run_id: int, history: HistoryDb, log_path: Path, config_path: Path,
                 deadline: datetime | None = None, keep_awake: bool = True, clock: Callable[[], datetime] = datetime.now,
                 on_change: Callable[[], None] | None = None, precheck: bool = True) -> None:
        self.rclone, self.job, self.creds, self.run_id, self.history = rclone, job, creds, run_id, history
        self.precheck = precheck   # False only in tests that use a fake rclone without a real server behind it
        self.log_path, self.config_path, self.deadline, self.keep_awake, self.clock = log_path, config_path, deadline, keep_awake, clock
        self.on_change = on_change
        self._cancel = threading.Event()
        self._rc: RcClient | None = None
        self._lock = threading.Lock()
        self._skipped: list[tuple[str, str]] = []
        self._snapshot: dict[str, Any] = {"run_id": run_id, "job_id": job["id"], "job_name": job["name"], "state": "starting", "mode": job["mode"], "dry_run": job["dry_run"],
                                          "started": now_iso(), "bytes": 0, "total_bytes": 0, "speed": 0.0, "eta": None, "files_done": 0, "transferring": [], "errors": 0, "last_error": "", "bwlimit": bwlimit_value(job["speed"]) or "off"}

    # control
    def cancel(self) -> None:
        self._cancel.set()

    def set_bandwidth(self, rate: str) -> None:
        if self._rc is None:
            raise RcloneError("The transfer is not running yet")
        try:
            self._rc.call("core/bwlimit", {"rate": rate or "off"})
        except RcloneError as error:
            raise RcloneError("The transfer is still starting. Try again in a moment.") from error
        self._update(bwlimit=rate or "off")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            copy = dict(self._snapshot)
            copy["transferring"] = [dict(item) for item in self._snapshot["transferring"]]
            return copy

    def _update(self, **fields: Any) -> None:
        with self._lock:
            self._snapshot.update(fields)
        if self.on_change:
            self.on_change()

    def _log_note(self, level: str, message: str, obj: str = "") -> None:
        """Append a line of our own to the run log (same JSON shape as rclone's) so History explains what NextPull itself decided."""
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            _log.log({"error": 40, "warning": 30}.get(level, 20), "run %s: %s", self.run_id, message)
            line = {"level": level, "msg": message, "time": datetime.now().astimezone().isoformat(timespec="seconds"), "source": "nextpull"}
            if obj:
                line["object"] = obj
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ---------------------------------------------------------------------------------------------------- phases
    def _precheck(self) -> dict[str, Any] | None:
        """Ask Nextcloud itself before starting rclone. Returns a failure result, or None when the folder is fine. Transient failures
        (network, 5xx) are retried a few times; a wrong login or a missing folder fail at once with a clear message. The request runs on
        a helper thread so **Cancel works even while the server hangs**."""
        if not self.precheck:
            return None
        session = NextcloudSession(self.creds["server"], self.creds["user"], self.creds["app_password"], timeout=30, uid=self.creds.get("uid"))
        last = ""
        for attempt in range(1, self.PRECHECK_ATTEMPTS + 1):
            box: dict[str, Any] = {}

            def ask() -> None:
                try:
                    box["is_dir"] = session.check_folder(self.job["source"])
                except NextcloudError as error:
                    box["error"] = error
                except Exception as error:   # never let a helper thread die silently
                    box["error"] = NextcloudError(f"Unexpected error while checking Nextcloud: {error}")

            worker = threading.Thread(target=ask, name="nextpull-precheck", daemon=True)
            worker.start()
            while worker.is_alive():
                if self._cancel.is_set():
                    return self._finish("cancelled", "Cancelled by you", 0)
                worker.join(timeout=0.2)
            if "is_dir" in box:
                self._source_is_dir = box["is_dir"]
                if session.uid:   # the WebDAV path needs the user id, which is not the login name when someone signed in with an e-mail address
                    self.creds = {**self.creds, "uid": session.uid}
                return None
            error = box["error"]
            last = str(error)
            if not getattr(error, "transient", False):
                break
            self._log_note("warning", f"Nextcloud pre-check attempt {attempt}/{self.PRECHECK_ATTEMPTS} failed: {last}")
            if attempt < self.PRECHECK_ATTEMPTS and self._cancel.wait(self.PRECHECK_DELAY):
                return self._finish("cancelled", "Cancelled by you", 0)
        self._log_note("error", last)
        return self._finish("failed", last, 1)

    def _run_capture(self, command: list[str], env: dict[str, str], timeout: float) -> subprocess.CompletedProcess:
        """subprocess.run that stays responsive to Cancel."""
        process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                   creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0)
        end = time.monotonic() + timeout
        box: dict[str, Any] = {}
        reader = threading.Thread(target=lambda: box.update(zip(("out", "err"), process.communicate())), daemon=True)
        reader.start()
        while reader.is_alive():
            if self._cancel.is_set() or time.monotonic() > end:
                process.kill()
                reader.join(timeout=5)
                raise RcloneError("Cancelled" if self._cancel.is_set() else "Timed out while listing the Nextcloud folder")
            reader.join(timeout=0.3)
        return subprocess.CompletedProcess(command, process.returncode, box.get("out", ""), box.get("err", ""))

    def _name_clash_guard(self, env: dict[str, str]) -> list[str] | None:
        """List the files this run would take and find names that collide on a case-insensitive disk (``Movie.mkv`` / ``movie.mkv``,
        ``a:b`` / ``a：b``). Returns ``--exclude`` patterns that leave ALL members of a group alone (nobody can know which one the user
        wants, and a *move* would delete both on Nextcloud while keeping one copy). None if the guard could not run safely."""
        if not COLLISION_GUARD or not getattr(self, "_source_is_dir", True):
            return []
        command = [*self.rclone, "lsf", f"{REMOTE}:{self.job['source']}", "-R", "--files-only", "--config", str(self.config_path), *filter_args(self.job),
                   "--timeout", "60s", "--contimeout", "30s", "--retries", "2", "--low-level-retries", str(LOW_LEVEL_RETRIES)]
        try:
            result = self._run_capture(command, env, timeout=180)
        except RcloneError as error:
            if self._cancel.is_set():
                raise
            self._log_note("warning", f"Name check could not run: {error}")
            return None
        if result.returncode != 0:
            self._log_note("warning", f"Name check could not run: {(result.stderr or '').strip()[-200:]}")
            return None
        groups = find_collisions(line.strip() for line in result.stdout.splitlines() if line.strip())
        patterns: list[str] = []
        for group in groups:
            for path in group:
                others = ", ".join(other for other in group if other != path)
                self._skipped.append((path, f"left untouched: the name clashes with {others} on a case-insensitive disk (one would overwrite the other)"))
                patterns.append(glob_escape(path))
                self._log_note("warning", f"Skipping {path}: its name clashes with {others} on this disk", path)
        return patterns

    # ---------------------------------------------------------------------------------------------------- the run
    def run(self) -> dict[str, Any]:
        job = self.job
        self._source_is_dir = True
        _log.info("run %s start: job=%r mode=%s dry_run=%s source=%s dest=%s", self.run_id, job["name"], job["mode"], job["dry_run"], job["source"], job["dest"])
        try:
            Path(job["dest"]).mkdir(parents=True, exist_ok=True)
        except OSError as error:
            return self._finish("failed", f"Cannot use the destination folder: {error}", 1)
        failure = self._precheck()
        if failure:
            return failure
        try:
            obscured = obscure_password(self.rclone, self.creds["app_password"])
        except RcloneError as error:
            return self._finish("failed", str(error), 1)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.touch(exist_ok=True)   # an empty config file keeps rclone from looking at the user's own rclone.conf
        env = build_env(self.creds, obscured)
        try:
            guard = self._name_clash_guard(env)
        except RcloneError:
            return self._finish("cancelled", "Cancelled by you", 0)
        if guard is None and job["mode"] == "move" and not job["dry_run"]:
            return self._finish("failed", "Could not check the file names before moving, so nothing was moved (see the log).", 1)
        for name, why in self._skipped:
            self.history.add_file(self.run_id, name, 0, "skipped", error=why)
        port, rc_pass = free_port(), secrets.token_urlsafe(18)
        applog.register_secret(rc_pass)
        self._rc = RcClient(port, rc_pass)
        args = build_args(job, config_path=self.config_path, rc_port=port, rc_pass=rc_pass, log_file=self.log_path, extra_excludes=guard or [])
        try:
            process = subprocess.Popen([*self.rclone, *args], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0)
        except OSError as error:
            return self._finish("failed", f"Cannot start rclone: {error}", 1)
        _log.info("run %s: rclone started (pid %s, rc port %s)", self.run_id, process.pid, port)
        tail_lines: list[str] = []
        drains = [threading.Thread(target=self._drain, args=(stream, tail_lines), daemon=True) for stream in (process.stdout, process.stderr)]
        for thread in drains:
            thread.start()
        if self.keep_awake:
            prevent_sleep(True)
        try:
            outcome = self._supervise(process)
        finally:
            if self.keep_awake:
                prevent_sleep(False)
        for thread in drains:
            thread.join(timeout=2)
        for stream in (process.stdout, process.stderr):   # no leaked pipe handles after months of nightly runs
            try:
                stream.close()
            except OSError:
                pass
        return self._conclude(process.returncode, outcome, tail_lines)

    @staticmethod
    def _drain(stream: Any, sink: list[str]) -> None:
        for line in stream:
            text = line.strip()
            if text:
                sink.append(text)
                del sink[:-40]

    def _supervise(self, process: subprocess.Popen) -> dict[str, Any]:
        tail = LogTail(self.log_path)
        started: dict[str, float] = {}
        sizes: dict[str, int] = {}
        file_errors: dict[str, str] = {}
        delete_errors: dict[str, str] = {}
        done: dict[str, dict[str, Any]] = {}
        present: dict[str, int] = {}          # files that were already downloaded earlier; the move just removed them from Nextcloud
        general: list[str] = []               # specific errors not tied to a file (retry noise excluded)
        outcome: dict[str, Any] = {"quit": "", "file_errors": file_errors, "delete_errors": delete_errors, "done": done, "present": present, "general": general}
        quit_sent_at: float | None = None

        def handle(events: list[dict[str, Any]]) -> None:
            for event in events:
                found = classify_event(event)
                if not found:
                    continue
                kind, obj, text = found
                if kind == "copied" and obj:
                    file_errors.pop(obj, None)
                    size = sizes.get(obj) or self._local_size(obj)
                    begin = started.pop(obj, None)
                    duration = time.monotonic() - begin if begin else 0.0
                    row = {"size": size, "duration": duration, "deleted": False, "finished": now_iso()}
                    done[obj] = row
                    self.history.add_file(self.run_id, obj, size, "ok", finished=row["finished"], duration=duration)
                elif kind == "deleted" and obj:
                    delete_errors.pop(obj, None)
                    if obj in done:
                        done[obj]["deleted"] = True
                        self.history.mark_file_deleted(self.run_id, obj)
                    elif obj not in present:   # copied in an earlier run; this run only removed it from Nextcloud
                        size = self._local_size(obj)
                        present[obj] = size
                        self.history.add_file(self.run_id, obj, size, "present", finished=now_iso(), deleted=True, error="already downloaded earlier; removed from Nextcloud now")
                elif kind == "dry":
                    match = re.search(r"\(size (\d+)\)", text)
                    size = int(match.group(1)) if match else 0
                    done[obj] = {"size": size, "duration": 0.0, "deleted": False, "finished": now_iso()}
                    self.history.add_file(self.run_id, obj, size, "dry", finished=now_iso(), error="dry-run: nothing was transferred")
                elif kind == "file_error":
                    file_errors[obj] = text
                elif kind == "delete_error":
                    delete_errors[obj] = text
                elif kind == "general" and not text.startswith("Attempt ") and text not in general:
                    general.append(text)
            if events:
                last = (next(iter(file_errors.values()), "") or next(iter(delete_errors.values()), "") or (general[-1] if general else ""))
                self._update(files_done=len(done) + len(present), errors=len(file_errors) + len(delete_errors), last_error=humanize_error(last) if last else "")

        while process.poll() is None:
            now = self.clock()
            if self._cancel.is_set() and not outcome["quit"]:
                outcome["quit"], quit_sent_at = "cancelled", time.monotonic()
                self._quit()
            elif self.deadline and now >= self.deadline and not outcome["quit"]:
                outcome["quit"], quit_sent_at = "stopped", time.monotonic()
                self._quit()
            if quit_sent_at and time.monotonic() - quit_sent_at > 10:
                process.kill()
            try:
                stats = self._rc.call("core/stats") if self._rc else {}
                transferring = [{"name": item.get("name", ""), "size": int(item.get("size") or 0), "bytes": int(item.get("bytes") or 0), "percentage": int(item.get("percentage") or 0),
                                 "speed": float(item.get("speed") or 0), "eta": item.get("eta")} for item in stats.get("transferring") or []]
                for item in transferring:
                    started.setdefault(item["name"], time.monotonic())
                    sizes[item["name"]] = item["size"] or sizes.get(item["name"], 0)
                self._update(state="stopping" if outcome["quit"] else "running", bytes=int(stats.get("bytes") or 0), total_bytes=int(stats.get("totalBytes") or 0), speed=float(stats.get("speed") or 0),
                             eta=stats.get("eta"), transferring=transferring)
                for item in (self._rc.call("core/transferred").get("transferred") or []):   # authoritative sizes, also for names rclone stores differently on Windows
                    if item.get("name") and not item.get("error") and item.get("size"):
                        sizes[item["name"]] = int(item["size"])
            except RcloneError:
                pass   # rc not up yet, or rclone is exiting
            handle(tail.read_new())
            time.sleep(self.POLL)
        handle(tail.read_new())
        return outcome

    def _quit(self) -> None:
        try:
            if self._rc:
                self._rc.call("core/quit", {"exitCode": 0})
        except RcloneError:
            pass

    def _local_size(self, name: str) -> int:
        """Size of a downloaded file, found by its REMOTE name (rclone may have stored it under a Windows-safe name)."""
        base = os.path.abspath(self.job["dest"])
        for candidate in (name, local_relpath(name)):
            path = os.path.join(base, *candidate.split("/"))
            if IS_WINDOWS:   # extended-length form: works for >260-character paths and for device-like names such as 'nul'
                bs = chr(92)
                prefix = bs * 2 + "?" + bs
                path = prefix + "UNC" + bs + path[2:] if path.startswith(bs * 2) else prefix + path
            try:
                return os.stat(path).st_size
            except OSError:
                continue
        return 0

    # ------------------------------------------------------------------------------------------------ conclusion
    def _conclude(self, code: int | None, outcome: dict[str, Any], tail_lines: list[str]) -> dict[str, Any]:
        file_errors, delete_errors = outcome["file_errors"], outcome["delete_errors"]
        for name, text in file_errors.items():
            self.history.add_file(self.run_id, name, 0, "error", error=humanize_error(text))
        for name, text in delete_errors.items():
            self.history.set_file_note(self.run_id, name, f"Downloaded, but could not be deleted on Nextcloud: {humanize_error(text)}")
        if code not in (0, None) and not self.log_path.is_file() and tail_lines:   # rclone failed before writing its log (e.g. invalid options): keep what it said
            for line in tail_lines:
                self._log_note("error", line)
        got = len(outcome["done"])
        present = len(outcome["present"])
        skipped = len(self._skipped)
        problems = len(file_errors) + len(delete_errors)
        if code not in (0, None) and not problems:
            problems = 1   # a failure that is not tied to a file (server, options): still one problem
        dry = self.job["dry_run"]
        parts = []
        if got:
            parts.append(f"{got} file(s) {'checked (dry-run)' if dry else 'transferred'}")
        if present:
            parts.append(f"{present} already downloaded earlier and removed from Nextcloud")
        if skipped:
            parts.append(f"{skipped} skipped because their names clash with another file on this disk")
        summary = ", ".join(parts)

        def reason() -> str:
            if file_errors:
                name, text = next(iter(file_errors.items()))
                more = f" (and {len(file_errors) - 1} more)" if len(file_errors) > 1 else ""
                return f"{name}: {humanize_error(text)}{more}"
            if delete_errors:
                name, text = next(iter(delete_errors.items()))
                more = f" (and {len(delete_errors) - 1} more)" if len(delete_errors) > 1 else ""
                return f"{len(delete_errors)} file(s) downloaded but not deleted on Nextcloud, e.g. {name}: {humanize_error(text)}{more}"[:300]
            if outcome["general"]:
                text = humanize_error(outcome["general"][-1])
            elif tail_lines:
                text = humanize_error(re.sub(r"^\S+ \S+ [A-Z]+: ", "", tail_lines[-1]))
            else:
                text = exit_message(code)
            return text[:1].upper() + text[1:]

        if outcome["quit"] == "cancelled":
            state, message = "cancelled", "Cancelled by you"
        elif outcome["quit"] == "stopped":
            state, message = "stopped", "Stopped at the stop-by time"
        elif code == 0:
            state = "partial" if skipped else "ok"
            message = summary or "Nothing new to download"
            if skipped:
                message += " (see the log)"
        else:
            state = "partial" if (got or present) else "failed"
            why = reason()
            message = f"{summary}; problem: {why}" if summary else why
        return self._finish(state, message[:480], problems if code not in (0, None) or problems else skipped, exit_code=code)

    def _finish(self, state: str, message: str, errors: int, exit_code: int | None = None) -> dict[str, Any]:
        self.history.finish_run(self.run_id, state, message, errors=errors)
        run = self.history.get_run(self.run_id) or {}
        _log.log(30 if state in ("partial", "failed") else 20, "run %s finished: state=%s exit_code=%s files=%s bytes=%s errors=%s - %s", self.run_id, state, exit_code, run.get("files", 0), run.get("bytes", 0), errors, message)
        self._update(state=state, message=message, eta=None, speed=0.0, transferring=[])
        return {"state": state, "message": message, "bytes": run.get("bytes", 0), "files": run.get("files", 0), "errors": run.get("errors", 0), "run_id": self.run_id, "exit_code": exit_code}
