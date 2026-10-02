from __future__ import annotations

import os
import time
import unittest
from datetime import datetime

import helpers
from helpers import FAKE_RCLONE, TempCase, make_job, write_file
from functools import partial
from engine import Engine
from rclone_runner import RcloneRun
from store import ConfigStore, HistoryDb, SecretStore

CREDS = ("https://cloud.example.org", "alice", "SuperSecret-Pass-42")


def dt(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, second)


class EngineCase(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.now = dt(1, 2, 0, 5)
        self.src = self.tmp / "remote" / "EXTRACTED"
        self.dest = self.tmp / "dest"
        os.environ["FAKE_REMOTE_ROOT"] = str(self.tmp / "remote")
        self.config = ConfigStore(None)
        self.config.save_settings({"prevent_sleep": False})
        self.history = HistoryDb(None)
        self.addCleanup(self.history.close)
        self.secrets = SecretStore(None)
        self.secrets.save(*CREDS)
        self.rclone = FAKE_RCLONE
        self.notified: list[tuple[str, str]] = []
        self.engine = Engine(self.config, self.history, self.secrets, logs_dir=self.tmp / "logs", rclone_finder=lambda: self.rclone, clock=lambda: self.now,
                             tick_seconds=10, notify=lambda title, text: self.notified.append((title, text)), run_factory=partial(RcloneRun, precheck=False))
        self.addCleanup(self.engine.stop)

    def add_job(self, **overrides) -> dict:
        return self.config.save_job(make_job(dest=str(self.dest), **overrides))

    def wait_idle(self, timeout: float = 40.0) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if not self.engine.is_busy():
                return
            time.sleep(0.1)
        self.fail(f"engine still busy: {self.engine.status()}")

    def wait_running(self) -> None:
        for _ in range(150):
            current = self.engine.status()["current"]
            if current and current["transferring"]:
                return
            time.sleep(0.1)
        self.fail("run never started transferring")

    def runs(self) -> list[dict]:
        return sorted(self.history.list_runs(), key=lambda run: run["id"])

    def slow(self) -> None:
        os.environ["FAKE_SPEED"] = "150000"
        write_file(self.src / "big.mkv", 3_000_000)


class Scheduling(EngineCase):
    def test_a_slot_that_just_passed_runs_on_time(self) -> None:
        self.add_job()
        write_file(self.src / "a.mkv", 1234)
        self.config.set_state("last_checked", dt(1, 1, 59, 50).isoformat())
        self.engine.tick()
        self.wait_idle()
        (run,) = self.runs()
        self.assertEqual((run["trigger"], run["state"], run["files"], run["bytes"]), ("schedule", "ok", 1, 1234))
        self.assertEqual((self.dest / "a.mkv").stat().st_size, 1234)
        self.assertEqual(self.config.state("last_checked"), self.now.isoformat(timespec="seconds"))

    def test_nothing_due_nothing_runs(self) -> None:
        self.add_job()
        self.config.set_state("last_checked", dt(1, 2, 0, 5).isoformat())
        self.now = dt(1, 2, 0, 15)
        self.engine.tick()
        self.assertEqual(self.runs(), [])

    def test_late_slot_catches_up_when_allowed(self) -> None:
        self.add_job(catch_up=True, catch_up_hours=12)
        write_file(self.src / "a.mkv", 10)
        self.config.set_state("last_checked", dt(1, 1, 0).isoformat())
        self.now = dt(1, 5, 0)
        self.engine.tick()
        self.wait_idle()
        self.assertEqual([(run["trigger"], run["state"]) for run in self.runs()], [("catch-up", "ok")])

    def test_late_slot_is_recorded_as_missed_without_catch_up(self) -> None:
        self.add_job(catch_up=False)
        write_file(self.src / "a.mkv", 10)
        self.config.set_state("last_checked", dt(1, 1, 0).isoformat())
        self.now = dt(1, 5, 0)
        self.engine.tick()
        self.assertEqual([(run["state"], run["started"]) for run in self.runs()], [("missed", "2026-10-01T02:00:00")])
        self.assertFalse(self.dest.exists())

    def test_several_missed_days_record_each_and_run_only_the_latest(self) -> None:
        self.add_job(catch_up=True, catch_up_hours=12)
        write_file(self.src / "a.mkv", 10)
        self.config.set_state("last_checked", dt(1, 0, 0).isoformat().replace("-10-01", "-09-28"))
        self.engine.tick()   # now = Oct 1 02:00:05
        self.wait_idle()
        self.assertEqual([run["state"] for run in self.runs()], ["missed", "missed", "missed", "ok"])   # Sep 28, 29, 30 missed; Oct 1 runs
        self.assertEqual([run["started"][:10] for run in self.runs()[:3]], ["2026-09-28", "2026-09-29", "2026-09-30"])

    def test_disabled_jobs_are_ignored(self) -> None:
        self.add_job(enabled=False)
        self.config.set_state("last_checked", dt(1, 1, 59, 50).isoformat())
        self.engine.tick()
        self.assertEqual(self.runs(), [])

    def test_a_clock_that_went_backwards_never_replays(self) -> None:
        self.add_job()
        self.config.set_state("last_checked", dt(5, 0, 0).isoformat())   # in the future
        self.engine.tick()
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.config.state("last_checked"), self.now.isoformat(timespec="seconds"))

    def test_only_on_chosen_weekdays(self) -> None:
        self.add_job(schedule={"days": [4], "time": "02:00", "stop_by": ""})   # Friday only; Oct 1 2026 is a Thursday
        self.config.set_state("last_checked", dt(1, 1, 59, 50).isoformat())
        self.engine.tick()
        self.assertEqual(self.runs(), [])


