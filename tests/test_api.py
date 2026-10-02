from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

import helpers
from helpers import FAKE_RCLONE, TempCase, make_job, write_file
from fake_nextcloud import FakeNextcloud
from nextpull import NextPullApi, safe
import winutil

PASSWORD = "app-pass-1234"


class BridgeContract(unittest.TestCase):
    def test_every_public_attribute_is_a_method(self) -> None:
        api = NextPullApi(None)
        for name in dir(api):
            if not name.startswith("_"):
                self.assertTrue(callable(getattr(api, name)), f"{name} would be exposed to JavaScript as a non-method")
        api.shutdown()

    def test_unexpected_errors_never_escape(self) -> None:
        class Boom:
            @safe
            def go(self) -> dict:
                raise KeyError("x")

            @safe
            def known(self) -> dict:
                raise ValueError("Pick a folder")

        self.assertFalse(Boom().go()["ok"])
        self.assertIn("Unexpected error", Boom().go()["error"])
        self.assertEqual(Boom().known(), {"ok": False, "error": "Pick a folder"})


class ApiCase(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.remote = self.tmp / "remote"
        write_file(self.remote / "EXTRACTED" / "a.mkv", 2000)
        write_file(self.remote / "EXTRACTED" / "Sub" / "b.mkv", 500)
        os.environ["FAKE_REMOTE_ROOT"] = str(self.remote)
        self.server = FakeNextcloud(self.remote)
        self.base = self.server.start()
        self.addCleanup(self.server.stop)
        self.api = NextPullApi(None, poll_seconds=0.05)
        self.api._engine.rclone_finder = lambda: FAKE_RCLONE
        self.api._config.save_settings({"prevent_sleep": False})
        self.addCleanup(self.api.shutdown)
        self.results: list[dict] = []

    def call(self, name: str, *args, **kwargs) -> dict:
        result = getattr(self.api, name)(*args, **kwargs)
        self.results.append(result)
        return result

    def connect(self) -> None:
        with mock.patch("nextpull.webbrowser.open"):
            self.assertTrue(self.call("connect_start", self.base)["ok"])
        self.server.approved.set()
        for _ in range(100):
            if self.call("connect_status")["state"] == "connected":
                return
            time.sleep(0.05)
        self.fail(self.api._login)

    def wait_idle(self) -> None:
        for _ in range(300):
            if not self.api._engine.is_busy():
                return
            time.sleep(0.1)
        self.fail("engine busy")

    def assertNoPasswordAnywhere(self) -> None:
        self.assertNotIn(PASSWORD, json.dumps(self.results, default=str))


class Connection(ApiCase):
    def test_not_connected_state(self) -> None:
        boot = self.call("bootstrap")
        self.assertEqual((boot["ok"], boot["connection"]), (True, {"connected": False}))
        self.assertFalse(self.call("browse")["ok"])
        self.assertIn("Not connected", self.call("browse")["error"])
        self.assertFalse(self.call("status")["connected"])

    def test_login_flow_through_the_bridge(self) -> None:
        with mock.patch("nextpull.webbrowser.open") as opened:
            started = self.call("connect_start", self.base)
        self.assertTrue(started["login_url"].startswith(self.base + "/login/v2/flow/"))
        opened.assert_called_once_with(started["login_url"])
        self.assertEqual(self.call("connect_status")["state"], "waiting")
        self.server.approved.set()
        for _ in range(100):
            if self.call("connect_status")["state"] == "connected":
                break
            time.sleep(0.05)
        connection = self.call("connection", refresh=True)["connection"]
        self.assertEqual((connection["connected"], connection["user"], connection["display_name"]), (True, "alice", "Alice Example"))
        self.assertEqual(self.api._secrets.load()["app_password"], PASSWORD)   # stored in the secret store...
        self.assertNoPasswordAnywhere()                                       # ...and never handed to the UI

    def test_bad_address_and_cancel(self) -> None:
        self.assertFalse(self.call("connect_start", "")["ok"])
        with mock.patch("nextpull.webbrowser.open"):
            self.call("connect_start", self.base)
        self.call("connect_cancel")
        for _ in range(100):
            if self.call("connect_status")["state"] == "cancelled":
                break
            time.sleep(0.05)
        self.assertEqual(self.call("connect_status")["state"], "cancelled")
        self.assertFalse(self.api._secrets.connected())

    def test_disconnect_revokes_the_app_password(self) -> None:
        self.connect()
        result = self.call("disconnect")
        self.assertEqual((result["ok"], result["revoked"]), (True, True))
        self.assertTrue(self.server.revoked)
        self.assertFalse(self.api._secrets.connected())

    def test_disconnect_without_revoking(self) -> None:
        self.connect()
        self.assertFalse(self.call("disconnect", revoke=False)["revoked"])
        self.assertFalse(self.server.revoked)

    def test_browse(self) -> None:
        self.connect()
        root = self.call("browse")
        self.assertEqual([entry["name"] for entry in root["entries"]], ["EXTRACTED"])
        self.assertEqual((root["path"], root["parent"]), ("", None))
        sub = self.call("browse", "EXTRACTED/Sub")
        self.assertEqual((sub["path"], sub["parent"], sub["entries"][0]["name"]), ("EXTRACTED/Sub", "EXTRACTED", "b.mkv"))
        self.assertNoPasswordAnywhere()


class JobsAndRuns(ApiCase):
    def job(self, **extra) -> dict:
        return {"name": "Night", "source": "EXTRACTED", "dest": str(self.tmp / "dest"), "min_age_minutes": 0, "mode": "copy", **extra}

    def test_job_crud_and_views(self) -> None:
        saved = self.call("save_job", self.job())
        self.assertTrue(saved["ok"])
        listed = self.call("list_jobs")["jobs"][0]
        self.assertEqual((listed["name"], listed["schedule_text"], listed["next_run"] is not None), ("Night", "Every day at 02:00", True))
        copy = self.call("duplicate_job", saved["job"]["id"])["job"]
        self.assertEqual((copy["name"], copy["enabled"]), ("Night (copy)", False))
        self.assertNotEqual(copy["id"], saved["job"]["id"])
        off = self.call("set_job_enabled", saved["job"]["id"], False)["job"]
        self.assertFalse(off["enabled"])
        self.assertIsNone([job for job in self.call("list_jobs")["jobs"] if job["id"] == off["id"]][0]["next_run"])
        self.assertTrue(self.call("delete_job", copy["id"])["ok"])
        self.assertEqual(len(self.call("list_jobs")["jobs"]), 1)
        self.assertFalse(self.call("set_job_enabled", "nope", True)["ok"])
        self.assertFalse(self.call("duplicate_job", "nope")["ok"])

    def test_invalid_jobs_come_back_as_messages(self) -> None:
        for bad, text in (({**self.job(), "name": ""}, "name"), ({**self.job(), "source": "a/../b"}, ".."), ({**self.job(), "schedule": {"days": []}}, "day")):
            result = self.call("save_job", bad)
            self.assertFalse(result["ok"])
            self.assertIn(text, result["error"])

    def test_run_history_log_and_stats_end_to_end(self) -> None:
        self.connect()
        job = self.call("save_job", self.job())["job"]
        started = self.call("run_job", job["id"])
        self.assertTrue(started["ok"])
        self.wait_idle()
        (run,) = self.call("history")["runs"]
        self.assertEqual((run["state"], run["files"], run["bytes"], run["trigger"]), ("ok", 2, 2500, "manual"))
        detail = self.call("run_detail", run["id"])
        self.assertEqual(sorted(f["name"] for f in detail["files"]), ["Sub/b.mkv", "a.mkv"])
        self.assertTrue(any(line["msg"].startswith("Copied") for line in self.call("run_log", run["id"])["lines"]))
        stats = self.call("stats", 7)
        self.assertEqual((stats["totals"]["files"], stats["totals"]["bytes"], len(stats["daily"])), (2, 2500, 7))
        self.assertEqual(self.call("history", job_id="other")["runs"], [])
        self.assertFalse(self.call("run_detail", 999)["ok"])
        self.assertTrue(self.call("clear_history")["ok"])
        self.assertEqual(self.call("history")["runs"], [])
        self.assertNoPasswordAnywhere()

    def test_cannot_disconnect_while_a_download_runs(self) -> None:
        self.connect()
        os.environ["FAKE_SPEED"] = "150000"
        write_file(self.remote / "EXTRACTED" / "big.mkv", 3_000_000)
        job = self.call("save_job", self.job())["job"]
        self.call("run_job", job["id"])
        for _ in range(150):   # wait until rclone is really transferring (its control port is up)
            current = self.call("status")["current"]
            if current and current["transferring"]:
                break
            time.sleep(0.1)
        self.assertFalse(self.call("disconnect")["ok"])
        self.assertTrue(self.call("set_speed", 1)["ok"])
        self.assertTrue(self.call("cancel_run")["ok"])
        self.wait_idle()
        self.assertFalse(self.call("set_speed", 1)["ok"])
        self.assertFalse(self.call("set_speed", -1)["ok"])

    def test_dry_run_through_the_bridge(self) -> None:
        self.connect()
        job = self.call("save_job", self.job())["job"]
        self.call("run_job", job["id"], dry_run=True)
        self.wait_idle()
        self.assertEqual(self.call("history")["runs"][0]["dry_run"], 1)
        self.assertFalse((self.tmp / "dest" / "a.mkv").exists())


class SettingsAndFiles(ApiCase):
    def test_settings_round_trip_and_whitelist(self) -> None:
        saved = self.call("save_settings", {"notifications": False, "history_days": 30, "evil": 1})["settings"]
        self.assertEqual((saved["notifications"], saved["history_days"], "evil" in saved), (False, 30, False))
        self.assertEqual(self.call("get_settings")["settings"], saved)

    def test_check_rclone(self) -> None:
        self.assertFalse(self.call("check_rclone", str(self.tmp / "nope.exe"))["found"])
        exe = write_file(self.tmp / "not-rclone.exe", 5)
        self.assertFalse(self.call("check_rclone", str(exe))["found"])   # exists but is not rclone

    def test_test_destination(self) -> None:
        existing = self.call("test_destination", str(self.tmp))
        self.assertEqual((existing["exists"], existing["writable"], existing["free_bytes"] > 0), (True, True, True))
        new = self.call("test_destination", str(self.tmp / "later" / "x"))
        self.assertEqual((new["exists"], new["writable"]), (False, True))
        self.assertFalse(self.call("test_destination", "  ")["ok"])

    def test_export_import(self) -> None:
        self.call("save_job", {"name": "A", "source": "x", "dest": "y"})
        text = self.call("export_jobs")["text"]
        self.assertNotIn(PASSWORD, text)
        self.assertEqual(self.call("import_jobs", text)["imported"], 1)
        self.assertEqual(len(self.call("list_jobs")["jobs"]), 2)
        self.assertFalse(self.call("import_jobs", "{bad")["ok"])

    def test_open_path_refuses_missing_folders(self) -> None:
        self.assertFalse(self.call("open_path", str(self.tmp / "nothing"))["ok"])
        with mock.patch.object(winutil, "open_path") as opened:
            self.assertTrue(self.call("open_path", str(self.tmp))["ok"])
        opened.assert_called_once()

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_autostart_commands_are_built_for_the_running_program(self) -> None:
        command = winutil.launch_command(True)
        self.assertIn("--background", command)
        self.assertIn("nextpull", command.lower())


class PersistenceIsOptIn(TempCase):
    def test_a_data_dir_persists_and_none_does_not(self) -> None:
        api = NextPullApi(self.tmp / "data")
        api.save_job({"name": "A", "source": "x", "dest": "y"})
        api.shutdown()
        again = NextPullApi(self.tmp / "data")
        self.assertEqual(len(again.list_jobs()["jobs"]), 1)
        again.shutdown()
        memory = NextPullApi(None)
        self.assertEqual(memory.list_jobs()["jobs"], [])
        memory.shutdown()


if __name__ == "__main__":
    unittest.main()
