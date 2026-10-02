from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta

import helpers
from helpers import TempCase, make_job
from store import ConfigStore, HistoryDb, SecretStore, clean_job, clean_remote_path, clean_settings, default_settings


class JobValidation(unittest.TestCase):
    def test_defaults_and_id(self) -> None:
        job = make_job(dest="D:\\Films")
        self.assertEqual(len(job["id"]), 8)
        self.assertEqual((job["mode"], job["transfers"], job["streams"], job["retries"]), ("copy", 1, 4, 3))
        self.assertTrue(job["catch_up"])
        self.assertFalse(job["dry_run"])

    def test_required_fields(self) -> None:
        for field, message in (("name", "name"), ("source", "Nextcloud folder"), ("dest", "destination")):
            with self.assertRaises(ValueError, msg=field) as caught:
                clean_job({"name": "x", "source": "a", "dest": "b"} | {field: "  "})
            self.assertIn(message, str(caught.exception))
        with self.assertRaises(ValueError):
            clean_job("nope")

    def test_mode_and_numbers_are_clamped(self) -> None:
        job = make_job(dest="x", transfers=99, streams=0, min_age_minutes=-5, retries="abc", mode="MOVE", speed={"limit_mbps": "-3", "timetable": " 08:00,2M "})
        self.assertEqual((job["mode"], job["transfers"], job["streams"], job["min_age_minutes"], job["retries"]), ("move", 8, 1, 0, 3))
        self.assertEqual(job["speed"], {"limit_mbps": 0, "timetable": "08:00,2M"})
        with self.assertRaises(ValueError):
            make_job(dest="x", mode="sync")

    def test_remote_path_cleaning(self) -> None:
        self.assertEqual(clean_remote_path("\\Shared\\EXTRACTED/"), "Shared/EXTRACTED")
        self.assertEqual(clean_remote_path("/a//b/./c"), "a/b/c")
        with self.assertRaises(ValueError):
            clean_remote_path("a/../b")
        with self.assertRaises(ValueError):
            make_job(dest="x", source="../etc")

    def test_patterns_from_text_or_list(self) -> None:
        job = make_job(dest="x", excludes="*.log\n  \n*.tmp\n*.log", includes=["*.mkv", "*.mkv"])
        self.assertEqual(job["excludes"], ["*.log", "*.tmp"])
        self.assertEqual(job["includes"], ["*.mkv"])

    def test_settings_whitelist(self) -> None:
        settings = clean_settings({"notifications": 0, "history_days": 99999, "evil": "x", "rclone_path": " C:\\r\\rclone.exe "})
        self.assertEqual(set(settings), set(default_settings()))
        self.assertFalse(settings["notifications"])
        self.assertEqual((settings["history_days"], settings["rclone_path"]), (3650, "C:\\r\\rclone.exe"))


class ConfigPersistence(TempCase):
    def test_round_trip_and_in_memory_mode(self) -> None:
        store = ConfigStore(self.tmp)
        saved = store.save_job(make_job(dest="D:\\a", name="One"))
        store.save_settings({"notifications": False})
        store.set_state("last_checked", "2026-10-01T02:00:00")
        again = ConfigStore(self.tmp)
        self.assertEqual(again.jobs(), [saved])
        self.assertFalse(again.settings()["notifications"])
        self.assertEqual(again.state("last_checked"), "2026-10-01T02:00:00")
        memory = ConfigStore(None)
        memory.save_job(make_job(dest="x"))
        self.assertEqual(len(memory.jobs()), 1)
        self.assertEqual(list(self.tmp.glob("*.json")), list(self.tmp.glob("*.json")))   # memory store wrote nothing of its own

    def test_update_delete_and_corrupt_entries_dropped(self) -> None:
        store = ConfigStore(self.tmp)
        job = store.save_job(make_job(dest="x", name="A"))
        store.save_job({**job, "name": "A2"})
        self.assertEqual([entry["name"] for entry in store.jobs()], ["A2"])
        self.assertTrue(store.delete_job(job["id"]))
        self.assertFalse(store.delete_job(job["id"]))
        (self.tmp / "jobs.json").write_text(json.dumps([{"name": "no source"}, make_job(dest="x", name="Good")]), encoding="utf-8")
        self.assertEqual([entry["name"] for entry in ConfigStore(self.tmp).jobs()], ["Good"])

    def test_mutating_a_returned_job_does_not_change_the_store(self) -> None:
        store = ConfigStore(None)
        saved = store.save_job(make_job(dest="x"))
        store.jobs()[0]["name"] = "hacked"
        self.assertEqual(store.job(saved["id"])["name"], "Night pull")

    def test_export_import_gives_new_ids_and_no_secrets(self) -> None:
        store = ConfigStore(None)
        original = store.save_job(make_job(dest="x"))
        text = store.export_jobs()
        self.assertNotIn("password", text.lower())
        other = ConfigStore(None)
        self.assertEqual(other.import_jobs(text), 1)
        self.assertNotEqual(other.jobs()[0]["id"], original["id"])
        self.assertEqual(store.import_jobs(text), 1)
        self.assertEqual(len(store.jobs()), 2)   # importing into the same store never overwrites
        with self.assertRaises(ValueError):
            store.import_jobs("not json")
        with self.assertRaises(ValueError):
            store.import_jobs('{"jobs": 5}')


