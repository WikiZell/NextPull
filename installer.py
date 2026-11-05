"""Install a NextPull update safely. The engine behind ``NextPullUpdater`` (see docs/UPDATES.md).

Order of work (nothing the user owns is touched before the new version has proved it starts):

1. ``wait``     hold the single-instance lock, so the old app, the watchdog and auto-start cannot run while files move
2. ``backup``   zip every settings/history file of the data folder (checked by reading it back)
3. ``download`` fetch the release zip from GitHub only (every redirect hop is checked)
4. ``verify``   SHA-256, safe unpack next to the install, then run the NEW program once with ``--self-check`` on a copy of the backup
5. ``install``  rename the install folder to ``<name>.previous``, rename the new folder into place (same volume, so it is instant)
6. ``restart``  start the new version and watch it; a crash rolls everything back (old folder renamed back, settings restored)

A failure at any step puts the old version back (and starts it again). Pure Python, no GUI: the window only listens to a
:class:`Reporter`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

import updates

STEPS: list[tuple[str, str]] = [
    ("wait", "Close NextPull"),
    ("backup", "Back up your settings"),
    ("download", "Download the update"),
    ("verify", "Check the new version"),
    ("install", "Install"),
    ("restart", "Start NextPull"),
]
BACKUPS_TO_KEEP = 5
CHUNK = 256 * 1024
EXE_NAME = "NextPull.exe"
RESULT_FILE = "update-result.json"
SKIP_NAMES = {"app.log", "update-result.json"}      # never worth keeping in a backup
SKIP_SUFFIXES = (".partial", ".tmp", ".lock")
MAX_UNPACKED = 3 * 1024 ** 3                         # a release larger than this is not NextPull
CREATE_NO_WINDOW = 0x08000000
DETACHED = 0x00000008 | 0x00000200                   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


class UpdateFailed(Exception):
    """A failure with a message fit for the window. ``step`` says which step it happened in."""

    def __init__(self, message: str, step: str = "") -> None:
        super().__init__(message)
        self.step = step


class Cancelled(UpdateFailed):
    pass


class Reporter(Protocol):
    """What the engine tells the window. The window reads these; the engine never reads the window except ``cancelled()``."""

    def step(self, key: str, state: str, detail: str = "") -> None: ...
    def progress(self, done: int, total: int, speed: float) -> None: ...
    def log(self, text: str) -> None: ...
    def cancelled(self) -> bool: ...
    def can_cancel(self, allowed: bool) -> None: ...
    def need_force(self, allowed: bool) -> None: ...


class NullReporter:
    def step(self, key: str, state: str, detail: str = "") -> None: pass
    def progress(self, done: int, total: int, speed: float) -> None: pass
    def log(self, text: str) -> None: pass
    def cancelled(self) -> bool: return False
    def can_cancel(self, allowed: bool) -> None: pass
    def need_force(self, allowed: bool) -> None: pass


@dataclass
class Request:
    """Everything the updater is told by NextPull (a JSON file, so nothing long travels on a command line)."""
    app_dir: str
    data_dir: str
    current_version: str
    version: str
    asset_name: str
    asset_url: str
    size: int = 0
    sha256: str = ""
    sha256_url: str = ""
    notes: str = ""
    page: str = ""
    name: str = ""
    pid: int = 0
    exe_name: str = EXE_NAME
    started: str = ""
    launch_args: list[str] = field(default_factory=list)   # how to start the program again (for example --data-dir), so a custom setup survives

    @classmethod
    def load(cls, path: Path) -> "Request":
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))   # -sig: tolerate a BOM from Windows PowerShell
        known = {key: raw[key] for key in cls.__dataclass_fields__ if key in raw}
        for needed in ("app_dir", "data_dir", "version", "asset_url"):
            if not known.get(needed):
                raise UpdateFailed(f"The update request is missing '{needed}'")
        return cls(**known)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


@dataclass
class Outcome:
    ok: bool
    version: str
    previous_version: str
    message: str = ""
    rolled_back: bool = False
    cancelled: bool = False
    restore_failed: bool = False
    backup: str = ""
    step: str = ""
    finished: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------------------------------------------------ helpers

def stamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


def human_bytes(value: float) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def retry(action: Callable[[], Any], what: str, report: Reporter, seconds: float = 45.0, pause: float = 0.5) -> Any:
    """Windows keeps folders locked for a moment after a program exits (antivirus, the search indexer). Retry for a while."""
    deadline = time.monotonic() + seconds
    announced = False
    while True:
        try:
            return action()
        except PermissionError as error:
            if time.monotonic() >= deadline:
                raise UpdateFailed(f"Windows would not release the files ({what}): {error}") from error
            if not announced:
                report.log(f"Waiting for Windows to release the files ({what})")
                announced = True
            time.sleep(pause)


def remove_tree(path: Path, report: Reporter | None = None) -> bool:
    """Best-effort delete (read-only files included). False if something stayed behind; never raises."""
    def onerror(function: Callable[..., Any], target: str, _info: Any) -> None:
        try:
            os.chmod(target, 0o700)
            function(target)
        except OSError:
            pass
    if path.is_dir():
        shutil.rmtree(path, onerror=onerror)
    elif path.exists():
        try:
            path.unlink()
        except OSError:
            pass
    left = path.exists()
    if left and report:
        report.log(f"Could not remove {path.name}")
    return not left


# ------------------------------------------------------------------------------------------------------------ backup

def _config_files(data_dir: Path) -> list[Path]:
    """Top-level files of the data folder that make up the user's setup (jobs, settings, sign-ins, history)."""
    found = []
    for entry in sorted(data_dir.iterdir()):
        if not entry.is_file() or entry.name in SKIP_NAMES or entry.name.endswith(SKIP_SUFFIXES) or entry.name.startswith("app.log"):
            continue
        found.append(entry)
    return found


