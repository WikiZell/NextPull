"""NextPullUpdater: the small companion program that replaces NextPull with a newer release.

NextPull starts a copy of this program from its data folder (``--request update/request.json``) and quits. The window shows the
steps of :class:`installer.Installer`; this file only connects the two. See docs/UPDATES.md.

    NextPullUpdater.exe --request <file>          # what NextPull runs
    NextPullUpdater.exe --demo ok|fail|rollback    # the window with a pretend update (touches nothing)
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import installer
import updates
import winutil
from installer import Installer, Outcome, Request, human_bytes

VERSION = "1.0"
FORCE_AFTER = 15.0          # seconds to wait for NextPull to close before offering "Close it now"
MAX_LOG_LINES = 300
SUCCESS_CLOSE_AFTER = 6     # seconds; the window closes itself after a good update


def allowed_url(url: str) -> bool:
    """GitHub only. ``NEXTPULL_UPDATER_ALLOW_LOOPBACK=1`` also allows http://127.0.0.1 (used by the end-to-end test; the
    variable can only be set by whoever already runs programs as this user)."""
    if updates.trusted_url(url):
        return True
    if os.environ.get("NEXTPULL_UPDATER_ALLOW_LOOPBACK") == "1":
        parts = urlsplit(url)
        return parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost")
    return False


class InstanceLock:
    """Holds NextPull's single-instance mutex so the watchdog, auto-start and the user cannot start the old program mid-update."""

    def __init__(self) -> None:
        self._single = winutil.SingleInstance()

    def acquire(self) -> bool:
        return self._single.acquire()

    def release(self) -> None:
        self._single.release()


def kill_process_tree(pid: int) -> None:
    if pid > 0 and sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=installer.CREATE_NO_WINDOW, timeout=30)


