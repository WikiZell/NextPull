from __future__ import annotations

import os
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import helpers
from helpers import FAKE_RCLONE, TempCase, make_job, write_file
from rclone_runner import (LogTail, RcloneError, RcloneRun, bwlimit_value, build_args, build_env, classify_event, exit_message, find_rclone, obscure_password,
                           parse_log_line, rclone_version)
from store import HistoryDb

PASSWORD = "SuperSecret-Pass-42"
CREDS = {"server": "https://cloud.example.org", "user": "al ice", "app_password": PASSWORD}


class PureHelpers(unittest.TestCase):
    def test_bwlimit_value(self) -> None:
        self.assertEqual(bwlimit_value({"limit_mbps": 0, "timetable": ""}), "")
        self.assertEqual(bwlimit_value({"limit_mbps": 8, "timetable": ""}), "8M")
        self.assertEqual(bwlimit_value({"limit_mbps": 2.5, "timetable": ""}), "2.5M")
        self.assertEqual(bwlimit_value({"limit_mbps": 8, "timetable": "08:00,512k 23:00,off"}), "08:00,512k 23:00,off")

    def test_build_args_have_no_secrets_and_the_right_flags(self) -> None:
        job = make_job(dest="D:\\Films", source="Shared/EXTRACTED", mode="move", min_age_minutes=5, excludes=["*.log"], includes=["*.mkv"], transfers=2, streams=6,
                       speed={"limit_mbps": 8, "timetable": ""}, checksum=True)
        args = build_args(job, config_path=Path("C:/c.conf"), rc_port=5572, rc_pass="rcpass", log_file=Path("C:/run.jsonl"))
        self.assertEqual(args[:3], ["move", "NC:Shared/EXTRACTED", "D:\\Films"])
        text = " ".join(args)
        for flag in ("--delete-empty-src-dirs", "--checksum", "--min-age 5m", "--bwlimit 8M", "--filter - *.log", "--filter + *.mkv", "--filter - **", "--transfers 2", "--multi-thread-streams 6",
                     "--use-json-log", "--rc-addr 127.0.0.1:5572", "--retries 4"):
            self.assertIn(flag, text)
        self.assertNotIn("--dry-run", text)
        self.assertNotIn(PASSWORD, text)

    def test_copy_has_no_delete_flags_and_dry_run_is_passed(self) -> None:
        job = make_job(dest="x", mode="copy", dry_run=True, min_age_minutes=0)
        args = build_args(job, config_path=Path("c"), rc_port=1, rc_pass="p", log_file=Path("l"))
        self.assertNotIn("--delete-empty-src-dirs", args)
        self.assertNotIn("--min-age", args)
        self.assertIn("--dry-run", args)
        self.assertEqual(args[0], "copy")
        self.assertNotIn("--inplace", args)   # hard rule: never write over local data in place

    def test_build_env_defines_the_remote_and_drops_user_rclone_vars(self) -> None:
        env = build_env(CREDS, "OBSCURED", {"PATH": "p", "RCLONE_CONFIG_PASS": "leak", "rclone_x": "y"})
        self.assertEqual(env["RCLONE_CONFIG_NC_TYPE"], "webdav")
        self.assertEqual(env["RCLONE_CONFIG_NC_URL"], "https://cloud.example.org/remote.php/dav/files/al%20ice")
        self.assertEqual((env["RCLONE_CONFIG_NC_VENDOR"], env["RCLONE_CONFIG_NC_USER"], env["RCLONE_CONFIG_NC_PASS"]), ("nextcloud", "al ice", "OBSCURED"))
        self.assertNotIn("RCLONE_CONFIG_PASS", env)
        self.assertNotIn("rclone_x", env)
        self.assertEqual(env["PATH"], "p")

    def test_log_events(self) -> None:
        self.assertIsNone(parse_log_line("plain text"))
        self.assertIsNone(parse_log_line("{broken"))
        self.assertEqual(classify_event({"level": "info", "msg": "Copied (new)", "object": "a/b.mkv"}), ("copied", "a/b.mkv", "Copied (new)"))
        self.assertEqual(classify_event({"level": "info", "msg": "Copied (replaced existing)", "object": "x"})[0], "copied")
        self.assertEqual(classify_event({"level": "info", "msg": "Multi-thread Copied (new)", "object": "big.mkv"}), ("copied", "big.mkv", "Multi-thread Copied (new)"))   # real wording for big files
        self.assertEqual(classify_event({"level": "info", "msg": "Deleted", "object": "a/b.mkv"})[0], "deleted")
        self.assertEqual(classify_event({"level": "notice", "msg": "Skipped copy as --dry-run is set (size 10)", "object": "f"})[0], "dry")
        self.assertEqual(classify_event({"level": "notice", "msg": "Skipped move as --dry-run is set (size 10)", "object": "f"})[0], "dry")   # real rclone wording
        for noise in ("Skipped set directory modification time as --dry-run is set", "Skipped remove directory as --dry-run is set", "Skipped delete as --dry-run is set"):
            self.assertEqual(classify_event({"level": "notice", "msg": noise, "object": "Sub"})[0], "dry-delete", noise)
        self.assertEqual(classify_event({"level": "error", "msg": "Failed to copy: x", "object": "f"})[0], "file_error")
        self.assertEqual(classify_event({"level": "error", "msg": "Attempt 1/3 failed"})[0:2], ("general", ""))
        self.assertIsNone(classify_event({"level": "info", "msg": "Transferred: 5"}))

    def test_log_tail_keeps_partial_lines_until_complete(self) -> None:
        import tempfile
        path = Path(tempfile.mkdtemp()) / "log.jsonl"
        tail = LogTail(path)
        self.assertEqual(tail.read_new(), [])   # file does not exist yet
        path.write_bytes(b'{"msg": "one"}\n{"msg": "tw')
        self.assertEqual([event["msg"] for event in tail.read_new()], ["one"])
        with open(path, "ab") as handle:
            handle.write(b'o"}\n')
        self.assertEqual([event["msg"] for event in tail.read_new()], ["two"])

    def test_exit_messages(self) -> None:
        self.assertIn("not found", exit_message(3))
        self.assertIn("fatal", exit_message(7))
        self.assertIn("42", exit_message(42))