class Failures(EngineCase):
    def test_not_connected_is_a_clear_failed_run(self) -> None:
        self.secrets.clear()
        job = self.add_job()
        self.assertTrue(self.engine.run_now(job["id"])["ok"])
        self.wait_idle()
        (run,) = self.runs()
        self.assertEqual(run["state"], "failed")
        self.assertIn("Not connected", run["message"])

    def test_missing_rclone_is_a_clear_failed_run(self) -> None:
        self.rclone = None
        job = self.add_job()
        self.engine.run_now(job["id"])
        self.wait_idle()
        self.assertIn("rclone was not found", self.runs()[0]["message"])

    def test_unknown_job(self) -> None:
        self.assertFalse(self.engine.run_now("nope")["ok"])


class Running(EngineCase):
    def test_manual_run_with_a_dry_run_override(self) -> None:
        job = self.add_job()
        write_file(self.src / "a.mkv", 10)
        self.engine.run_now(job["id"], dry_run=True)
        self.wait_idle()
        self.assertEqual(self.runs()[0]["dry_run"], 1)
        self.assertFalse((self.dest / "a.mkv").exists())

    def test_the_same_job_is_never_queued_twice(self) -> None:
        self.slow()
        job = self.add_job()
        self.assertTrue(self.engine.run_now(job["id"])["ok"])
        self.wait_running()
        again = self.engine.run_now(job["id"])
        self.assertFalse(again["ok"])
        self.assertIn("already", again["error"])
        self.engine.cancel()
        self.wait_idle()

    def test_a_due_slot_while_the_job_still_runs_is_recorded_as_skipped(self) -> None:
        self.slow()
        job = self.add_job()
        self.engine.run_now(job["id"])
        self.wait_running()
        self.config.set_state("last_checked", dt(1, 1, 59, 50).isoformat())
        self.engine.tick()
        self.assertIn("skipped", [run["state"] for run in self.runs()])
        self.engine.cancel()
        self.wait_idle()

    def test_second_job_waits_for_the_first(self) -> None:
        self.slow()
        first = self.add_job(name="First")
        second = self.add_job(name="Second", source="Other")
        write_file(self.tmp / "remote" / "Other" / "o.mkv", 10)
        self.engine.run_now(first["id"])
        self.wait_running()
        result = self.engine.run_now(second["id"])
        self.assertEqual((result["ok"], result["queued_behind"]), (True, "First"))
        self.assertEqual([entry["job_name"] for entry in self.engine.status()["queue"]], ["Second"])
        self.engine.cancel(first["id"])   # cancel only the running one
        self.wait_idle()
        self.assertEqual([(run["job_name"], run["state"]) for run in self.runs()], [("First", "cancelled"), ("Second", "ok")])

    def test_cancel_stops_the_run_and_clears_the_queue(self) -> None:
        self.slow()
        first, second = self.add_job(name="A"), self.add_job(name="B")
        self.engine.run_now(first["id"])
        self.wait_running()
        self.engine.run_now(second["id"])
        self.assertTrue(self.engine.cancel())
        self.wait_idle()
        self.assertEqual([run["state"] for run in self.runs()], ["cancelled"])
        self.assertEqual(self.engine.status()["queue"], [])

    def test_stop_by_ends_a_scheduled_run_but_not_a_manual_one(self) -> None:
        self.slow()
        job = self.add_job(schedule={"days": list(range(7)), "time": "02:00", "stop_by": "02:01"})
        self.config.set_state("last_checked", dt(1, 1, 59, 50).isoformat())
        self.engine.tick()
        self.wait_running()
        self.now = dt(1, 2, 2)   # past the window
        self.wait_idle()
        self.assertEqual(self.runs()[0]["state"], "stopped")
        self.history.clear()
        self.now = dt(1, 5, 0)
        self.engine.run_now(job["id"])
        self.wait_running()
        time.sleep(1.5)
        self.assertEqual(self.engine.status()["current"]["state"], "running")   # manual ignores stop_by
        self.engine.cancel()
        self.wait_idle()

    def test_status_and_log_reading(self) -> None:
        job = self.add_job()
        write_file(self.src / "a.mkv", 10)
        self.engine.run_now(job["id"])
        self.wait_idle()
        status = self.engine.status()
        entry = status["jobs"][0]
        self.assertEqual((entry["name"], entry["enabled"], entry["next_run"]), ("Night pull", True, "2026-10-02T02:00:00"))
        self.assertEqual((entry["last_run"]["state"], entry["last_run"]["files"]), ("ok", 1))
        self.assertFalse(status["running"])
        lines = self.engine.read_log(self.runs()[0]["id"])
        self.assertTrue(any(line["msg"].startswith("Copied") and line["object"] == "a.mkv" for line in lines))
        self.assertEqual(self.engine.read_log(99999), [])

    def test_events_are_kept_newest_first(self) -> None:
        job = self.add_job()
        write_file(self.src / "a.mkv", 10)
        self.engine.run_now(job["id"])
        self.wait_idle()
        texts = [event["text"] for event in self.engine.events()]
        self.assertTrue(texts[0].startswith("Night pull: ok"))
        self.assertTrue(any("started" in text for text in texts))