class UpdaterState:
    """The Reporter for the window: the worker thread writes, the window polls ``snapshot()`` a few times a second."""

    def __init__(self, request: Request, log_path: Path | None = None) -> None:
        self.request = request
        self._lock = threading.RLock()
        self._steps = [{"key": key, "label": label, "state": "pending", "detail": ""} for key, label in installer.STEPS]
        self._progress = {"done": 0, "total": 0, "speed": 0.0}
        self._log: list[str] = []
        self._cancel = threading.Event()
        self._can_cancel = False
        self._need_force = False
        self.outcome: Outcome | None = None
        self.log_path = log_path
        self.started = time.monotonic()
        self._file = logging.getLogger(f"updater.{id(self)}")
        self._file.setLevel(logging.INFO)
        self._file.propagate = False
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(log_path, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
            self._file.addHandler(handler)
        self.log(f"NextPullUpdater {VERSION}: {request.current_version} -> {request.version}")

    # -- Reporter -----------------------------------------------------------------------------------------------
    def step(self, key: str, state: str, detail: str = "") -> None:
        with self._lock:
            for item in self._steps:
                if item["key"] == key:
                    item["state"], item["detail"] = state, detail
                elif state in ("active", "error", "skipped") and item["state"] == "active":
                    item["state"] = "done"   # a checksum failure is reported on the verify step; the download itself did finish
            if state == "active" and key != "download":
                self._progress = {"done": 0, "total": 0, "speed": 0.0}
        self.log(f"[{key}] {state}" + (f": {detail}" if detail else ""))

    def progress(self, done: int, total: int, speed: float) -> None:
        with self._lock:
            self._progress = {"done": done, "total": total, "speed": speed}

    def log(self, text: str) -> None:
        with self._lock:
            self._log.append(f"{datetime.now():%H:%M:%S}  {text}")
            del self._log[:-MAX_LOG_LINES]
        self._file.info(text)

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def can_cancel(self, allowed: bool) -> None:
        with self._lock:
            self._can_cancel = allowed

    def need_force(self, allowed: bool) -> None:
        with self._lock:
            self._need_force = allowed

    # -- window -----------------------------------------------------------------------------------------------------
    def request_cancel(self) -> bool:
        with self._lock:
            if not self._can_cancel and not self._need_force:
                return False
        self._cancel.set()
        self.log("Cancel requested")
        return True

    def finish(self, outcome: Outcome) -> None:
        with self._lock:
            self.outcome = outcome

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            request, outcome = self.request, self.outcome
            steps = [dict(item) for item in self._steps]
            progress = dict(self._progress)
            phase = "running" if outcome is None else "success" if outcome.ok else "cancelled" if outcome.cancelled else "failed"
            headline, sub = describe(phase, steps, outcome, request, self._need_force)
            remaining = (progress["total"] - progress["done"]) / progress["speed"] if progress["speed"] > 0 and progress["total"] else 0
            active = next((item["key"] for item in steps if item["state"] == "active"), "")
            return {"phase": phase, "headline": headline, "sub": sub, "steps": steps, "active": active,
                    "progress": {"done": progress["done"], "total": progress["total"], "speed": progress["speed"], "eta": remaining,
                                 "text": progress_text(progress)},
                    "canCancel": self._can_cancel and outcome is None, "needForce": self._need_force and outcome is None,
                    "release": {"version": request.version, "current": request.current_version, "name": request.name or f"NextPull {request.version}", "notes": request.notes,
                                "size": request.size, "sizeText": human_bytes(request.size) if request.size else "", "page": request.page},
                    "outcome": outcome.to_json() if outcome else None, "log": list(self._log), "logPath": str(self.log_path or ""),
                    "elapsed": time.monotonic() - self.started, "closeAfter": SUCCESS_CLOSE_AFTER}


def progress_text(progress: dict[str, Any]) -> str:
    done, total, speed = progress["done"], progress["total"], progress["speed"]
    if not done and not total:
        return ""
    text = f"{human_bytes(done)} of {human_bytes(total)}" if total else human_bytes(done)
    if speed > 0:
        text += f"  ·  {human_bytes(speed)}/s"
        if total and done < total:
            seconds = int((total - done) / speed)
            text += f"  ·  {seconds // 60} min {seconds % 60} s left" if seconds >= 60 else f"  ·  {max(seconds, 1)} s left"
    return text


ACTIVE_TEXT = {
    "wait": ("Closing NextPull", "Waiting for NextPull to finish what it is doing. A running download is stopped cleanly."),
    "backup": ("Backing up your settings", "Your jobs, history and sign-ins are copied to a safe place first."),
    "download": ("Downloading the update", "From GitHub, and only from GitHub."),
    "verify": ("Checking the new version", "Verifying the download, then test-starting it on a copy of your settings."),
    "install": ("Installing", "Swapping the program files. This takes a moment."),
    "restart": ("Starting the new version", "Making sure it runs properly. If it does not, the old version comes back."),
}


def describe(phase: str, steps: list[dict[str, str]], outcome: Outcome | None, request: Request, need_force: bool = False) -> tuple[str, str]:
    """The big headline and the sentence under it. Plain language; the facts (what was restored, what was kept) come from the outcome."""
    new, old = request.version, request.current_version
    if phase == "running":
        active = next((item["key"] for item in steps if item["state"] == "active"), "wait")
        if active == "wait" and need_force:
            return "NextPull is taking a while to close", "You can wait a little longer, or close it now. Any running download will be picked up again at its next scheduled time."
        title, sub = ACTIVE_TEXT.get(active, ACTIVE_TEXT["wait"])
        return title, sub
    assert outcome is not None
    if phase == "success":
        return f"NextPull {new} is installed", "Your settings were backed up first and are unchanged. NextPull is running again."
    if outcome.restore_failed:
        return "The update failed, and the automatic repair did not finish", "Your settings are safe in the backup. Open the backup folder below; the previous program files are kept next to NextPull as NextPull.previous."
    if phase == "cancelled":
        return "Update cancelled", f"Nothing was changed. NextPull {old} is running again." if outcome.step != "wait" else "Nothing was changed. NextPull was left running."
    if outcome.rolled_back:
        return "The update didn't work, so it was undone", f"NextPull {old} is back exactly as it was, and your settings were restored from the backup. NextPull is running again."
    return "The update was not installed", f"Nothing was changed. NextPull {old} is running again."


def run_update(request: Request, state: UpdaterState, lock: InstanceLock | None = None, **overrides: Any) -> Outcome:
    lock = lock or InstanceLock()
    engine = Installer(request, state, allowed=allowed_url, acquire_lock=lock.acquire, release_lock=lock.release, lock_seconds=FORCE_AFTER, **overrides)
    outcome = engine.run()
    state.finish(outcome)
    return outcome


# ------------------------------------------------------------------------------------------------------------- bridge

class UpdaterApi:
    """pywebview exposes every public attribute: keep them all methods that return ``{"ok": ...}``."""

    def __init__(self, state: UpdaterState) -> None:
        self._state = state
        self._window: Any = None
        self._worker: threading.Thread | None = None

    def get_state(self) -> dict[str, Any]:
        return {"ok": True, **self._state.snapshot()}

    def cancel(self) -> dict[str, Any]:
        return {"ok": self._state.request_cancel()}

    def force_close(self) -> dict[str, Any]:
        pid = self._state.request.pid
        self._state.log(f"Closing NextPull (process {pid}) by force")
        try:
            kill_process_tree(pid)
        except Exception as error:   # the window must keep working whatever happens here
            self._state.log(f"Could not close it: {error}")
            return {"ok": False, "error": str(error)}
        return {"ok": True}

    def open_release_page(self) -> dict[str, Any]:
        page = self._state.request.page
        if page and updates.trusted_url(page):
            webbrowser.open(page)
            return {"ok": True}
        return {"ok": False, "error": "No release page"}

    def open_backups_folder(self) -> dict[str, Any]:
        folder = Path(self._state.request.data_dir) / "backups"
        if folder.is_dir():
            winutil.open_path(folder)
            return {"ok": True}
        return {"ok": False, "error": "No backup folder"}

    def open_log(self) -> dict[str, Any]:
        path = self._state.log_path
        if path and path.is_file():
            winutil.open_path(path)
            return {"ok": True}
        return {"ok": False, "error": "No log file"}

    def finish(self) -> dict[str, Any]:
        """Close the window (only once the update is over)."""
        if self._state.outcome is None:
            return {"ok": False, "error": "The update is still running"}
        if self._window:
            threading.Timer(0.05, self._window.destroy).start()
        return {"ok": True}

    def _on_closing(self) -> bool:
        if self._state.outcome is not None:
            return True
        self._state.request_cancel()   # while it can still be cancelled this stops the update; later the window just stays
        return False


# --------------------------------------------------------------------------------------------------------------- demo

def demo(state: UpdaterState, scenario: str) -> None:
    """Drive the window with a pretend update so it can be seen (and screenshotted) without touching anything."""
    request = state.request

    def pause(seconds: float) -> None:
        time.sleep(seconds)

    state.step("wait", "active")
    pause(1.5)
    state.step("wait", "done")
    state.step("backup", "active")
    pause(1.2)
    state.step("backup", "done", "20261105-101500-v0.3.0.zip")
    state.step("download", "active")
    state.can_cancel(True)
    total = request.size or 85_000_000
    done = 0
    while done < total:
        if state.cancelled():
            break
        done = min(total, done + 2_100_000)
        state.progress(done, total, 7_400_000)
        pause(0.12)
    state.can_cancel(False)
    state.step("download", "done", human_bytes(total))
    state.step("verify", "active")
    pause(1.5)
    state.step("verify", "done", f"Version {request.version} works with your settings")
    state.step("install", "active")
    pause(1.0)
    if scenario == "ok":
        state.step("install", "done")
        state.step("restart", "active")
        pause(2.0)
        state.step("restart", "done")
        state.finish(Outcome(ok=True, version=request.version, previous_version=request.current_version, backup=r"C:\Users\you\AppData\Local\NextPull\backups\20261105-101500-v0.2.0.zip"))
    else:
        state.step("install", "done")
        state.step("restart", "active")
        pause(2.0)
        state.step("restart", "error", "The new version stopped right after starting (exit code 3)")
        state.log("Restoring the previous version")
        pause(1.0)
        state.finish(Outcome(ok=False, version=request.version, previous_version=request.current_version, step="restart", rolled_back=True,
                             message="The new version stopped right after starting (exit code 3)"))


# --------------------------------------------------------------------------------------------------------------- main

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="NextPullUpdater", description="Installs a NextPull update")
    parser.add_argument("--request", type=Path, help="the update request written by NextPull")
    parser.add_argument("--demo", choices=["ok", "fail"], help="show the window with a pretend update")
    return parser.parse_args(argv)