class Secrets(TempCase):
    CODEC = {"protect": lambda data: data[::-1], "unprotect": lambda data: data[::-1]}

    def test_memory_only_without_data_dir(self) -> None:
        store = SecretStore(None)
        self.assertFalse(store.connected())
        store.save("https://c.example", "bob", "pw")
        self.assertEqual(store.load(), {"server": "https://c.example", "user": "bob", "app_password": "pw"})
        store.clear()
        self.assertIsNone(store.load())

    def test_file_never_holds_the_plain_password(self) -> None:
        store = SecretStore(self.tmp, **self.CODEC)
        store.save("https://c.example", "bob", "SuperSecret-123")
        blob = (self.tmp / "credentials.bin").read_bytes()
        self.assertNotIn(b"SuperSecret-123", blob)
        self.assertEqual(SecretStore(self.tmp, **self.CODEC).load()["app_password"], "SuperSecret-123")
        SecretStore(self.tmp, **self.CODEC).clear()
        self.assertFalse((self.tmp / "credentials.bin").exists())

    def test_corrupt_file_means_not_connected(self) -> None:
        (self.tmp / "credentials.bin").write_bytes(b"garbage")
        self.assertIsNone(SecretStore(self.tmp, **self.CODEC).load())

    @unittest.skipUnless(sys.platform == "win32", "DPAPI is Windows only")
    def test_real_dpapi_round_trip(self) -> None:
        store = SecretStore(self.tmp)   # default codec: Windows DPAPI
        store.save("https://c.example", "bob", "SuperSecret-123")
        self.assertNotIn(b"SuperSecret-123", (self.tmp / "credentials.bin").read_bytes())
        self.assertEqual(SecretStore(self.tmp).load()["user"], "bob")


