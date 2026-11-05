"""NextPull: scheduled Nextcloud downloads with rclone. Entry point, the JS bridge (``NextPullApi``) and ``main()``.

The window (pywebview) is only a view. The engine runs in this process on background threads and keeps going while the
window is hidden in the tray. Every public attribute of :class:`NextPullApi` must be a method (pywebview exposes them all
to JavaScript); every bridge method returns ``{"ok": bool, "error": str, ...}`` and never raises to the UI. No bridge method
ever returns the Nextcloud password.
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import applog
import connections
import diagnostics
import installer
import updates
import winutil
from engine import Engine
from nc_client import LoginFlow, NextcloudError, NextcloudSession, normalize_server_url
from rclone_runner import RcloneError, find_rclone, rclone_version
from scheduler import describe_schedule
from store import ConfigStore, HistoryDb, SecretStore, new_job_id

__version__ = "0.3.0"
APP_NAME = "NextPull"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_NAME
LOGIN_TIMEOUT = 20 * 60
_log = applog.get_logger("app")
_ui_log = applog.get_logger("ui")
MAX_UI_ERRORS = 60   # a broken page must not flood the log
UPDATE_FIRST_DELAY = 20.0       # seconds after start before the first update check
UPDATER_EXE = "NextPullUpdater.exe"
UPDATE_INTERVAL = 6 * 3600.0    # then every 6 hours (only while "check for updates" is on)


def safe(method: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Bridge methods must never raise: turn expected errors into ``{"ok": False, "error": ...}``."""
    @functools.wraps(method)
    def wrapper(self: "NextPullApi", *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return method(self, *args, **kwargs)
        except (NextcloudError, RcloneError, ValueError, OSError) as error:
            _log.warning("%s failed: %s", method.__name__, error)
            return {"ok": False, "error": str(error)}
        except Exception as error:   # a bug: say so, keep the app alive
            _log.exception("%s crashed", method.__name__)
            return {"ok": False, "error": f"Unexpected error: {type(error).__name__}: {error}"}
    return wrapper


class NextPullApi:
    def __init__(self, data_dir: Path | None = None, *, notify: Callable[[str, str], None] | None = None, poll_seconds: float = 2.0) -> None:
        self._data_dir = data_dir
        applog.setup_logging(data_dir)
        self._poll_seconds = poll_seconds
        self._config = ConfigStore(data_dir)
        self._history = HistoryDb(data_dir / "history.db" if data_dir else None)
        self._secrets = SecretStore(data_dir)
        self._engine = Engine(self._config, self._history, self._secrets, logs_dir=(data_dir / "logs") if data_dir else None,
                              config_path=(data_dir / "rclone.empty.conf") if data_dir else None, rclone_finder=self._find_rclone, notify=notify)
        self._window: Any = None
        self._quitting = False
        self._tray: Any = None
        self._login: dict[str, Any] = {"state": "idle", "message": ""}
        self._login_cancel = threading.Event()
        self._user_cache: dict[str, Any] | None = None
        self._ui_errors = 0
        self._update_url = updates.API_URL
        self._update_delay = UPDATE_FIRST_DELAY
        self._update_stop = threading.Event()
        self._update: dict[str, Any] = {"checked": "", "error": "", "latest": None}
        self._last_self_test: list[dict[str, str]] | None = None
        self._frozen = bool(getattr(sys, "frozen", False))   # tests switch this to exercise the installer path
        self._program_dir = winutil.exe_dir()
        self._spawn_updater: Callable[[Path, Path], None] = self._start_updater_process
        self._quit_delay = 0.6
        saved = self._secrets.load()
        if saved:
            applog.register_secret(saved["app_password"])
        for record in self._secrets.connections():
            for secret in connections.secrets_of(record):
                applog.register_secret(secret)
        _log.info("NextPull %s opened (data dir %s, connected=%s)", __version__, data_dir, bool(saved))

    # ------------------------------------------------------------------------------------------------ internals
    def _find_rclone(self) -> list[str] | None:
        return find_rclone(self._config.settings()["rclone_path"], [winutil.exe_dir(), winutil.app_dir()])

    def _session(self, conn_id: str = "nextcloud") -> NextcloudSession:
        """A client for one connection (the original Nextcloud login by default)."""
        creds = self._secrets.creds_for(conn_id)
        if not creds:
            raise NextcloudError("Not connected to Nextcloud. Open Settings and connect." if conn_id in ("", "nextcloud") else "That connection no longer exists. Add it again in Settings.")
        for secret in connections.secrets_of(creds):
            applog.register_secret(secret)
        return connections.make_session(creds)

    def _remember_uid(self, session: NextcloudSession) -> None:
        """Keep the user id the session found (older saved logins do not have it) so later calls skip the lookup."""
        creds = self._secrets.load()
        if creds and session.uid and creds.get("uid") != session.uid:
            self._secrets.save(creds["server"], creds["user"], creds["app_password"], session.uid)

    def _connection(self) -> dict[str, Any]:
        creds = self._secrets.load()
        if not creds:
            return {"connected": False}
        info = self._user_cache or {}
        return {"connected": True, "server": creds["server"], "user": creds["user"], "display_name": info.get("display_name") or creds["user"],
                "quota_used": info.get("quota_used"), "quota_total": info.get("quota_total")}

    def _connection_label(self, conn_id: str | None) -> str:
        if not conn_id or conn_id == "nextcloud":
            return "Nextcloud"
        record = self._secrets.creds_for(conn_id)
        return (record or {}).get("label") or "(missing connection)"

    def _job_view(self, job: dict[str, Any], status_jobs: dict[str, dict[str, Any]]) -> dict[str, Any]:
        extra = status_jobs.get(job["id"], {})
        return {**job, "connection_label": self._connection_label(job.get("connection")), "schedule_text": describe_schedule(job["schedule"]), "next_run": extra.get("next_run"), "last_run": extra.get("last_run"), "running": extra.get("running", False)}

    def start_engine(self) -> None:
        self._engine.start()
        self._update_stop.clear()
        threading.Thread(target=self._update_loop, name="nextpull-update-check", daemon=True).start()

    # ------------------------------------------------------------------------------------------------ updates
    def _update_loop(self) -> None:
        wait = self._update_delay
        while not self._update_stop.wait(wait):
            wait = UPDATE_INTERVAL
            if self._config.settings()["check_updates"]:
                self._check_update()

    def _check_update(self) -> None:
        """One check. Never raises; remembers the result for the banner and tells the user (once per version) through a notification."""
        previous = self._update.get("error", "")
        try:
            latest = updates.latest_release(self._update_url)
            self._update = {"checked": datetime.now().isoformat(timespec="seconds"), "error": "", "latest": latest}
        except updates.UpdateError as error:
            self._update = {**self._update, "checked": datetime.now().isoformat(timespec="seconds"), "error": str(error)}
            if str(error) != previous:
                _log.warning("update check failed: %s", error)
            return
        except Exception:   # noqa: BLE001 - a background check must never take the app down
            _log.exception("update check crashed")
            return
        if latest and updates.is_newer(latest["version"], __version__):
            _log.info("update available: %s (running %s)", latest["version"], __version__)
            if self._config.state("update_notified") != latest["version"] and self._config.settings()["notifications"] and self._engine.notify:
                self._config.set_state("update_notified", latest["version"])
                try:
                    self._engine.notify("NextPull update", f"Version {latest['version']} is available")
                except Exception:   # noqa: BLE001
                    _log.warning("could not show the update notification", exc_info=True)

    def _update_view(self) -> dict[str, Any]:
        latest = self._update.get("latest")
        available = bool(latest and updates.is_newer(latest["version"], __version__))
        skipped = bool(available and self._config.state("update_skipped") == latest["version"])
        asset = (latest or {}).get("asset")
        return {"ok": True, "enabled": self._config.settings()["check_updates"], "current": __version__, "available": available, "skipped": skipped, "checked": self._update.get("checked", ""),
                "error": self._update.get("error", ""), "latest": {"version": latest["version"], "name": latest["name"], "notes": latest["notes"], "page": latest["page"], "published": latest["published"],
                                                                       "size": (asset or {}).get("size", 0), "installable": bool(asset), "can_install": bool(asset) and self._can_self_update()} if latest else None}

    @safe
    def check_update(self) -> dict[str, Any]:
        """Check now (the Settings button). Works even when the automatic check is switched off."""
        self._check_update()
        return self._update_view()

    @safe
    def update_info(self) -> dict[str, Any]:
        """What the last check found, without asking GitHub again."""
        return self._update_view()

    @safe
    def skip_update(self, version: str) -> dict[str, Any]:
        """Hide the banner for this version (a newer one shows it again)."""
        self._config.set_state("update_skipped", str(version))
        return self._update_view()

    def _can_self_update(self) -> bool:
        """Only the packaged app (with its updater next to it) can replace itself; a source checkout updates with git."""
        return bool(self._data_dir) and self._frozen and (self._program_dir / "updater" / UPDATER_EXE).is_file()

    def _start_updater_process(self, updater: Path, request_file: Path) -> None:
        subprocess.Popen([str(updater), "--request", str(request_file)], cwd=str(updater.parent), close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=installer.DETACHED if sys.platform == "win32" else 0)

    @safe
    def install_update(self, force: bool = False) -> dict[str, Any]:
        """The user agreed: hand over to NextPullUpdater and quit. The updater backs up, downloads, verifies, swaps and restarts.

        A copy of the updater runs from the data folder, because the one in the program folder would be locked while that folder is replaced.
        """
        latest = self._update.get("latest")
        if not latest or not updates.is_newer(latest["version"], __version__):
            return {"ok": False, "error": "There is no newer version to install"}
        asset = latest.get("asset")
        if not asset:
            return {"ok": False, "error": "This release has no download for NextPull. Open the release page instead"}
        if not self._can_self_update():
            return {"ok": False, "error": "This copy of NextPull cannot update itself (it is not the packaged app). Download the new version from the release page"}
        if self._engine.is_busy() and not force:
            return {"ok": False, "busy": True, "quiet": True, "error": "A download is running right now. It would be stopped and picked up again at its next scheduled time"}
        assert self._data_dir is not None
        run_dir = self._data_dir / "updater-run" / installer.stamp()
        try:
            shutil.copytree(self._program_dir / "updater", run_dir)
        except OSError as error:
            raise OSError(f"Could not prepare the updater: {error}") from error
        request = installer.Request(app_dir=str(self._program_dir), data_dir=str(self._data_dir), current_version=__version__, version=latest["version"], asset_name=asset["name"], asset_url=asset["url"],
                                    size=int(asset.get("size") or 0), sha256=asset.get("sha256", ""), sha256_url=asset.get("sha256_url", ""), notes=latest.get("notes", ""), page=latest.get("page", ""),
                                    name=latest.get("name", ""), pid=os.getpid(), started=datetime.now().isoformat(timespec="seconds"), launch_args=["--data-dir", str(self._data_dir)])
        request_file = self._data_dir / "update" / "request.json"
        request.save(request_file)
        installer.clear_result(self._data_dir)
        _log.info("update to %s: starting the updater (%s)", latest["version"], run_dir.name)
        self._spawn_updater(run_dir / UPDATER_EXE, request_file)
        threading.Timer(self._quit_delay, self.quit_app).start()
        return {"ok": True}

    @safe
    def update_result(self) -> dict[str, Any]:
        """What the last update did (shown once on the Dashboard after the restart)."""
        return {"ok": True, "result": installer.read_result(self._data_dir) if self._data_dir else None}

    @safe
    def dismiss_update_result(self) -> dict[str, Any]:
        if self._data_dir:
            installer.clear_result(self._data_dir)
        return {"ok": True}

    @safe
    def open_backups_folder(self) -> dict[str, Any]:
        folder = (self._data_dir / "backups") if self._data_dir else None
        if not folder or not folder.is_dir():
            return {"ok": False, "error": "There is no backup folder yet"}
        winutil.open_path(folder)
        return {"ok": True}

    def _clean_update_leftovers(self) -> None:
        """After an update: remove the updater copy, the download and half-finished folders (best effort, never raises)."""
        if not self._data_dir:
            return
        try:
            for name in ("updater-run", "update"):
                installer.remove_tree(self._data_dir / name)
            if self._frozen:
                for suffix in (".new", ".unpack", ".failed"):
                    installer.remove_tree(self._program_dir.with_name(self._program_dir.name + suffix))
        except Exception:
            _log.warning("could not clean up after an update", exc_info=True)

    @safe
    def open_release_page(self) -> dict[str, Any]:
        page = (self._update.get("latest") or {}).get("page")
        if not page or not updates.trusted_url(page):
            return {"ok": False, "error": "No release page to open"}
        webbrowser.open(page)
        return {"ok": True}

    def shutdown(self) -> None:
        _log.info("NextPull shutting down")
        self._update_stop.set()
        self._engine.stop()
        self._history.close()
        applog.teardown_logging()

    def _on_closing(self) -> bool:
        """pywebview 'closing' handler: hide to the tray instead of quitting when that is possible."""
        if self._quitting or not self._tray or not self._config.settings()["close_to_tray"]:
            return True
        try:
            self._window.hide()
        except Exception:
            return True
        return False

    # ----------------------------------------------------------------------------------------------- bootstrap
    @safe
    def bootstrap(self) -> dict[str, Any]:
        return {"ok": True, "app": {"name": APP_NAME, "version": __version__}, "settings": self._config.settings(), "connection": self._connection(),
                "rclone": self.check_rclone(), "autostart": winutil.autostart_enabled(), "watchdog": winutil.watchdog_enabled() if winutil.IS_WINDOWS else False,
                "data_dir": str(self._data_dir or ""), "tray": self._tray is not None, "windows": winutil.IS_WINDOWS}

    @safe
    def status(self) -> dict[str, Any]:
        status = self._engine.status()
        status["ok"] = True
        status["connected"] = self._secrets.connected() or bool(self._secrets.connections())
        status["nextcloud_connected"] = self._secrets.connected()
        status["rclone_found"] = self._find_rclone() is not None
        status["events"] = self._engine.events()[:25]
        view = self._update_view()
        status["update"] = {"version": view["latest"]["version"], "installable": view["latest"]["installable"], "can_install": view["latest"]["can_install"]} if view["enabled"] and view["available"] and not view["skipped"] else None
        result = installer.read_result(self._data_dir) if self._data_dir else None
        status["update_result"] = {key: result.get(key) for key in ("ok", "version", "previous_version", "message", "rolled_back", "restore_failed", "cancelled", "backup")} if result else None
        status["problem_count"] = diagnostics.unseen_problem_count(self._history.list_runs(40), self._config.state("problems_seen", ""))
        return status

    # ----------------------------------------------------------------------------------------------- connection
    @safe
    def connection(self, refresh: bool = False) -> dict[str, Any]:
        if refresh and self._secrets.connected():
            self._user_cache = self._session().user_info()
        return {"ok": True, "connection": self._connection(), **({"tls": connections.tls_info(self._connection()["server"])} if refresh and self._secrets.connected() else {})}

    @safe
    def connect_start(self, server: str) -> dict[str, Any]:
        flow = LoginFlow(server)
        login_url = flow.start()
        _log.info("sign-in started for %s", server)
        self._login_cancel.clear()
        self._login = {"state": "waiting", "message": "Approve the sign-in in your browser", "login_url": login_url}
        threading.Thread(target=self._poll_login, args=(flow,), name="nextpull-login", daemon=True).start()
        try:
            webbrowser.open(login_url)
        except Exception:
            pass   # the UI also shows the link
        return {"ok": True, "login_url": login_url}

    def _poll_login(self, flow: LoginFlow) -> None:
        deadline = time.monotonic() + LOGIN_TIMEOUT
        while time.monotonic() < deadline and not self._login_cancel.is_set():
            try:
                creds = flow.poll_once()
            except NextcloudError as error:
                _log.warning("sign-in failed: %s", error)
                self._login = {"state": "error", "message": str(error)}
                return
            if creds:
                applog.register_secret(creds["app_password"])
                self._secrets.save(creds["server"], creds["user"], creds["app_password"])
                _log.info("connected to %s as %s", creds["server"], creds["user"])
                try:
                    session = NextcloudSession(creds["server"], creds["user"], creds["app_password"])
                    self._user_cache = session.user_info()
                    session.uid = self._user_cache.get("id") or None
                    self._remember_uid(session)
                except NextcloudError:
                    self._user_cache = None
                self._login = {"state": "connected", "message": "Connected"}
                return
            self._login_cancel.wait(self._poll_seconds)
        self._login = {"state": "cancelled" if self._login_cancel.is_set() else "error", "message": "Sign-in was cancelled" if self._login_cancel.is_set() else "Sign-in timed out"}

    @safe
    def connect_status(self) -> dict[str, Any]:
        return {"ok": True, **self._login, "connection": self._connection()}

    @safe
    def connect_cancel(self) -> dict[str, Any]:
        self._login_cancel.set()
        return {"ok": True}

    @safe
    def disconnect(self, revoke: bool = True) -> dict[str, Any]:
        if self._engine.is_busy():
            return {"ok": False, "error": "A download is running. Cancel it first."}
        revoked = False
        creds = self._secrets.load()
        if creds and revoke:
            revoked = NextcloudSession(creds["server"], creds["user"], creds["app_password"]).revoke()
        self._secrets.clear()
        _log.info("disconnected (token revoked on server: %s)", revoked)
        self._user_cache = None
        self._login = {"state": "idle", "message": ""}
        return {"ok": True, "revoked": revoked}

    # ----------------------------------------------------------------------------------------- other connections
    @safe
    def list_connections(self) -> dict[str, Any]:
        """Every connection a job can use, without secrets. The Nextcloud login (when present) comes first with the fixed id ``nextcloud``."""
        items: list[dict[str, Any]] = []
        nc = self._secrets.load()
        if nc:
            items.append({"id": "nextcloud", "type": "nextcloud", "type_label": "Nextcloud", "label": "Nextcloud", "server": nc["server"], "user": nc["user"], "has_access_key": False,
                          "secure": nc["server"].startswith("https://")})
        items += [connections.public_view(record) for record in self._secrets.connections()]
        return {"ok": True, "connections": items}

    @safe
    def discover_server(self, address: str) -> dict[str, Any]:
        """What is at this address? ``{type: nextcloud|sftpgo|unknown, ...}``; the form uses it to pick the right sign-in."""
        return connections.discover(address)

    @safe
    def read_invite(self, code: str) -> dict[str, Any]:
        """Prefill data from an invite code (address, optional user name and access key; never a password)."""
        return {"ok": True, **connections.parse_invite(code)}

    @safe
    def add_connection(self, record: dict[str, Any]) -> dict[str, Any]:
        """Validate, **test the login against the server**, and only then save (encrypted). Nothing is stored when the test fails."""
        clean = connections.clean_connection(record)
        for secret in connections.secrets_of(clean):
            applog.register_secret(secret)
        connections.make_session(clean, timeout=20).user_info()
        self._secrets.save_connection(clean)
        _log.info("connection added: %s (%s, user %s)", clean["id"], clean["server"], clean["user"])
        return {"ok": True, "connection": connections.public_view(clean)}

    @safe
    def test_connection(self, conn_id: str) -> dict[str, Any]:
        started = time.monotonic()
        session = self._session(conn_id)
        session.user_info()
        return {"ok": True, "seconds": round(time.monotonic() - started, 2), "tls": connections.tls_info(session.server)}

    @safe
    def remove_connection(self, conn_id: str) -> dict[str, Any]:
        if conn_id in ("", "nextcloud"):
            return {"ok": False, "error": "Use Disconnect in the Nextcloud card for the Nextcloud login"}
        users = [job["name"] for job in self._config.jobs() if job.get("connection") == conn_id]
        if users:
            return {"ok": False, "error": f"{len(users)} job(s) use this connection ({', '.join(users[:3])}). Edit or delete them first."}
        removed = self._secrets.delete_connection(conn_id)
        _log.info("connection removed: %s (%s)", conn_id, removed)
        return {"ok": removed, **({} if removed else {"error": "That connection no longer exists"})}

    # -------------------------------------------------------------------------------------------------- browsing
    @safe
    def browse(self, path: str = "", connection: str = "nextcloud") -> dict[str, Any]:
        session = self._session(connection)
        entries = [entry.to_dict() for entry in session.list_dir(path)]
        if connection in ("", "nextcloud"):
            self._remember_uid(session)
        clean = "/".join(part for part in str(path).replace("\\", "/").split("/") if part)
        parent = clean.rsplit("/", 1)[0] if "/" in clean else ""
        return {"ok": True, "path": clean, "parent": parent if clean else None, "entries": entries}

    # ------------------------------------------------------------------------------------------------------ jobs
    @safe
    def list_jobs(self) -> dict[str, Any]:
        status_jobs = {item["id"]: item for item in self._engine.status()["jobs"]}
        return {"ok": True, "jobs": [self._job_view(job, status_jobs) for job in self._config.jobs()]}

    @safe
    def save_job(self, job: dict[str, Any]) -> dict[str, Any]:
        saved = self._config.save_job(job)
        return {"ok": True, "job": saved}

    @safe
    def delete_job(self, job_id: str) -> dict[str, Any]:
        self._engine.cancel(job_id)
        return {"ok": self._config.delete_job(job_id)}

    @safe
    def set_job_enabled(self, job_id: str, enabled: bool) -> dict[str, Any]:
        job = self._config.job(job_id)
        if not job:
            return {"ok": False, "error": "That job no longer exists"}
        job["enabled"] = bool(enabled)
        return {"ok": True, "job": self._config.save_job(job)}

    @safe
    def duplicate_job(self, job_id: str) -> dict[str, Any]:
        job = self._config.job(job_id)
        if not job:
            return {"ok": False, "error": "That job no longer exists"}
        job.update({"id": new_job_id(), "name": f"{job['name']} (copy)"[:60], "enabled": False})
        return {"ok": True, "job": self._config.save_job(job)}

    @safe
    def run_job(self, job_id: str, dry_run: bool | None = None) -> dict[str, Any]:
        return self._engine.run_now(job_id, dry_run)

    @safe
    def cancel_run(self) -> dict[str, Any]:
        return {"ok": self._engine.cancel()}

    @safe
    def set_speed(self, mbps: float) -> dict[str, Any]:
        """Change the speed limit of the run in progress (MB/s, 0 = unlimited). Not saved into the job."""
        value = float(mbps)
        if value < 0:
            raise ValueError("The speed cannot be negative")
        done = self._engine.set_bandwidth(f"{value:g}M" if value > 0 else "off")
        return {"ok": done, **({} if done else {"error": "No download is running"})}

    # --------------------------------------------------------------------------------------- history and stats
    @safe
    def history(self, limit: int = 50, offset: int = 0, job_id: str = "") -> dict[str, Any]:
        return {"ok": True, "runs": self._history.list_runs(int(limit), int(offset), job_id or None)}

    @safe
    def run_detail(self, run_id: int) -> dict[str, Any]:
        run = self._history.get_run(int(run_id))
        if not run:
            return {"ok": False, "error": "That run no longer exists"}
        return {"ok": True, "run": run, "files": self._history.list_files(int(run_id))}

    @safe
    def run_log(self, run_id: int, tail: int = 400) -> dict[str, Any]:
        return {"ok": True, "lines": self._engine.read_log(int(run_id), int(tail))}

    @safe
    def clear_history(self) -> dict[str, Any]:
        self._history.clear()
        return {"ok": True}

    @safe
    def stats(self, days: int = 30) -> dict[str, Any]:
        return {"ok": True, **self._history.stats(int(days))}

    # ---------------------------------------------------------------------------------------------- settings
    @safe
    def get_settings(self) -> dict[str, Any]:
        return {"ok": True, "settings": self._config.settings()}

    @safe
    def save_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "settings": self._config.save_settings({**self._config.settings(), **(settings if isinstance(settings, dict) else {})})}

    @safe
    def check_rclone(self, path: str = "") -> dict[str, Any]:
        command = find_rclone(path.strip(), [winutil.exe_dir(), winutil.app_dir()]) if path.strip() else self._find_rclone()
        if not command:
            return {"ok": True, "found": False, "message": "rclone was not found. Put rclone.exe in the tools\\rclone folder next to NextPull, or set its path in Settings."}
        try:
            return {"ok": True, "found": True, "path": command[0], "version": rclone_version(command)}
        except RcloneError as error:
            return {"ok": True, "found": False, "path": command[0], "message": str(error)}

    @safe
    def set_autostart(self, enabled: bool) -> dict[str, Any]:
        winutil.set_autostart(bool(enabled))
        return {"ok": True, "autostart": winutil.autostart_enabled()}

    @safe
    def set_watchdog(self, enabled: bool) -> dict[str, Any]:
        winutil.set_watchdog(bool(enabled))
        return {"ok": True, "watchdog": winutil.watchdog_enabled()}

    @safe
    def export_jobs(self) -> dict[str, Any]:
        return {"ok": True, "text": self._config.export_jobs()}

    @safe
    def import_jobs(self, text: str) -> dict[str, Any]:
        return {"ok": True, "imported": self._config.import_jobs(text)}

    # --------------------------------------------------------------------------------------------------- files
    @safe
    def pick_folder(self, initial: str = "") -> dict[str, Any]:
        if self._window is None:
            return {"ok": False, "error": "No window"}
        import webview
        kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or webview.FOLDER_DIALOG
        chosen = self._window.create_file_dialog(kind, directory=initial or "")
        return {"ok": True, "path": chosen[0] if chosen else ""}

    @safe
    def test_destination(self, path: str) -> dict[str, Any]:
        target = Path(str(path).strip())
        if not str(path).strip():
            raise ValueError("Pick the destination folder first")
        free = winutil.free_bytes(target)
        exists = target.is_dir()
        writable = False
        probe = target if exists else next((parent for parent in target.parents if parent.is_dir()), None)
        if probe is not None:
            writable = os.access(probe, os.W_OK)
        return {"ok": True, "exists": exists, "writable": writable, "free_bytes": free}

    @safe
    def open_path(self, path: str) -> dict[str, Any]:
        target = Path(str(path))
        if not target.exists():
            return {"ok": False, "error": "That folder does not exist yet"}
        winutil.open_path(target)
        return {"ok": True}

    @safe
    def open_logs(self) -> dict[str, Any]:
        folder = (self._data_dir or Path(".")) / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        winutil.open_path(folder)
        return {"ok": True}

    # ------------------------------------------------------------------------------------------- diagnostics
    @safe
    def app_log(self, tail: int = 300, level: str = "", query: str = "") -> dict[str, Any]:
        """Recent lines of NextPull's own log (already redacted); level is a minimum such as WARNING."""
        return {"ok": True, "entries": applog.read_log(self._data_dir, int(tail), str(level), str(query))}

    @safe
    def log_ui_error(self, message: str, source: str = "", line: int = 0) -> dict[str, Any]:
        """The window reports its own JavaScript errors here so they reach the log (capped per session)."""
        self._ui_errors += 1
        if self._ui_errors <= MAX_UI_ERRORS:
            _ui_log.error("%s (%s:%s)", str(message)[:600], str(source)[-80:], line)
        return {"ok": True}

    @safe
    def diagnostics(self, days: int = 7) -> dict[str, Any]:
        """What is wrong right now: grouped problem runs, warnings from the application log, and the system info."""
        since = (datetime.now() - timedelta(days=max(1, int(days)))).isoformat(timespec="seconds")
        runs = self._history.list_runs(300)
        return {"ok": True, "runs": diagnostics.group_problems(runs, since), "log": diagnostics.app_log_problems(applog.read_log(self._data_dir, 2000, "WARNING")),
                "unseen": diagnostics.unseen_problem_count(runs, self._config.state("problems_seen", "")), "system": diagnostics.system_info(__version__),
                "log_file": str(self._data_dir / "app.log") if self._data_dir else ""}

    @safe
    def ack_problems(self) -> dict[str, Any]:
        self._config.set_state("problems_seen", datetime.now().isoformat(timespec="seconds"))
        return {"ok": True}

    @safe
    def self_test(self) -> dict[str, Any]:
        _log.info("self-test started")
        known = self.list_connections()["connections"]
        results = diagnostics.run_self_test(rclone=self.check_rclone, session=self._session if known else None, connections=[(item["id"], item["label"]) for item in known], jobs=self._config.jobs(), engine_last_tick=self._engine.last_tick,
                                            tick_seconds=self._engine.tick_seconds, data_dir=self._data_dir, free_bytes=winutil.free_bytes, autostart=winutil.autostart_enabled(),
                                            watchdog=winutil.watchdog_enabled() if winutil.IS_WINDOWS else False)
        self._last_self_test = results
        bad = [item for item in results if item["status"] == "error"]
        _log.info("self-test finished: %d ok, %d warnings, %d errors", sum(i["status"] == "ok" for i in results), sum(i["status"] == "warn" for i in results), len(bad))
        for item in bad:
            _log.warning("self-test problem: %s - %s", item["title"], item["detail"])
        return {"ok": True, "results": results}

    @safe
    def export_report(self, include_names: bool = False, note: str = "") -> dict[str, Any]:
        """Build the zip a user can send. Includes the last self-test if one was run. Never includes a password."""
        out_dir = (self._data_dir or Path(tempfile.gettempdir()) / "nextpull-test") / "reports"
        runs = self._history.list_runs(100)
        path = diagnostics.build_report(version=__version__, data_dir=self._data_dir, out_dir=out_dir, settings=self._config.settings(), jobs=self._config.jobs(), runs=runs,
                                        files_for=self._history.list_files, problems=diagnostics.group_problems(runs), self_test=self._last_self_test,
                                        connection=self._connection(), include_names=bool(include_names), note=str(note)[:2000],
                                        extra_literals=[value for record in self._secrets.connections() for value in (record.get("server"), record.get("user"), record.get("label"))])
        _log.info("report written: %s (names included: %s)", path.name, bool(include_names))
        return {"ok": True, "path": str(path), "folder": str(out_dir), "size": path.stat().st_size}

    @safe
    def reveal_report(self, path: str) -> dict[str, Any]:
        """Open Explorer with the report selected, so it can be attached to a message."""
        target = Path(str(path))
        if not target.is_file() or target.parent.name != "reports":
            return {"ok": False, "error": "That report no longer exists"}
        winutil.reveal_path(target)
        return {"ok": True}

    # ---------------------------------------------------------------------------------------------- window
    @safe
    def hide_window(self) -> dict[str, Any]:
        if self._window:
            self._window.hide()
        return {"ok": True}

    @safe
    def show_window(self) -> dict[str, Any]:
        if self._window:
            self._window.show()
            self._window.restore()
        return {"ok": True}

    @safe
    def quit_app(self) -> dict[str, Any]:
        self._quitting = True
        if self._tray:
            self._tray.stop()
        if self._window:
            self._window.destroy()
        return {"ok": True}


