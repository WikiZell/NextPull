"""Integration tests with the REAL rclone binary (tools/rclone/rclone.exe or PATH) talking WebDAV to tests/fake_nextcloud.py.
Skipped when rclone is not available (fetch it with tools/Fetch-rclone.ps1)."""
from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timedelta

import helpers
from helpers import ROOT, TempCase, make_job, write_file
from fake_nextcloud import FakeNextcloud
from rclone_runner import RcloneRun, find_rclone, obscure_password, rclone_version
from store import HistoryDb

RCLONE = find_rclone("", [ROOT])


@unittest.skipUnless(RCLONE, "real rclone not available (run tools/Fetch-rclone.ps1)")
class RealRclone(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.remote = self.tmp / "remote"
        self.src = self.remote / "EXTRACTED"
        self.dest = self.tmp / "dest"
        self.server = FakeNextcloud(self.remote, user="al ice", password="app-pass-1234")
        self.base = self.server.start()
        self.addCleanup(self.server.stop)
        self.creds = {"server": self.base, "user": "al ice", "app_password": "app-pass-1234"}
        self.history = HistoryDb(None)
        self.addCleanup(self.history.close)

    def make_run(self, deadline: datetime | None = None, creds: dict | None = None, **job_overrides) -> RcloneRun:
        job = make_job(dest=str(self.dest), **job_overrides)
        run_id = self.history.start_run(job, "manual")
        return RcloneRun(rclone=RCLONE, job=job, creds=creds or self.creds, run_id=run_id, history=self.history, log_path=self.tmp / "logs" / f"run-{run_id}.jsonl",
                         config_path=self.tmp / "rclone.empty.conf", deadline=deadline, keep_awake=False)

    def test_binary_and_obscure(self) -> None:
        self.assertTrue(rclone_version(RCLONE).startswith("rclone v"))
        obscured = obscure_password(RCLONE, "app-pass-1234")
        self.assertNotIn("app-pass-1234", obscured)
        self.assertTrue(obscured)

    def test_copy_over_real_webdav_keeps_source_and_timestamps(self) -> None:
        a = write_file(self.src / "a.mkv", 5000, age_seconds=7200, fill=b"a")
        write_file(self.src / "Sub folder" / "Café & b.mkv", 3000, fill=b"b")
        result = self.make_run().run()
        self.assertEqual((result["state"], result["files"], result["bytes"]), ("ok", 2, 8000), result)
        self.assertEqual((self.dest / "a.mkv").read_bytes(), b"a" * 5000)
        self.assertEqual((self.dest / "Sub folder" / "Café & b.mkv").stat().st_size, 3000)
        self.assertAlmostEqual((self.dest / "a.mkv").stat().st_mtime, a.stat().st_mtime, delta=3)
        self.assertTrue(a.exists())
        self.assertEqual(list(self.dest.rglob("*.partial")), [])
        self.assertEqual(sorted(f["name"] for f in self.history.list_files(result["run_id"])), ["Sub folder/Café & b.mkv", "a.mkv"])
        self.assertNotIn("DELETE", {method for method, _ in self.server.requests})

    def test_move_deletes_after_the_copy_and_clears_empty_folders(self) -> None:
        write_file(self.src / "Sub" / "b.mkv", 3000)
        write_file(self.src / "a.mkv", 100)
        result = self.make_run(mode="move").run()
        self.assertEqual(result["state"], "ok", result)
        self.assertFalse((self.src / "a.mkv").exists())
        self.assertFalse((self.src / "Sub").exists())
        run = self.history.get_run(result["run_id"])
        self.assertEqual((run["files"], run["deleted"]), (2, 2))
        self.assertTrue((self.dest / "Sub" / "b.mkv").exists())

    def test_a_403_on_delete_is_reported_and_the_file_is_still_downloaded(self) -> None:
        write_file(self.src / "good.mkv", 100)
        write_file(self.src / "locked.log", 100)
        self.server.forbid_delete = {"locked.log"}
        result = self.make_run(mode="move").run()
        self.assertIn(result["state"], ("partial", "failed"), result)
        self.assertTrue((self.dest / "locked.log").exists())   # downloaded...
        self.assertTrue((self.src / "locked.log").exists())    # ...but not deletable: the exact Nextcloud 403 case
        self.assertFalse((self.src / "good.mkv").exists())

    def test_dry_run_changes_nothing(self) -> None:
        write_file(self.src / "a.mkv", 100)
        result = self.make_run(mode="move", dry_run=True).run()
        self.assertEqual(result["state"], "ok", result)
        self.assertEqual(list(self.dest.glob("*")), [])
        self.assertTrue((self.src / "a.mkv").exists())
        self.assertNotIn("DELETE", {method for method, _ in self.server.requests})
        # the history must say what WOULD have happened (real rclone words a move dry run "Skipped move as --dry-run is set (size N)")
        self.assertEqual([(f["name"], f["status"], f["size"]) for f in self.history.list_files(result["run_id"])], [("a.mkv", "dry", 100)])
        self.assertEqual(result["message"], "1 file(s) checked (dry-run)")

    def test_min_age_excludes_and_includes(self) -> None:
        write_file(self.src / "old.mkv", 100, age_seconds=3600)
        write_file(self.src / "new.mkv", 100, age_seconds=10)
        write_file(self.src / "old.log", 100, age_seconds=3600)
        self.make_run(min_age_minutes=5, excludes=["*.log"]).run()
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), ["old.mkv"])

    def test_wrong_password_fails_with_a_reason(self) -> None:
        write_file(self.src / "a.mkv", 100)
        result = self.make_run(creds={**self.creds, "app_password": "wrong"}).run()
        self.assertEqual(result["state"], "failed", result)
        self.assertTrue(result["message"])
        self.assertEqual(list(self.dest.glob("*")), [])

    def test_missing_remote_folder_is_failed(self) -> None:
        self.remote.mkdir(parents=True)
        self.assertEqual(self.make_run(source="Nope").run()["state"], "failed")

    def test_password_not_in_the_real_log(self) -> None:
        write_file(self.src / "a.mkv", 100)
        result = self.make_run().run()
        log = (self.tmp / "logs" / f"run-{result['run_id']}.jsonl").read_text(encoding="utf-8")
        self.assertTrue(log.strip())
        self.assertNotIn("app-pass-1234", log)

    def test_live_progress_cancel_and_live_speed_change(self) -> None:
        write_file(self.src / "big.mkv", 6_000_000)
        run = self.make_run(speed={"limit_mbps": 0.25, "timetable": ""}, streams=1)
        box: dict = {}
        thread = threading.Thread(target=lambda: box.update(run.run()), daemon=True)
        thread.start()
        snap = {}
        for _ in range(150):
            snap = run.snapshot()
            if snap["transferring"] and snap["bytes"] > 0:
                break
            time.sleep(0.2)
        self.assertTrue(snap["transferring"], snap)
        self.assertEqual(snap["transferring"][0]["name"], "big.mkv")
        run.set_bandwidth("0.5M")
        self.assertEqual(run.snapshot()["bwlimit"], "0.5M")
        run.cancel()
        thread.join(timeout=40)
        self.assertEqual(box.get("state"), "cancelled", box)
        self.assertEqual(list(self.dest.rglob("*")), [])   # no .partial left

    def test_stop_by_deadline(self) -> None:
        write_file(self.src / "big.mkv", 6_000_000)
        run = self.make_run(deadline=datetime.now() + timedelta(seconds=3), speed={"limit_mbps": 0.25, "timetable": ""}, streams=1)
        box: dict = {}
        thread = threading.Thread(target=lambda: box.update(run.run()), daemon=True)
        thread.start()
        thread.join(timeout=40)
        self.assertEqual(box.get("state"), "stopped", box)


if __name__ == "__main__":
    unittest.main()