class Notifications(EngineCase):
    def test_notifies_for_transfers_and_failures_only(self) -> None:
        job = self.add_job()
        self.src.mkdir(parents=True)
        self.engine.run_now(job["id"])   # nothing to download: quiet
        self.wait_idle()
        self.assertEqual(self.notified, [])
        write_file(self.src / "a.mkv", 10)
        self.engine.run_now(job["id"])
        self.wait_idle()
        self.assertEqual(len(self.notified), 1)
        self.assertIn("Night pull", self.notified[0][0])
        os.environ["FAKE_FATAL"] = "1"
        self.engine.run_now(job["id"])
        self.wait_idle()
        self.assertEqual(len(self.notified), 2)
        self.assertIn("Failed", self.notified[1][1])

    def test_can_be_switched_off(self) -> None:
        self.config.save_settings({"prevent_sleep": False, "notifications": False})
        job = self.add_job()
        write_file(self.src / "a.mkv", 10)
        self.engine.run_now(job["id"])
        self.wait_idle()
        self.assertEqual(self.notified, [])


class Lifecycle(EngineCase):
    def test_start_marks_leftover_running_rows_interrupted_and_sets_the_marker(self) -> None:
        job = self.add_job()
        stale = self.history.start_run(job, "schedule")
        self.engine.start()
        self.engine.stop()
        self.assertEqual(self.history.get_run(stale)["state"], "interrupted")
        self.assertTrue(self.config.state("last_checked"))

    def test_housekeeping_removes_old_logs_once_a_day(self) -> None:
        old = write_file(self.tmp / "logs" / "run-1.jsonl", 10, age_seconds=200 * 86400)
        fresh = write_file(self.tmp / "logs" / "run-2.jsonl", 10, age_seconds=60)
        self.engine.tick()
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_the_background_thread_runs_ticks(self) -> None:
        self.add_job()
        write_file(self.src / "a.mkv", 10)
        self.engine.tick_seconds = 0.2
        self.config.set_state("last_checked", dt(1, 1, 59, 50).isoformat())
        self.engine.start()
        for _ in range(100):
            if any(run["state"] == "ok" for run in self.runs()):
                break
            time.sleep(0.2)
        self.engine.stop()
        self.assertTrue(any(run["state"] == "ok" for run in self.runs()))


if __name__ == "__main__":
    unittest.main()
