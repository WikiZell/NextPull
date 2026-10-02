"""Windows-only helpers for NextPull (all guarded so the pure modules and tests import on any platform):

* DPAPI encryption for the stored Nextcloud login (``dpapi_protect`` / ``dpapi_unprotect``)
* start with Windows (HKCU Run key) and a "keep running" watchdog (Task Scheduler)
* single-instance mutex plus a named event that lets a second launch wake the running window
* preventing system sleep while a transfer runs
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

IS_WINDOWS = sys.platform == "win32"
APP_NAME = "NextPull"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WATCHDOG_TASK = "NextPull-Watchdog"
MUTEX_NAME = "Local\\NextPull.SingleInstance"
EVENT_NAME = "Local\\NextPull.Show"
ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
CREATE_NO_WINDOW = 0x08000000


def app_dir() -> Path:
    """Folder with the bundled resources (web/, tools/): the PyInstaller bundle when frozen, else the source folder."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def exe_dir() -> Path:
    """Folder that holds NextPull.exe (frozen) or the sources. Used to find a tools/rclone/ next to the program."""
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def launch_command(background: bool = True) -> str:
    """Command line that starts NextPull again (used by auto-start and the watchdog)."""
    flag = " --background" if background else ""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"{flag}'
    interpreter = Path(sys.executable)
    pythonw = interpreter.with_name("pythonw.exe")
    runner = pythonw if pythonw.is_file() else interpreter
    return f'"{runner}" "{Path(__file__).resolve().with_name("nextpull.py")}"{flag}'


# ------------------------------------------------------------------------------------------------------------ DPAPI

if IS_WINDOWS:
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _crypt32.CryptProtectData.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(_Blob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.CreateEventW.restype = wintypes.HANDLE
    _kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.OpenEventW.restype = wintypes.HANDLE
    _kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
    _kernel32.SetThreadExecutionState.restype = wintypes.DWORD


def _blob_in(data: bytes):
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _blob_out(blob) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        _kernel32.LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))


def dpapi_protect(data: bytes) -> bytes:
    """Encrypt for the current Windows user (CryptProtectData, no UI). Only this user on this PC can decrypt."""
    if not IS_WINDOWS:
        raise OSError("DPAPI is only available on Windows")
    blob_in, _keep = _blob_in(data)
    blob_out = _Blob()
    if not _crypt32.CryptProtectData(ctypes.byref(blob_in), APP_NAME, None, None, None, 1, ctypes.byref(blob_out)):
        raise ctypes.WinError(ctypes.get_last_error())
    return _blob_out(blob_out)


def dpapi_unprotect(data: bytes) -> bytes:
    if not IS_WINDOWS:
        raise OSError("DPAPI is only available on Windows")
    blob_in, _keep = _blob_in(data)
    blob_out = _Blob()
    if not _crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 1, ctypes.byref(blob_out)):
        raise ctypes.WinError(ctypes.get_last_error())
    return _blob_out(blob_out)


# ------------------------------------------------------------------------------------------- autostart + watchdog

def autostart_enabled() -> bool:
    if not IS_WINDOWS:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled: bool, command: str | None = None) -> None:
    if not IS_WINDOWS:
        raise OSError("Start with Windows is only available on Windows")
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command or launch_command(True))
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except OSError:
                pass


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0, timeout=30)


def watchdog_enabled() -> bool:
    return IS_WINDOWS and _schtasks("/Query", "/TN", WATCHDOG_TASK).returncode == 0


def set_watchdog(enabled: bool, command: str | None = None) -> None:
    """A Task Scheduler task that starts NextPull every 5 minutes. The single-instance mutex makes every launch a
    no-op while the app is running, so it only matters after a crash or after the app was closed by mistake."""
    if not IS_WINDOWS:
        raise OSError("The watchdog is only available on Windows")
    if enabled:
        result = _schtasks("/Create", "/F", "/TN", WATCHDOG_TASK, "/SC", "MINUTE", "/MO", "5", "/TR", command or launch_command(True))
        if result.returncode != 0:
            raise OSError((result.stderr or result.stdout or "schtasks failed").strip())
    else:
        _schtasks("/Delete", "/F", "/TN", WATCHDOG_TASK)


# ------------------------------------------------------------------------------------------------ single instance

class SingleInstance:
    """``acquire()`` is True for the first process. A later launch calls ``signal_existing()`` to raise the window of the
    running one; the running one calls ``watch(callback)`` once to react."""

    def __init__(self, name: str = MUTEX_NAME, event: str = EVENT_NAME) -> None:
        self.name, self.event_name = name, event
        self._mutex = None
        self._event = None
        self._stop = threading.Event()

    def acquire(self) -> bool:
        if not IS_WINDOWS:
            return True
        self._mutex = _kernel32.CreateMutexW(None, False, self.name)
        return ctypes.get_last_error() != 183   # ERROR_ALREADY_EXISTS: another process holds the mutex

    def signal_existing(self) -> None:
        if not IS_WINDOWS:
            return
        handle = _kernel32.OpenEventW(0x0002, False, self.event_name)
        if handle:
            _kernel32.SetEvent(handle)
            _kernel32.CloseHandle(handle)

    def watch(self, callback: Callable[[], None]) -> None:
        if not IS_WINDOWS:
            return
        self._event = _kernel32.CreateEventW(None, False, False, self.event_name)

        def loop() -> None:
            while not self._stop.is_set():
                if _kernel32.WaitForSingleObject(self._event, 1000) == 0:
                    try:
                        callback()
                    except Exception:
                        pass

        threading.Thread(target=loop, name="nextpull-show-event", daemon=True).start()

    def release(self) -> None:
        self._stop.set()


# ------------------------------------------------------------------------------------------------------ misc

def prevent_sleep(on: bool) -> None:
    """Keep the PC awake (system, not display). Must be called from the thread that does the work."""
    if IS_WINDOWS:
        _kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED if on else ES_CONTINUOUS)


def free_bytes(path: str | Path) -> int | None:
    """Free space of the disk that holds ``path`` (or of its nearest existing parent). None if it cannot be read."""
    candidate = Path(path)
    for _ in range(64):
        if candidate.exists():
            try:
                return shutil.disk_usage(candidate).free
            except OSError:
                return None
        if candidate.parent == candidate:
            return None
        candidate = candidate.parent
    return None


def open_path(path: str | Path) -> None:
    if IS_WINDOWS:
        os.startfile(str(path))   # noqa: S606 - opens a folder in Explorer, user initiated
    else:
        subprocess.Popen(["xdg-open", str(path)])


def reveal_path(path: str | Path) -> None:
    """Show a file selected in Explorer (falls back to opening its folder)."""
    if IS_WINDOWS:
        subprocess.Popen(["explorer", "/select,", str(path)])   # noqa: S603,S607 - fixed program, user initiated
    else:
        open_path(Path(path).parent)