def backup_config(data_dir: Path, backups_dir: Path, version: str, now: datetime | None = None) -> Path:
    """Zip the setup into ``backups/<time>-v<version>.zip`` with a manifest of SHA-256 sums, then read it back to be sure."""
    if not data_dir.is_dir():
        raise UpdateFailed(f"The data folder does not exist: {data_dir}", "backup")
    backups_dir.mkdir(parents=True, exist_ok=True)
    target = backups_dir / f"{stamp(now)}-v{re.sub(r'[^0-9A-Za-z.+-]', '_', version)}.zip"
    partial = target.with_suffix(".zip.partial")
    manifest: dict[str, Any] = {"app": "NextPull", "version": version, "created": (now or datetime.now()).isoformat(timespec="seconds"), "files": {}}
    try:
        with zipfile.ZipFile(partial, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for source in _config_files(data_dir):
                digest = sha256_file(source)
                archive.write(source, source.name)
                manifest["files"][source.name] = {"sha256": digest, "size": source.stat().st_size}
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        check_backup(partial)
        os.replace(partial, target)
    except UpdateFailed:
        remove_tree(partial)
        raise
    except (OSError, zipfile.BadZipFile) as error:
        remove_tree(partial)
        raise UpdateFailed(f"Could not back up your settings: {error}", "backup") from error
    prune_backups(backups_dir)
    return target


def check_backup(path: Path) -> dict[str, Any]:
    """Read the zip back and compare every file with the manifest. Returns the manifest."""
    with zipfile.ZipFile(path) as archive:
        bad = archive.testzip()
        if bad:
            raise UpdateFailed(f"The backup is damaged ({bad})", "backup")
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        for name, info in manifest["files"].items():
            digest = hashlib.sha256(archive.read(name)).hexdigest()
            if digest != info["sha256"]:
                raise UpdateFailed(f"The backup of {name} does not match", "backup")
    return manifest


def restore_config(backup: Path, data_dir: Path, report: Reporter | None = None) -> list[str]:
    """Put back every file whose content differs from the backup (nothing is deleted). Returns the names restored."""
    manifest = check_backup(backup)
    restored = []
    with zipfile.ZipFile(backup) as archive:
        for name, info in manifest["files"].items():
            destination = data_dir / name
            if destination.is_file() and sha256_file(destination) == info["sha256"]:
                continue
            temporary = destination.with_name(destination.name + ".restore.tmp")
            temporary.write_bytes(archive.read(name))
            os.replace(temporary, destination)
            restored.append(name)
            if report:
                report.log(f"Restored {name}")
    return restored


def prune_backups(backups_dir: Path, keep: int = BACKUPS_TO_KEEP) -> None:
    zips = sorted(backups_dir.glob("*.zip"))
    for old in zips[:-keep] if keep else zips:
        try:
            old.unlink()
        except OSError:
            pass


# ----------------------------------------------------------------------------------------------------------- download

class _GuardedRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to addresses the caller trusts (GitHub's release CDN), never to plain http or another host."""

    def __init__(self, allowed: Callable[[str], bool]) -> None:
        self._allowed = allowed

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        if not self._allowed(newurl):
            raise UpdateFailed("The download was redirected to an address that is not GitHub, so it was refused", "download")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_url(url: str, allowed: Callable[[str], bool], timeout: float = 30.0) -> Any:
    if not allowed(url):
        raise UpdateFailed("The download address is not a GitHub address, so it was refused", "download")
    opener = urllib.request.build_opener(_GuardedRedirects(allowed))
    return opener.open(urllib.request.Request(url, headers={"User-Agent": "NextPull-Updater"}), timeout=timeout)


def fetch_expected_hash(request: Request, allowed: Callable[[str], bool]) -> str:
    """The SHA-256 the release promises: from GitHub's own digest, else from the ``.sha256`` asset. Both present must agree."""
    given = request.sha256.strip().lower()
    from_file = ""
    if request.sha256_url:
        try:
            with open_url(request.sha256_url, allowed) as response:
                text = response.read(4096).decode("utf-8", "replace")
            match = re.search(r"\b([0-9a-fA-F]{64})\b", text)
            from_file = match.group(1).lower() if match else ""
        except (urllib.error.URLError, OSError, TimeoutError):
            if not given:
                raise UpdateFailed("Could not read the checksum of the release, so the update was not installed", "verify")
    if given and from_file and given != from_file:
        raise UpdateFailed("The two checksums published for this release disagree, so the update was not installed", "verify")
    found = given or from_file
    if not re.fullmatch(r"[0-9a-f]{64}", found):
        raise UpdateFailed("This release has no checksum, so the update was not installed", "verify")
    return found


def download(request: Request, destination: Path, report: Reporter, allowed: Callable[[str], bool], attempts: int = 3) -> str:
    """Download to ``destination`` (a ``.part`` file first). Returns the SHA-256 of what arrived."""
    part = destination.with_name(destination.name + ".part")
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        digest = hashlib.sha256()
        done, started, window_start, window_bytes, speed = 0, time.monotonic(), time.monotonic(), 0, 0.0
        try:
            with open_url(request.asset_url, allowed) as response, open(part, "wb") as out:
                total = int(response.headers.get("Content-Length") or request.size or 0)
                while True:
                    if report.cancelled():
                        raise Cancelled("Update cancelled", "download")
                    block = response.read(CHUNK)
                    if not block:
                        break
                    out.write(block)
                    digest.update(block)
                    done += len(block)
                    window_bytes += len(block)
                    now = time.monotonic()
                    if now - window_start >= 0.5:
                        speed = window_bytes / (now - window_start) if speed == 0 else 0.6 * speed + 0.4 * (window_bytes / (now - window_start))
                        window_start, window_bytes = now, 0
                    report.progress(done, total, speed)
            if total and done != total:
                raise OSError(f"the download stopped early ({human_bytes(done)} of {human_bytes(total)})")
            report.progress(done, done, speed)
            os.replace(part, destination)
            return digest.hexdigest()
        except Cancelled:
            remove_tree(part)
            raise
        except UpdateFailed:
            remove_tree(part)
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            last = error
            report.log(f"Download attempt {attempt} of {attempts} failed: {error}")
            remove_tree(part)
            if attempt < attempts:
                time.sleep(min(2.0 * attempt, 5.0))
    reason = getattr(last, "reason", last)
    raise UpdateFailed(f"The download failed after {attempts} tries ({reason}). Check your internet connection", "download")


# ------------------------------------------------------------------------------------------------------------- unpack

def _safe_member(name: str) -> bool:
    if not name or name.startswith(("/", "\\")) or ":" in name or "\x00" in name:
        return False
    parts = re.split(r"[\\/]+", name)
    return all(part not in ("..",) for part in parts)


def unpack(archive_path: Path, destination: Path, exe_name: str = EXE_NAME) -> Path:
    """Unpack a release zip into ``destination`` and return the folder that holds the program. Refuses anything unsafe."""
    try:
        archive = zipfile.ZipFile(archive_path)
    except (zipfile.BadZipFile, OSError) as error:
        raise UpdateFailed(f"The downloaded file is not a valid zip ({error})", "verify") from error
    with archive:
        infos = archive.infolist()
        if not infos:
            raise UpdateFailed("The release zip is empty", "verify")
        if sum(item.file_size for item in infos) > MAX_UNPACKED:
            raise UpdateFailed("The release is far larger than NextPull should be, so it was refused", "verify")
        for item in infos:
            mode = (item.external_attr >> 16) & 0o170000
            if not _safe_member(item.filename) or mode == 0o120000:
                raise UpdateFailed(f"The release contains an unsafe path ({item.filename}), so it was refused", "verify")
        bad = archive.testzip()
        if bad:
            raise UpdateFailed(f"The downloaded zip is damaged ({bad})", "verify")
        destination.mkdir(parents=True, exist_ok=False)
        root = destination.resolve()
        for item in infos:
            target = (destination / item.filename).resolve()
            if root != target and root not in target.parents:
                raise UpdateFailed(f"The release contains an unsafe path ({item.filename}), so it was refused", "verify")
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, open(target, "xb") as out:
                shutil.copyfileobj(source, out, CHUNK)
    program = destination
    if not (program / exe_name).is_file():
        children = [entry for entry in destination.iterdir() if entry.is_dir()]
        if len(children) == 1 and (children[0] / exe_name).is_file():
            program = children[0]
    if not (program / exe_name).is_file():
        raise UpdateFailed(f"The release does not contain {exe_name}", "verify")
    return program


def carry_over_tools(old_dir: Path, new_dir: Path, report: Reporter) -> list[str]:
    """Files the user added under ``tools/`` (for example rclone.exe) that the release does not ship are copied across."""
    copied = []
    old_tools = old_dir / "tools"
    if not old_tools.is_dir():
        return copied
    for source in old_tools.rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(old_dir)
        target = new_dir / relative
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(str(relative))
        report.log(f"Kept {relative} from the old install")
    return copied


# ------------------------------------------------------------------------------------------------------- orchestration

class Installer:
    """One update run. ``hooks`` are replaced in tests; the defaults are the real Windows behaviour."""

    def __init__(self, request: Request, report: Reporter | None = None, *, allowed: Callable[[str], bool] = updates.trusted_url,
                 acquire_lock: Callable[[], bool] | None = None, release_lock: Callable[[], None] | None = None,
                 health_check: Callable[[Path, Path], dict[str, Any]] | None = None, launch: Callable[[Path], Any] | None = None,
                 watch_seconds: float = 10.0, lock_seconds: float = 60.0) -> None:
        self.request = request
        self.report = report or NullReporter()
        self.allowed = allowed
        self.app_dir = Path(request.app_dir)
        self.data_dir = Path(request.data_dir)
        self.backups_dir = self.data_dir / "backups"
        self.work_dir = self.data_dir / "update"
        self.acquire_lock = acquire_lock or (lambda: True)
        self.release_lock = release_lock or (lambda: None)
        self.health_check = health_check or self._default_health_check
        self.launch = launch or self._default_launch
        self.watch_seconds = watch_seconds
        self.lock_seconds = lock_seconds
        self.stamp = stamp()
        self.new_dir = self.app_dir.with_name(self.app_dir.name + ".new")
        self.staging_dir = self.app_dir.with_name(self.app_dir.name + ".unpack")
        self.previous_dir = self.app_dir.with_name(self.app_dir.name + ".previous")
        self.backup_path: Path | None = None
        self.swapped = False
        self.launched: Any = None
        self._lock_held = False

    # -- the steps ---------------------------------------------------------------------------------------------
    def run(self) -> Outcome:
        r, rep = self.request, self.report
        outcome = Outcome(ok=False, version=r.version, previous_version=r.current_version)
        step = "wait"
        try:
            step = self._do("wait", self._step_wait)
            step = self._do("backup", self._step_backup)
            step = self._do("download", self._step_download)
            step = self._do("verify", self._step_verify)
            rep.can_cancel(False)
            step = self._do("install", self._step_install)
            step = self._do("restart", self._step_restart)
            outcome.ok = True
            outcome.message = f"NextPull {r.version} is installed."
            outcome.backup = str(self.backup_path or "")
        except UpdateFailed as error:
            outcome.message, outcome.step, outcome.cancelled = str(error), error.step or step, isinstance(error, Cancelled)
            rep.log(f"FAILED in {outcome.step}: {error}")
        except Exception as error:   # a bug in the updater itself: still put the old version back
            outcome.message, outcome.step = f"Unexpected error: {type(error).__name__}: {error}", step
            rep.log(f"CRASH in {step}: {type(error).__name__}: {error}")
        if not outcome.ok:
            rep.can_cancel(False)
            rep.step(outcome.step or step, "skipped" if outcome.cancelled else "error", "Cancelled" if outcome.cancelled else outcome.message)
            outcome = self._recover(outcome)
        outcome.finished = datetime.now().isoformat(timespec="seconds")
        outcome.backup = outcome.backup or str(self.backup_path or "")
        self._write_result(outcome)
        return outcome

    def _do(self, key: str, action: Callable[[], str]) -> str:
        self.report.step(key, "active")
        detail = action()
        self.report.step(key, "done", detail or "")
        return key

    def _step_wait(self) -> str:
        r = self.request
        if not (self.app_dir / r.exe_name).is_file():
            raise UpdateFailed(f"NextPull was not found in {self.app_dir}", "wait")
        probe = self.app_dir.with_name(f".nextpull-write-test-{self.stamp}")
        try:
            probe.mkdir()
            probe.rmdir()
        except OSError as error:
            raise UpdateFailed(f"NextPull cannot update itself here because Windows does not allow changes in {self.app_dir.parent} ({error}). Install it in a folder you own, such as C:\\NextPull", "wait") from error
        deadline = time.monotonic() + self.lock_seconds
        announced = False
        while not self.acquire_lock():
            if self.report.cancelled():
                raise Cancelled("Update cancelled", "wait")
            if time.monotonic() > deadline and not announced:
                announced = True
                self.report.log("NextPull is still running")
                self.report.need_force(True)
            time.sleep(0.4)
        self.report.need_force(False)
        self._lock_held = True
        need = max(r.size * 3, 400 * 1024 * 1024)
        free = shutil.disk_usage(self.app_dir.parent).free
        if free < need:
            raise UpdateFailed(f"Not enough free disk space: {human_bytes(need)} are needed, {human_bytes(free)} are free", "wait")
        for leftover in (self.new_dir, self.staging_dir):
            if leftover.exists():
                retry(lambda leftover=leftover: shutil.rmtree(leftover), leftover.name, self.report)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        return ""

    def _step_backup(self) -> str:
        self.backup_path = backup_config(self.data_dir, self.backups_dir, self.request.current_version)
        self.report.log(f"Settings backed up to {self.backup_path.name}")
        return self.backup_path.name

    def _step_download(self) -> str:
        r = self.request
        self.report.can_cancel(True)
        expected = fetch_expected_hash(r, self.allowed)
        destination = self.work_dir / r.asset_name
        got = download(r, destination, self.report, self.allowed)
        if got != expected:
            remove_tree(destination)
            raise UpdateFailed("The downloaded file does not match its checksum, so it was thrown away. Nothing was changed", "verify")
        self.report.log(f"Checksum ok ({got[:12]}...)")
        self._zip = destination
        return human_bytes(destination.stat().st_size)

    def _step_verify(self) -> str:
        self.report.progress(0, 0, 0)
        program = unpack(self._zip, self.staging_dir, self.request.exe_name)
        # the unpacked folder becomes the staged install (same parent as the real one, so the later rename is instant)
        os.replace(program, self.new_dir)
        remove_tree(self.staging_dir)
        carry_over_tools(self.app_dir, self.new_dir, self.report)
        assert self.backup_path is not None
        scratch = Path(tempfile.mkdtemp(prefix="nextpull-check-", dir=self.work_dir))
        try:
            with zipfile.ZipFile(self.backup_path) as archive:
                for name in json.loads(archive.read("manifest.json").decode("utf-8"))["files"]:
                    (scratch / name).write_bytes(archive.read(name))
            result = self.health_check(self.new_dir, scratch)
        finally:
            remove_tree(scratch)
        if not result.get("ok"):
            problems = "; ".join(str(item) for item in result.get("problems", [])) or str(result.get("error") or "it did not start")
            raise UpdateFailed(f"The new version did not pass its start-up check ({problems}). Nothing was changed", "verify")
        if result.get("version") != self.request.version.lstrip("vV"):
            raise UpdateFailed(f"The downloaded program is version {result.get('version')}, not {self.request.version}. Nothing was changed", "verify")
        return f"Version {result['version']} works with your settings"

    def _step_install(self) -> str:
        self.report.log("Swapping the program folder")
        if self.previous_dir.exists():
            retry(lambda: shutil.rmtree(self.previous_dir), self.previous_dir.name, self.report)
        retry(lambda: os.replace(self.app_dir, self.previous_dir), "rename the old version", self.report)
        try:
            os.replace(self.new_dir, self.app_dir)
        except OSError as error:
            os.replace(self.previous_dir, self.app_dir)
            raise UpdateFailed(f"Could not move the new version into place: {error}", "install") from error
        self.swapped = True
        return ""

    def _step_restart(self) -> str:
        self.release_lock()
        self._lock_held = False
        exe = self.app_dir / self.request.exe_name
        process = self.launch(exe)
        self.launched = process
        deadline = time.monotonic() + self.watch_seconds
        while time.monotonic() < deadline:
            code = process.poll() if process is not None else None
            if code not in (None, 0):
                raise UpdateFailed(f"The new version stopped right after starting (exit code {code})", "restart")
            time.sleep(0.25)
        return ""

    # -- failure ------------------------------------------------------------------------------------------------
    def _recover(self, outcome: Outcome) -> Outcome:
        """Put the old version and the old settings back, then start the old version (unless it never stopped)."""
        rep, r = self.report, self.request
        touched = self.swapped or self.launched is not None or (self.previous_dir.is_dir() and not self.app_dir.exists())
        app_was_stopped = self._lock_held or self.launched is not None
        try:
            if touched:
                rep.log("Restoring the previous version")
                if self.launched is not None:
                    self._stop_process(self.launched)
                if not self._lock_held:   # the restart step released it; take it again so nothing else starts meanwhile
                    self._lock_held = self._reacquire()
                if self.previous_dir.is_dir() and (self.swapped or not self.app_dir.exists()):
                    failed = self.app_dir.with_name(self.app_dir.name + ".failed")
                    if failed.exists():
                        remove_tree(failed, rep)
                    if self.app_dir.exists():
                        retry(lambda: os.replace(self.app_dir, failed), "set the new version aside", rep)
                    retry(lambda: os.replace(self.previous_dir, self.app_dir), "put the old version back", rep)
                    self.swapped = False
                    remove_tree(failed, rep)
                    outcome.rolled_back = True
                if self.backup_path and self.backup_path.is_file():
                    restore_config(self.backup_path, self.data_dir, rep)
        except Exception as error:
            outcome.restore_failed = True
            rep.log(f"RESTORE FAILED: {type(error).__name__}: {error}")
        finally:
            remove_tree(self.new_dir, rep)
            remove_tree(self.staging_dir, rep)
            if self._lock_held:
                self.release_lock()
                self._lock_held = False
        if app_was_stopped and not outcome.restore_failed and (self.app_dir / r.exe_name).is_file():
            try:
                self.launch(self.app_dir / r.exe_name)
                rep.log(f"NextPull {r.current_version} started again")
            except Exception as error:
                rep.log(f"Could not start NextPull again: {error}")
        return outcome

    def _reacquire(self, seconds: float = 20.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.acquire_lock():
                return True
            time.sleep(0.3)
        return False

    @staticmethod
    def _stop_process(process: Any) -> None:
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(8)
                except Exception:
                    process.kill()
        except Exception:
            pass

    def _write_result(self, outcome: Outcome) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            (self.data_dir / RESULT_FILE).write_text(json.dumps(outcome.to_json(), indent=2), encoding="utf-8")
        except OSError as error:
            self.report.log(f"Could not write the result file: {error}")
        if outcome.ok:
            remove_tree(self.work_dir, self.report)

    # -- real implementations -------------------------------------------------------------------------------------
    def _default_health_check(self, program_dir: Path, scratch_data: Path) -> dict[str, Any]:
        out = scratch_data / "self-check.json"
        command = [str(program_dir / self.request.exe_name), "--self-check", str(out), "--data-dir", str(scratch_data)]
        try:
            completed = subprocess.run(command, cwd=program_dir, capture_output=True, timeout=120, creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        except (OSError, subprocess.TimeoutExpired) as error:
            return {"ok": False, "error": f"could not run it ({type(error).__name__})"}
        try:
            result = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"ok": False, "error": f"it exited with code {completed.returncode} without a report"}
        return result if isinstance(result, dict) else {"ok": False, "error": "unreadable report"}

    def _default_launch(self, exe: Path) -> subprocess.Popen:
        return subprocess.Popen([str(exe), *self.request.launch_args], cwd=str(exe.parent), close_fds=True, creationflags=DETACHED if sys.platform == "win32" else 0,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def read_result(data_dir: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads((data_dir / RESULT_FILE).read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except (OSError, ValueError):
        return None


def clear_result(data_dir: Path) -> None:
    try:
        (data_dir / RESULT_FILE).unlink()
    except OSError:
        pass
