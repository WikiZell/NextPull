from __future__ import annotations

import json
import sys
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import applog
import diagnostics
import helpers
from helpers import FAKE_RCLONE, TempCase, make_job
from nextpull import MAX_UI_ERRORS, NextPullApi

SECRET = "s3cr3t-App-Pass-42"


class Redaction(unittest.TestCase):
    def test_shapes_are_hidden(self) -> None:
        cases = ["password=hunter22", 'password: "hunter22"', "Authorization: Basic dXNlcjpodW50ZXIyMg==", "Bearer abcdef0123456789",
                 "rclone --rc-pass hunter22 --rc-user x", "RCLONE_CONFIG_NC_PASS=hunter22", "https://bob:hunter22@cloud.example/remote.php"]
        for text in cases:
            self.assertNotIn("hunter22", applog.redact(text), text)
            self.assertNotIn("dXNlcjpodW50ZXIyMg", applog.redact(text), text)
            self.assertNotIn("abcdef0123456789", applog.redact(text), text)
        self.assertIn("cloud.example", applog.redact(cases[-1]))

    def test_registered_secret_is_hidden_anywhere(self) -> None:
        applog.register_secret(SECRET)
        self.assertEqual(applog.redact(f"weird {SECRET} spot"), "weird <hidden> spot")

    def test_plain_text_is_untouched(self) -> None:
        text = "Copied 3 files from /Anime/EXTRACTED (12 MB)"
        self.assertEqual(applog.redact(text), text)


class LogFile(TempCase):
    def setUp(self) -> None:
        super().setUp()
        applog.setup_logging(self.tmp)
        self.addCleanup(applog.teardown_logging)
        applog.register_secret(SECRET)

    def test_secret_never_reaches_disk_even_in_a_traceback(self) -> None:
        log = applog.get_logger("app")
        log.info("login with %s", SECRET)
        try:
            raise RuntimeError(f"boom {SECRET}")
        except RuntimeError:
            log.exception("it broke")
        for handler in applog._root().handlers:
            handler.flush()
        text = (self.tmp / "app.log").read_text(encoding="utf-8")
        self.assertNotIn(SECRET, text)
        self.assertIn("RuntimeError", text)
        self.assertIn("<hidden>", text)

    def test_read_log_filters_and_keeps_tracebacks_together(self) -> None:
        log = applog.get_logger("engine")
        log.info("fine")
        log.warning("careful with Photos")
        try:
            1 / 0
        except ZeroDivisionError:
            log.exception("bad thing")
        warnings = applog.read_log(self.tmp, level="WARNING")
        self.assertEqual([e["level"] for e in warnings], ["WARNING", "ERROR"])
        self.assertIn("ZeroDivisionError", warnings[1]["message"])
        self.assertEqual(len(applog.read_log(self.tmp, query="photos")), 1)

    def test_without_data_dir_the_ring_serves_the_log(self) -> None:
        applog.setup_logging(None)
        applog.get_logger("app").warning("in memory")
        self.assertEqual(applog.read_log(None, level="WARNING")[-1]["message"], "in memory")

    def test_thread_exceptions_are_logged(self) -> None:
        applog.install_excepthooks()
        with mock.patch("sys.stderr"):
            thread = threading.Thread(target=lambda: 1 / 0, name="crashy")
            thread.start()
            thread.join()
        entries = applog.read_log(self.tmp, level="CRITICAL")
        self.assertTrue(any("crashy" in e["message"] and "ZeroDivisionError" in e["message"] for e in entries))