def fatal(message: str) -> None:
    """A windowed program has no console: show a message box (Windows) and also print."""
    print(message, file=sys.stderr)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, "NextPull Update", 0x10)
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.demo:
        request = Request(app_dir=r"C:\NextPull", data_dir=r"C:\Users\you\AppData\Local\NextPull", current_version="0.2.0", version="0.3.0", asset_name="NextPull-0.3.0.zip",
                          asset_url="https://github.com/WikiZell/NextPull/releases/download/v0.3.0/NextPull-0.3.0.zip", size=85_000_000, name="NextPull 0.3.0",
                          notes="## What's new\n- One-click updates\n- Backups before every update\n- Fixes", page="https://github.com/WikiZell/NextPull/releases/tag/v0.3.0")
        state = UpdaterState(request)
        worker: Callable[[], Any] = lambda: demo(state, args.demo)   # noqa: E731
    elif args.request:
        try:
            request = Request.load(args.request)
        except (OSError, ValueError, installer.UpdateFailed) as error:
            fatal(f"The update request could not be read ({error}). Nothing was changed.")
            return 2
        state = UpdaterState(request, Path(request.data_dir) / "logs" / f"updater-{installer.stamp()}.log")
        worker = lambda: run_update(request, state)   # noqa: E731
    else:
        print("Usage: NextPullUpdater --request <file>", file=sys.stderr)
        return 2
    import webview
    api = UpdaterApi(state)
    web_dir = winutil.app_dir() / "updater_web"
    window = webview.create_window("NextPull Update", (web_dir / "index.html").as_uri(), js_api=api, width=580, height=720, min_size=(520, 640),
                                   background_color="#0d1525", on_top=True, text_select=False)
    api._window = window
    window.events.closing += api._on_closing

    def work() -> None:
        try:
            worker()
        except Exception as error:   # the engine reports its own failures; this is a bug in the updater itself
            state.log(f"The updater crashed: {type(error).__name__}: {error}")
            state.finish(Outcome(ok=False, version=request.version, previous_version=request.current_version, message=f"Unexpected error: {error}"))

    api._worker = threading.Thread(target=work, name="updater-worker", daemon=True)
    webview.start(api._worker.start, debug=False)
    return 0 if state.outcome is not None and state.outcome.ok else 1


if __name__ == "__main__":
    sys.exit(main())
