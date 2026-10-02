"""Pure helpers for reading what rclone says and for protecting files from rclone's blind spots. No I/O, no subprocesses.

Everything here was derived from the REAL rclone's behaviour (see tools/rclone_probe.py and docs/RCLONE.md):

* object names in errors can be temp names (``movie.mkv.d0ee9403.partial``) -> :func:`clean_object`;
* on Windows rclone stores ``a:b?.txt`` as ``a：b？.txt`` (full-width look-alikes, ``␠`` for a trailing space, ``．`` for a trailing dot),
  so the local file name differs from the remote one -> :func:`local_relpath`;
* Windows is case-insensitive: remote ``Movie.mkv`` and ``movie.mkv`` land on ONE local file, and a *move* then deletes both on
  Nextcloud -> :func:`find_collisions` finds such groups so the run can leave them alone;
* real rclone words a move dry run ``Skipped move as --dry-run is set (size N)`` -> :func:`classify_event`;
* error lines mix per-file errors with retry noise (``Attempt 2/4 failed``), delete failures and directory bookkeeping
  -> :func:`classify_event` and :func:`humanize_error` turn them into something a person can act on.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

# Per-request retries inside one attempt. Measured with the real rclone: a persistent HTTP 500 stalls one attempt for 189 s at 10, 41 s at 5 and
# 7 s at 3; but a connection cut every 500 KB needs one resume per cut, so 3 fails a 2 MB file. 6 survives realistic drops and bounds an outage.
LOW_LEVEL_RETRIES = 6
COPIED = re.compile(r"^(?:Multi-thread )?Copied(?: |$)")   # "Copied (new)", "Copied (replaced existing)", "Multi-thread Copied (new)"
PARTIAL_SUFFIX = re.compile(r"\.[0-9a-f]{8}\.partial$")
GLOB_SPECIAL = re.compile(r"([\\*?\[\]{}])")
_WIN_INVALID = {ord(c): chr(ord(c) + 0xFEE0) for c in '<>:"|?*\\'}


# ------------------------------------------------------------------------------------------------------- names

def clean_object(name: str) -> str:
    """``movie.mkv.d0ee9403.partial`` (rclone's temp name in some errors) -> ``movie.mkv``."""
    return PARTIAL_SUFFIX.sub("", name)


def is_file_object(name: str) -> bool:
    """False for rclone's pseudo-objects such as ``webdav root 'Src'``; True for a path."""
    return bool(name) and not name.startswith("webdav root ")


def local_component(name: str) -> str:
    """How rclone's local backend stores one path component on Windows."""
    out = name.translate(_WIN_INVALID)
    out = "".join(chr(0x2400 + ord(c)) if ord(c) < 32 else c for c in out)
    if out.endswith(" "):
        out = out[:-1] + "␠"
    elif out.endswith("."):
        out = out[:-1] + "．"
    return out


def local_relpath(remote_relpath: str) -> str:
    return "/".join(local_component(part) for part in remote_relpath.split("/"))


def collision_key(remote_relpath: str) -> str:
    """Two remote paths with the same key end up on the same local file on a case-insensitive filesystem."""
    return local_relpath(remote_relpath).casefold()


def find_collisions(paths: Iterable[str]) -> list[list[str]]:
    groups: dict[str, list[str]] = {}
    for path in paths:
        groups.setdefault(collision_key(path), []).append(path)
    return [sorted(group) for group in groups.values() if len(group) > 1]


def glob_escape(path: str) -> str:
    """Make a literal path safe for rclone's --exclude (anchored at the source root)."""
    return "/" + GLOB_SPECIAL.sub(r"\\\1", path.lstrip("/"))


# ------------------------------------------------------------------------------------------------- log events

def classify_event(event: dict[str, Any]) -> tuple[str, str, str] | None:
    """``(kind, object, text)`` for the log lines that matter, else None. Kinds:

    ``copied``        file transferred                      ``deleted``       source removed (move)
    ``dry``           dry-run file (would transfer)         ``dry-delete``    dry-run bookkeeping (ignored)
    ``file_error``    a transfer of one file failed          ``delete_error``  downloaded but the delete on Nextcloud failed
    ``general``       an error not tied to a file (incl. "Attempt N/M failed" retry noise and the dir-delete guard)
    """
    msg, level, obj = str(event.get("msg", "")).strip(), str(event.get("level", "")).lower(), str(event.get("object", "") or "")
    obj = clean_object(obj)
    if COPIED.match(msg):   # also "Multi-thread Copied (new)": rclone words big (multi-stream) downloads that way
        return "copied", obj, msg
    if msg == "Deleted" and obj:
        return "deleted", obj, msg
    if "--dry-run" in msg and obj:
        return ("dry" if msg.lower().startswith(("skipped copy", "skipped move", "not copying", "not moving")) else "dry-delete"), obj, msg
    if level in ("error", "critical") and msg:
        if msg.startswith("Couldn't delete") and is_file_object(obj):
            return "delete_error", obj, msg
        if msg.startswith(("Attempt ", "not deleting directories")) or not is_file_object(obj):
            return "general", obj, msg
        return "file_error", obj, msg
    return None


HTTP_REASONS = {"401": "Nextcloud rejected the login (HTTP 401). Disconnect and connect again", "403": "Nextcloud refused access (HTTP 403 Forbidden)",
                "404": "not found on Nextcloud (HTTP 404)", "423": "locked on Nextcloud (HTTP 423)", "429": "Nextcloud is rate limiting requests (HTTP 429)",
                "500": "the Nextcloud server had an internal error (HTTP 500)", "502": "the proxy in front of Nextcloud answered 502 Bad Gateway",
                "503": "Nextcloud is unavailable or in maintenance (HTTP 503)", "504": "gateway timeout in front of Nextcloud (HTTP 504)"}


def humanize_error(text: str) -> str:
    """Shorten rclone's wording to what a person can act on; unknown messages are returned trimmed, never lost."""
    t = " ".join(text.split())
    t = re.sub(r'^Failed to create file system for "[^"]*": (read metadata failed: )?', "", t)
    t = re.sub(r"^(Failed to copy|Failed to move|Failed to sync|error reading source root directory): ", "", t)
    lowered = t.lower()
    if "--bwlimit" in lowered:
        return "the speed limit/timetable is not valid for rclone"
    if "directory not found" in lowered or "object not found" in lowered:
        return "the Nextcloud folder was not found"
    if re.search(r"no such host|dial tcp .*(refused|unreachable|timed out)|i/o timeout|connection reset|connectex|network is unreachable|eof$|tls handshake timeout", lowered):
        return "cannot reach the Nextcloud server (network or server down)"
    if "x509" in lowered or "certificate" in lowered:
        return "the server's HTTPS certificate was rejected"
    if "no space left" in lowered or "not enough space" in lowered or "disk full" in lowered:
        return "the destination disk is full"
    if "access is denied" in lowered or "permission denied" in lowered:
        return "Windows denied access to the destination folder"
    if "corrupted on transfer" in lowered:
        return "the downloaded file did not match the checksum from Nextcloud (corrupted in transfer)"
    if "not deleting directories" in lowered:
        return "some files could not be deleted on Nextcloud, so their folders were kept"
    match = re.search(r"\b(40[1349]|42[39]|50[0234])\b\s+(?:[A-Za-z]+(?:\s+[A-Za-z]+)*)", t)
    if match and match.group(1) in HTTP_REASONS:
        base = HTTP_REASONS[match.group(1)]
        return (base[0].lower() + base[1:]) if not base.startswith("Nextcloud") else base
    return t[:240]


def exit_message(code: int | None) -> str:
    return {
        0: "Finished", 1: "Finished with errors", 2: "rclone was started with invalid options", 3: "The Nextcloud folder was not found", 4: "The Nextcloud folder was not found",
        5: "Too many temporary errors (network or server?)", 6: "Finished with less serious errors", 7: "rclone stopped with a fatal error", 8: "Transfer limit reached", 9: "No files were transferred",
    }.get(code if code is not None else -1, f"rclone ended unexpectedly (exit code {code}), it may have been killed")


# ------------------------------------------------------------------------------------------- bandwidth text

_SIZE = r"(?:off|\d+(?:\.\d+)?[bBkKmMgGtTpP]?(?:i?[bB]?))"
_LIMIT = rf"{_SIZE}(?::{_SIZE})?"
_ENTRY = rf"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:-(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun))?-)?\d{{1,2}}:\d{{2}},{_LIMIT}"
_TIMETABLE = re.compile(rf"^(?:{_LIMIT}|{_ENTRY}(?:\s+{_ENTRY})*)$", re.IGNORECASE)


def valid_bwlimit(text: str) -> bool:
    """rclone ``--bwlimit`` syntax: ``8M``, ``off``, ``1M:2M`` or a timetable ``08:00,2M 23:00,off`` (optionally with weekdays)."""
    return bool(_TIMETABLE.match(text.strip()))
