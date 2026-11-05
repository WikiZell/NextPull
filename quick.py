"""Quick downloads: files or folders the user picks on the Files page and wants *now*, outside any schedule.

A quick download is an ordinary job that is built on the spot, queued like every other run (one run at a time) and recorded in
History under the job id ``quick``. Everything the safety rules promise for scheduled jobs therefore applies unchanged: server
pre-check, name-clash guard, verified transfers, ``move`` only deletes after rclone verified the copy, per-file history.

Rules specific to this module:

* A **file** is downloaded by naming the file as the source (rclone copies/moves one object). Several files from the same folder are
  grouped into one run with anchored, escaped include rules. A **folder** goes to ``<destination>/<folder name>``.
* Nothing at the destination is ever lost: ``keep`` (``--ignore-existing``) leaves existing files alone, ``replace`` moves the old
  copy into a dated backup folder (``--backup-dir``) before the new one is written.
* Paths never contain ``..`` (``store.clean_remote_path``), names are never used to build a command line.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from rclone_logic import glob_escape, local_relpath
from store import clean_remote_path, default_job

MAX_ITEMS = 500
ON_CONFLICT = ("keep", "replace")
_BAD_FILE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def clean_items(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("Pick at least one file or folder")
    if len(raw) > MAX_ITEMS:
        raise ValueError(f"Pick at most {MAX_ITEMS} items at once")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("Invalid selection")
        path = clean_remote_path(entry.get("path"))
        if not path:
            raise ValueError("Pick a file or a folder, not the root of the server")
        if path in seen:
            continue
        seen.add(path)
        items.append({"path": path, "is_dir": bool(entry.get("is_dir")), "size": max(0, int(entry.get("size") or 0))})
    return items


def _safe_folder_name(name: str) -> str:
    cleaned = _BAD_FILE_CHARS.sub("_", name).strip(" .")
    return cleaned[:120] or "Download"


def local_name(remote_name: str) -> str:
    """The file name this remote name gets on the PC (same mapping rclone uses for Windows-forbidden characters)."""
    return local_relpath(remote_name)


def find_conflicts(dest: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Files the user picked that already exist in the destination with a different size. Folders are merged and are not listed."""
    found: list[dict[str, Any]] = []
    for item in items:
        if item["is_dir"]:
            continue
        target = Path(dest) / local_name(item["path"].rsplit("/", 1)[-1])
        try:
            if target.is_file() and target.stat().st_size != item["size"]:
                found.append({"path": item["path"], "name": target.name, "local_size": target.stat().st_size, "size": item["size"]})
        except OSError:
            continue
    return found


def _title(names: list[str], what: str) -> str:
    if len(names) == 1:
        return names[0][:60]
    return f"{len(names)} {what}"[:60]


def quick_jobs(connection: str, raw_items: Any, mode: str, dest: str, on_conflict: str = "replace", backup_root: Path | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    """Complete job dicts (ready for ``Engine.fetch_now``) for what the user picked."""
    mode = str(mode or "").lower()
    if mode not in ("copy", "move"):
        raise ValueError("Choose Download (keep a copy on the server) or Move (remove it from the server after it is downloaded)")
    dest = str(dest or "").strip()
    if not dest:
        raise ValueError("Choose the folder on this PC where the files should go")
    if on_conflict not in ON_CONFLICT:
        raise ValueError("Choose what to do with files that already exist")
    items = clean_items(raw_items)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    backup = str(backup_root / stamp) if backup_root and on_conflict == "replace" else ""

    def base(name: str, source: str, target: str, includes: list[str]) -> dict[str, Any]:
        job = default_job() | {"id": "quick", "name": name, "connection": connection or "nextcloud", "source": source, "mode": mode, "dest": target, "includes": includes,
                               "min_age_minutes": 0, "streams": 4, "transfers": 2, "delete_empty_dirs": False, "retries": 3}
        if on_conflict == "keep":
            job["ignore_existing"] = True
        elif backup:
            job["backup_dir"] = backup
        return job

    jobs: list[dict[str, Any]] = []
    folders = [item for item in items if item["is_dir"]]
    files = [item for item in items if not item["is_dir"]]
    for item in folders:
        name = item["path"].rsplit("/", 1)[-1]
        job = base(name, item["path"], str(Path(dest) / _safe_folder_name(name)), [])
        job["delete_empty_dirs"] = mode == "move"   # a moved folder leaves no empty shells behind; the folder itself is never removed (rule 18)
        jobs.append(job)
    by_parent: dict[str, list[dict[str, Any]]] = {}
    for item in files:
        by_parent.setdefault(item["path"].rsplit("/", 1)[0] if "/" in item["path"] else "", []).append(item)
    for parent, group in by_parent.items():
        if len(group) == 1:   # one object: the file itself is the source
            jobs.append(base(group[0]["path"].rsplit("/", 1)[-1], group[0]["path"], dest, []))
        else:                 # several from one folder: that folder, with an anchored literal include per file (and nothing else)
            names = [item["path"].rsplit("/", 1)[-1] for item in group]
            job = base(_title(names, "files"), parent, dest, [glob_escape(name) for name in names])
            jobs.append(job)
    return jobs