class Grouping(unittest.TestCase):
    def test_hints_cover_the_common_failures(self) -> None:
        self.assertIn("Reconnect", diagnostics.hint_for("Not connected to Nextcloud"))
        self.assertIn("full", diagnostics.hint_for("write: There is not enough space on the disk"))
        self.assertIn("reached", diagnostics.hint_for("dial tcp: lookup cloud: no such host"))
        self.assertIn("History", diagnostics.hint_for("something never seen before"))

    def test_runs_are_grouped_by_job_and_reason(self) -> None:
        runs = [{"id": i, "job_name": "Anime", "state": "failed", "message": f"timeout after {i} s", "finished": f"2026-10-0{i}T03:00:00"} for i in (1, 2, 3)]
        runs += [{"id": 9, "job_name": "Anime", "state": "ok", "message": "", "finished": "2026-10-04T03:00:00"},
                 {"id": 10, "job_name": "Movies", "state": "partial", "message": "2 files not deleted", "finished": "2026-10-05T03:00:00"}]
        groups = diagnostics.group_problems(runs)
        self.assertEqual([g["job"] for g in groups], ["Movies", "Anime"])
        self.assertEqual(groups[1]["count"], 3)
        self.assertEqual(groups[1]["run_id"], 3)
        self.assertEqual(groups[0]["severity"], "warn")
        self.assertEqual(diagnostics.group_problems(runs, "2026-10-05T00:00:00")[0]["job"], "Movies")
        self.assertEqual(diagnostics.unseen_problem_count(runs, "2026-10-02T12:00:00"), 2)


