"""Shared test setup: import path, the fake rclone command, temp folders and job factories."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

FAKE_RCLONE = [sys.executable, str(ROOT / "tests" / "fake_rclone.py")]

from store import clean_job  # noqa: E402  (after the path setup)


def make_job(**overrides) -> dict:
    """A valid cleaned job; ``overrides`` may use dotted keys for nested dicts via explicit dict values."""
    base = {"name": "Night pull", "source": "EXTRACTED", "dest": "", "mode": "copy", "min_age_minutes": 0, "schedule": {"days": [0, 1, 2, 3, 4, 5, 6], "time": "02:00", "stop_by": ""}}
    base.update(overrides)
    return clean_job(base)


def write_file(path: Path, size: int = 1000, age_seconds: float = 3600, fill: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((fill * size)[:size])
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


class TempCase(unittest.TestCase):
    """Gives every test its own temp folder (``self.tmp``) and cleans up."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="nextpull-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._env = dict(os.environ)
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