class Discovery(TempCase):
    def test_find_rclone(self) -> None:
        self.assertIsNone(find_rclone(str(self.tmp / "missing.exe")))
        exe = write_file(self.tmp / "tools" / "rclone" / ("rclone.exe" if os.name == "nt" else "rclone"), 10)
        self.assertEqual(find_rclone("", [self.tmp]), [str(exe)])
        self.assertEqual(find_rclone(str(exe)), [str(exe)])

    def test_version_and_obscure_with_the_fake(self) -> None:
        self.assertIn("rclone v1.63.1", rclone_version(FAKE_RCLONE))
        obscured = obscure_password(FAKE_RCLONE, PASSWORD)
        self.assertTrue(obscured.startswith("obscured:"))
        with self.assertRaises(RcloneError):
            rclone_version([str(self.tmp / "nope.exe")])


class RunsAgainstFakeRclone(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.remote_root = self.tmp / "remote"
        self.src = self.remote_root / "EXTRACTED"
        self.dest = self.tmp / "dest"
        os.environ["FAKE_REMOTE_ROOT"] = str(self.remote_root)
        self.history = HistoryDb(None)
        self.addCleanup(self.history.close)

    def make_run(self, deadline: datetime | None = None, **job_overrides) -> RcloneRun:
        job = make_job(dest=str(self.dest), **job_overrides)
        run_id = self.history.start_run(job, "manual")
        return RcloneRun(rclone=FAKE_RCLONE, job=job, creds=CREDS, run_id=run_id, history=self.history, log_path=self.tmp / "logs" / f"run-{run_id}.jsonl",
                         config_path=self.tmp / "rclone.empty.conf", deadline=deadline, keep_awake=False, precheck=False)

    def test_copy_transfers_files_keeps_the_source_and_records_everything(self) -> None:
        a = write_file(self.src / "a.mkv", 5000, age_seconds=7200)
        write_file(self.src / "Sub" / "b.mkv", 3000)
        result = self.make_run().run()
        self.assertEqual((result["state"], result["files"], result["bytes"], result["errors"]), ("ok", 2, 8000, 0))
        self.assertEqual((self.dest / "a.mkv").stat().st_size, 5000)
        self.assertEqual((self.dest / "Sub" / "b.mkv").stat().st_size, 3000)
        self.assertAlmostEqual((self.dest / "a.mkv").stat().st_mtime, a.stat().st_mtime, delta=2)   # original timestamp kept
        self.assertTrue(a.exists(), "copy must leave the Nextcloud source alone")
        run = self.history.get_run(result["run_id"])
        self.assertEqual((run["state"], run["files"], run["deleted"]), ("ok", 2, 0))
        self.assertEqual(sorted(f["name"] for f in self.history.list_files(result["run_id"])), ["Sub/b.mkv", "a.mkv"])
        self.assertEqual(list(self.dest.rglob("*.partial")), [])

    def test_move_deletes_sources_after_copy_and_removes_empty_folders(self) -> None:
        write_file(self.src / "Sub" / "b.mkv", 3000)
        write_file(self.src / "a.mkv", 100)
        result = self.make_run(mode="move").run()
        self.assertEqual(result["state"], "ok")
        self.assertFalse((self.src / "a.mkv").exists())
        self.assertFalse((self.src / "Sub").exists())
        run = self.history.get_run(result["run_id"])
        self.assertEqual((run["files"], run["deleted"]), (2, 2))
        self.assertEqual((self.dest / "a.mkv").stat().st_size, 100)

    def test_move_keeps_empty_folders_when_asked_to(self) -> None:
        write_file(self.src / "Sub" / "b.mkv", 30)
        self.make_run(mode="move", delete_empty_dirs=False).run()
        self.assertTrue((self.src / "Sub").is_dir())

    def test_min_age_skips_files_that_are_still_being_written(self) -> None:
        write_file(self.src / "old.mkv", 100, age_seconds=3600)
        write_file(self.src / "new.mkv", 100, age_seconds=10)
        result = self.make_run(min_age_minutes=5).run()
        self.assertEqual(result["files"], 1)
        self.assertTrue((self.dest / "old.mkv").exists())
        self.assertFalse((self.dest / "new.mkv").exists())

    def test_excludes_and_includes(self) -> None:
        for name in ("a.mkv", "b.log", "c.srt"):
            write_file(self.src / name, 10)
        self.make_run(excludes=["*.log"]).run()
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), ["a.mkv", "c.srt"])
        for item in self.dest.iterdir():
            item.unlink()
        self.make_run(includes=["*.mkv"]).run()
        self.assertEqual([p.name for p in self.dest.iterdir()], ["a.mkv"])

    def test_nothing_to_do_is_a_clean_ok(self) -> None:
        self.src.mkdir(parents=True)
        result = self.make_run().run()
        self.assertEqual((result["state"], result["files"]), ("ok", 0))
        self.assertIn("Nothing new", result["message"])

    def test_dry_run_copies_nothing(self) -> None:
        write_file(self.src / "a.mkv", 100)
        result = self.make_run(dry_run=True).run()
        self.assertEqual(result["state"], "ok")
        self.assertEqual(list(self.dest.glob("*")), [])
        self.assertEqual([(f["status"], f["size"]) for f in self.history.list_files(result["run_id"])], [("dry", 100)])

    def test_dry_run_of_a_move_job_lists_the_files_and_keeps_them(self) -> None:
        write_file(self.src / "a.mkv", 100)
        write_file(self.src / "Sub" / "b.mkv", 50)
        result = self.make_run(mode="move", dry_run=True).run()
        self.assertEqual((result["state"], result["message"]), ("ok", "2 file(s) checked (dry-run)"))
        self.assertTrue((self.src / "a.mkv").exists() and (self.src / "Sub" / "b.mkv").exists())
        self.assertEqual(sorted((f["name"], f["size"]) for f in self.history.list_files(result["run_id"])), [("Sub/b.mkv", 50), ("a.mkv", 100)])

    def test_one_failing_file_makes_the_run_partial_and_is_recorded(self) -> None:
        write_file(self.src / "good.mkv", 100)
        write_file(self.src / "bad.mkv", 100)
        os.environ["FAKE_FAIL_FILES"] = "bad.mkv"
        result = self.make_run().run()
        self.assertEqual((result["state"], result["files"]), ("partial", 1))
        self.assertGreaterEqual(result["errors"], 1)
        statuses = {f["name"]: f["status"] for f in self.history.list_files(result["run_id"])}
        self.assertEqual(statuses, {"good.mkv": "ok", "bad.mkv": "error"})
        self.assertIn("simulated failure", result["message"])

    def test_all_files_failing_is_failed(self) -> None:
        write_file(self.src / "bad.mkv", 100)
        os.environ["FAKE_FAIL_FILES"] = "bad.mkv"
        self.assertEqual(self.make_run().run()["state"], "failed")

    def test_fatal_error_is_failed_with_the_reason(self) -> None:
        write_file(self.src / "a.mkv", 100)
        os.environ["FAKE_FATAL"] = "1"
        result = self.make_run().run()
        self.assertEqual(result["state"], "failed")
        self.assertIn("401", result["message"])

    def test_missing_remote_folder(self) -> None:
        result = self.make_run(source="DoesNotExist").run()
        self.assertEqual(result["state"], "failed")
        self.assertIn("not found", result["message"])

    def test_retry_noise_does_not_turn_a_good_run_into_a_failure(self) -> None:
        write_file(self.src / "a.mkv", 100)
        os.environ["FAKE_RETRY_NOISE"] = "1"
        self.assertEqual(self.make_run().run()["state"], "ok")

    def test_unusable_destination_fails_cleanly(self) -> None:
        blocker = write_file(self.tmp / "iamafile", 1)
        write_file(self.src / "a.mkv", 10)
        job_dest = str(blocker / "sub")
        job = make_job(dest=job_dest)
        run_id = self.history.start_run(job, "manual")
        run = RcloneRun(rclone=FAKE_RCLONE, job=job, creds=CREDS, run_id=run_id, history=self.history, log_path=self.tmp / "l.jsonl", config_path=self.tmp / "c.conf", keep_awake=False, precheck=False)
        result = run.run()
        self.assertEqual(result["state"], "failed")
        self.assertIn("destination", result["message"])

    def test_missing_rclone_binary_fails_cleanly(self) -> None:
        job = make_job(dest=str(self.dest))
        run_id = self.history.start_run(job, "manual")
        run = RcloneRun(rclone=[str(self.tmp / "nope.exe")], job=job, creds=CREDS, run_id=run_id, history=self.history, log_path=self.tmp / "l.jsonl", config_path=self.tmp / "c.conf", keep_awake=False, precheck=False)
        self.assertEqual(run.run()["state"], "failed")

    def test_password_never_reaches_the_log_or_history(self) -> None:
        write_file(self.src / "a.mkv", 100)
        result = self.make_run().run()
        log = (self.tmp / "logs" / f"run-{result['run_id']}.jsonl").read_text(encoding="utf-8")
        self.assertTrue(log.strip())
        self.assertNotIn(PASSWORD, log)
        self.assertNotIn(PASSWORD, repr(self.history.get_run(result["run_id"])))
        self.assertEqual((self.tmp / "rclone.empty.conf").read_text(), "")   # the config file stays empty: no secrets there either

    def _start_slow(self, **kwargs) -> tuple[RcloneRun, dict]:
        os.environ["FAKE_SPEED"] = "150000"   # ~150 KB/s: a 3 MB file takes about 20 s
        write_file(self.src / "big.mkv", 3_000_000)
        run = self.make_run(**kwargs)
        box: dict = {}
        thread = threading.Thread(target=lambda: box.update(run.run()), daemon=True)
        thread.start()
        box["thread"] = thread
        return run, box

    def _wait_transferring(self, run: RcloneRun) -> dict:
        for _ in range(100):
            snap = run.snapshot()
            if snap["transferring"] and snap["bytes"] > 0:
                return snap
            time.sleep(0.2)
        self.fail(f"never saw an active transfer: {run.snapshot()}")

    def test_live_progress_then_cancel_removes_partial_files(self) -> None:
        run, box = self._start_slow()
        snap = self._wait_transferring(run)
        self.assertEqual(snap["state"], "running")
        self.assertEqual(snap["transferring"][0]["name"], "big.mkv")
        self.assertEqual(snap["transferring"][0]["size"], 3_000_000)
        self.assertGreater(snap["speed"], 0)
        run.cancel()
        box["thread"].join(timeout=30)
        self.assertEqual(box["state"], "cancelled")
        self.assertEqual(list(self.dest.rglob("*")), [])   # no .partial and no half file left behind
        self.assertTrue((self.src / "big.mkv").exists())

    def test_stop_by_deadline_ends_the_run(self) -> None:
        run, box = self._start_slow(deadline=datetime.now() + timedelta(seconds=2))
        box["thread"].join(timeout=30)
        self.assertEqual(box["state"], "stopped")
        self.assertIn("stop-by", box["message"])

    def test_bandwidth_can_be_changed_while_running(self) -> None:
        run, box = self._start_slow()
        self._wait_transferring(run)
        run.set_bandwidth("1M")
        self.assertEqual(run.snapshot()["bwlimit"], "1M")
        run.cancel()
        box["thread"].join(timeout=30)

    def test_bandwidth_before_start_is_an_error(self) -> None:
        with self.assertRaises(RcloneError):
            self.make_run().set_bandwidth("1M")


if __name__ == "__main__":
    unittest.main()