class Report(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.api = NextPullApi(self.tmp / "data", poll_seconds=0.05)
        self.api._engine.rclone_finder = lambda: FAKE_RCLONE
        self.addCleanup(self.api.shutdown)
        self.api._secrets.save("https://cloud.example.org", "friendbob", SECRET)
        applog.register_secret(SECRET)
        job = self.api._config.save_job(make_job(name="Secret-Show", source="/Private/Holiday Pics", dest=str(self.tmp / "dest")))
        run_id = self.api._history.start_run(job, "schedule")
        log_path = self.tmp / "data" / "logs" / f"run-{run_id}.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps({"level": "error", "msg": f"Failed to copy: 403 Forbidden using {SECRET}", "object": "Private/Holiday Pics/beach.jpg", "time": "2026-10-01T03:00:00"}) + "\n", encoding="utf-8")
        self.api._history.set_log_file(run_id, str(log_path))
        self.api._history.add_file(run_id, "Private/Holiday Pics/beach.jpg", 10, "error", error="403 Forbidden")
        self.api._history.finish_run(run_id, "failed", f"403 Forbidden for friendbob at https://cloud.example.org with {SECRET}")
        self.run_id = run_id

    def read_zip(self, path: str) -> dict[str, str]:
        with zipfile.ZipFile(path) as archive:
            return {name: archive.read(name).decode("utf-8") for name in archive.namelist()}

    def test_report_has_no_secret_and_hides_names_by_default(self) -> None:
        result = self.api.export_report(False, "it fails every night")
        self.assertTrue(result["ok"], result)
        files = self.read_zip(result["path"])
        self.assertTrue({"README.txt", "summary.json", "problems.json", "runs.csv", "app.log"} <= set(files), set(files))
        blob = "\n".join(files.values())
        for forbidden in (SECRET, "friendbob", "cloud.example.org", "Holiday Pics", "beach.jpg", "Secret-Show"):
            self.assertNotIn(forbidden, blob, forbidden)
        self.assertIn("run-logs/run-%d.jsonl" % self.run_id, files)
        self.assertIn("403 Forbidden", blob)           # the actual reason survives
        self.assertIn("it fails every night", blob)    # the user's own note
        self.assertTrue(json.loads(files["summary.json"])["system"]["app_version"])

    def test_report_with_names_still_never_has_the_password(self) -> None:
        files = self.read_zip(self.api.export_report(True)["path"])
        blob = "\n".join(files.values())
        self.assertNotIn(SECRET, blob)
        self.assertIn("beach.jpg", blob)
        self.assertIn("Holiday Pics", blob)

    def test_only_the_five_newest_reports_are_kept(self) -> None:
        paths = []
        for index in range(7):
            with mock.patch("diagnostics.datetime") as fake:
                fake.now.return_value = __import__("datetime").datetime(2026, 10, 1, 12, 0, index)
                paths.append(self.api.export_report(False)["path"])
        existing = sorted(p.name for p in (self.tmp / "data" / "reports").glob("*.zip"))
        self.assertEqual(len(existing), 5)

    def test_diagnostics_and_ack(self) -> None:
        info = self.api.diagnostics()
        self.assertTrue(info["ok"])
        self.assertEqual(info["runs"][0]["job"], "Secret-Show")
        self.assertEqual(info["unseen"], 1)
        self.assertEqual(self.api.status()["problem_count"], 1)
        time.sleep(1.1)
        self.api.ack_problems()
        self.assertEqual(self.api.status()["problem_count"], 0)

    def test_reveal_only_serves_reports(self) -> None:
        self.assertFalse(self.api.reveal_report(str(self.tmp / "data" / "settings.json"))["ok"])
        path = self.api.export_report(False)["path"]
        with mock.patch("winutil.reveal_path") as reveal:
            self.assertTrue(self.api.reveal_report(path)["ok"])
            reveal.assert_called_once()

    def test_ui_errors_are_logged_and_capped(self) -> None:
        for index in range(MAX_UI_ERRORS + 25):
            self.api.log_ui_error(f"TypeError: x{index}", "app.js", index)
        entries = applog.read_log(self.tmp / "data", 5000, "ERROR", "TypeError")
        self.assertEqual(len(entries), MAX_UI_ERRORS)


class SelfTest(TempCase):
    def test_not_connected_reports_clear_errors(self) -> None:
        api = NextPullApi(None, poll_seconds=0.05)
        self.addCleanup(api.shutdown)
        api._engine.rclone_finder = lambda: FAKE_RCLONE
        result = api.self_test()
        self.assertTrue(result["ok"])
        by_id = {item["id"]: item for item in result["results"]}
        self.assertEqual(by_id["nextcloud"]["status"], "error")
        self.assertIn("Reconnect", by_id["nextcloud"]["hint"])
        self.assertEqual(by_id["jobs"]["status"], "warn")
        self.assertEqual(by_id["scheduler"]["status"], "error")   # engine not started in this test

    def test_a_crashing_check_does_not_hide_the_others(self) -> None:
        def boom() -> dict:
            raise RuntimeError("kaput")

        results = diagnostics.run_self_test(rclone=boom, session=None, jobs=[], engine_last_tick=time.monotonic(), tick_seconds=10, data_dir=None,
                                            free_bytes=lambda p: 10 ** 12, autostart=True, watchdog=False)
        by_id = {item["id"]: item for item in results}
        self.assertEqual(by_id["rclone"]["status"], "error")
        self.assertIn("kaput", by_id["rclone"]["detail"])
        self.assertEqual(by_id["scheduler"]["status"], "ok")
        self.assertEqual(by_id["autostart"]["status"], "ok")

    def test_job_checks_find_a_missing_drive_and_low_space(self) -> None:
        jobs = [make_job(id="j1", name="Missing", source="/A", dest="Z:\\definitely\\not\\there" if sys.platform == "win32" else "/nonexistent-root-xyz/a"),
                make_job(id="j2", name="Tight", source="/B", dest=str(self.tmp))]
        session = mock.Mock()
        session.return_value.check_folder.return_value = True
        session.return_value.user_info.return_value = {"display_name": "Bob"}
        results = diagnostics.run_self_test(rclone=lambda: {"found": True, "version": "v1", "path": "x"}, session=session, jobs=jobs, engine_last_tick=time.monotonic(),
                                            tick_seconds=10, data_dir=None, free_bytes=lambda p: 1024 ** 3, autostart=False, watchdog=False)
        by_id = {item["id"]: item for item in results}
        self.assertEqual(by_id["job:j1"]["status"], "error")
        self.assertEqual(by_id["job:j2:space"]["status"], "warn")
        self.assertEqual(by_id["autostart"]["status"], "warn")
        self.assertEqual(by_id["nextcloud"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