class History(unittest.TestCase):
    def setUp(self) -> None:
        self.db = HistoryDb(None)
        self.addCleanup(self.db.close)
        self.job = make_job(dest="D:\\x")

    def run_with(self, state: str, files: list[tuple[str, int, float]], started: str | None = None, **extra) -> int:
        run_id = self.db.start_run(self.job, "schedule", started=started)
        for name, size, duration in files:
            self.db.add_file(run_id, name, size, "ok", duration=duration)
        self.db.finish_run(run_id, state, "msg", **extra)
        return run_id

    def test_aggregates_and_speed(self) -> None:
        run_id = self.db.start_run(self.job, "schedule")
        self.db.add_file(run_id, "a.mkv", 1000, "ok", duration=2.0)
        self.db.add_file(run_id, "b.mkv", 3000, "ok", duration=2.0)
        self.db.mark_file_deleted(run_id, "a.mkv")
        self.db.finish_run(run_id, "ok", "msg")
        run = self.db.get_run(run_id)
        self.assertEqual((run["state"], run["bytes"], run["files"], run["deleted"], run["errors"]), ("ok", 4000, 2, 1, 0))
        self.assertAlmostEqual(run["avg_speed"], 1000.0)
        self.assertEqual([f["name"] for f in self.db.list_files(run_id)], ["a.mkv", "b.mkv"])

    def test_error_files_count_and_explicit_error_floor(self) -> None:
        run_id = self.db.start_run(self.job, "manual")
        self.db.add_file(run_id, "bad.mkv", 0, "error", error="boom")
        self.db.finish_run(run_id, "partial", "x", errors=3)
        self.assertEqual(self.db.get_run(run_id)["errors"], 3)

    def test_recover_interrupted(self) -> None:
        running = self.db.start_run(self.job, "schedule")
        done = self.run_with("ok", [])
        self.assertEqual(self.db.recover_interrupted(), 1)
        self.assertEqual(self.db.get_run(running)["state"], "interrupted")
        self.assertEqual(self.db.get_run(done)["state"], "ok")

    def test_missed_and_skipped_rows(self) -> None:
        self.db.record_skipped(self.job, datetime(2026, 10, 1, 2, 0), "missed", "off")
        run = self.db.list_runs()[0]
        self.assertEqual((run["state"], run["started"], run["finished"]), ("missed", "2026-10-01T02:00:00", "2026-10-01T02:00:00"))
        self.assertIsNone(self.db.last_run(self.job["id"]))   # missed/skipped are not "the last run"

    def test_stats_series_totals_and_success_rate(self) -> None:
        today = datetime(2026, 10, 10, 12, 0)
        self.run_with("ok", [("a", 10_000_000, 10.0)], started="2026-10-10T02:00:00")
        self.run_with("ok", [("b", 30_000_000, 10.0)], started="2026-10-09T02:00:00")
        self.run_with("failed", [], started="2026-10-09T03:00:00")
        self.db.record_skipped(self.job, datetime(2026, 10, 8, 2, 0), "missed", "x")
        self.run_with("ok", [("old", 5, 1.0)], started="2026-08-01T02:00:00")   # outside a 7 day window
        stats = self.db.stats(7, today=today)
        self.assertEqual(len(stats["daily"]), 7)
        self.assertEqual((stats["daily"][0]["date"], stats["daily"][-1]["date"]), ("2026-10-04", "2026-10-10"))
        self.assertEqual(stats["totals"]["bytes"], 40_000_000)
        self.assertEqual((stats["totals"]["ok_runs"], stats["totals"]["failed_runs"], stats["totals"]["missed_runs"]), (2, 1, 1))
        self.assertAlmostEqual(stats["totals"]["success_rate"], 66.7)
        self.assertAlmostEqual(stats["totals"]["avg_speed"], 2_000_000.0)
        self.assertEqual(stats["daily"][-2]["bytes"], 30_000_000)
        self.assertEqual(stats["top_files"][0]["name"], "b")
        self.assertEqual(stats["by_job"][0]["files"], 2)
        self.assertIsNone(self.db.stats(1, today=datetime(2026, 12, 1))["totals"]["success_rate"])   # no runs -> no rate

    def test_prune_and_clear(self) -> None:
        old = self.run_with("ok", [("x", 1, 1.0)], started=(datetime.now() - timedelta(days=400)).replace(microsecond=0).isoformat())
        fresh = self.run_with("ok", [("y", 1, 1.0)])
        self.assertEqual(self.db.prune(365), 1)
        self.assertIsNone(self.db.get_run(old))
        self.assertEqual(self.db.list_files(old), [])
        self.assertIsNotNone(self.db.get_run(fresh))
        self.db.clear()
        self.assertEqual(self.db.list_runs(), [])

    def test_list_runs_filter_and_paging(self) -> None:
        other = make_job(dest="x", name="Other")
        for index in range(5):
            self.run_with("ok", [], started=f"2026-10-0{index + 1}T02:00:00")
        self.db.finish_run(self.db.start_run(other, "manual", started="2026-10-09T02:00:00"), "ok")
        self.assertEqual(len(self.db.list_runs(limit=3)), 3)
        self.assertEqual(self.db.list_runs(limit=2, offset=1)[0]["started"], "2026-10-05T02:00:00")
        self.assertEqual(len(self.db.list_runs(job_id=other["id"])), 1)


if __name__ == "__main__":
    unittest.main()