# ---------------------------------------------------------------------------------------------------------- main

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=APP_NAME, description="Scheduled Nextcloud downloads with rclone")
    parser.add_argument("--background", action="store_true", help="start hidden in the tray (used by start with Windows and the watchdog)")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR, help="where jobs, history and logs are kept")
    parser.add_argument("--self-check", type=Path, metavar="REPORT", help="used by the updater: load everything against --data-dir, write a JSON report and exit (no window)")
    return parser.parse_args(argv)


def self_check(report: Path, data_dir: Path) -> int:
    """Prove this program starts and can read a settings folder, without a window, a tray, the network or the real data."""
    problems: list[str] = []
    try:
        web_dir = winutil.app_dir() / "web"
        for name in ("index.html", "app.js", "style.css"):
            if not (web_dir / name).is_file():
                problems.append(f"missing web/{name}")
        import webview   # noqa: F401 - the window library must be importable
        api = NextPullApi(data_dir)
        try:
            api._config.jobs()
            api._config.settings()
            api._history.list_runs(1)
            api._secrets.connections()
        finally:
            api.shutdown()
    except Exception as error:
        problems.append(f"{type(error).__name__}: {error}")
    try:
        report.write_text(json.dumps({"ok": not problems, "version": __version__, "problems": problems}), encoding="utf-8")
    except OSError:
        return 2
    return 0 if not problems else 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_check:
        return self_check(args.self_check, args.data_dir)
    single = winutil.SingleInstance()
    if not single.acquire():
        single.signal_existing()   # a second launch just raises the window of the running one
        return 0
    import webview
    from tray import Tray
    web_dir = winutil.app_dir() / "web"
    api = NextPullApi(args.data_dir)
    api._clean_update_leftovers()
    applog.install_excepthooks()
    tray = Tray(api)
    api._tray = tray if tray.available else None
    api._engine.notify = tray.notify if tray.available else None
    api.start_engine()
    hidden = args.background and tray.available
    window = webview.create_window(f"{APP_NAME} {__version__}", (web_dir / "index.html").as_uri(), js_api=api, width=1280, height=860, min_size=(980, 640), background_color="#0d1525", hidden=hidden)
    api._window = window
    window.events.closing += api._on_closing
    single.watch(api.show_window)
    if tray.available:
        tray.start()
    webview.start(debug=False)
    api._quitting = True
    api.shutdown()
    single.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
