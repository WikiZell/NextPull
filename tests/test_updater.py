"""NextPullUpdater (window state and bridge) and NextPull's side of the hand-over (install_update, --self-check, result banner)."""
from __future__ import annotations

import json
import os
import threading
import unittest
from pathlib import Path
from unittest import mock

import installer
import nextpull
import nextpull_updater as nu
import test_installer as ti
import updates
from helpers import TempCase
from installer import Outcome, Request


def sample_request(**kw) -> Request:
    base = dict(app_dir="C:/NextPull", data_dir="C:/data", current_version="0.2.0", version="0.3.0", asset_name="NextPull-0.3.0.zip", asset_url="https://github.com/x/NextPull-0.3.0.zip", size=1000)
    base.update(kw)
    return Request(**base)


class Describe(unittest.TestCase):
    def steps(self, active: str = "") -> list[dict]:
        return [{"key": key, "label": label, "state": "active" if key == active else "pending", "detail": ""} for key, label in installer.STEPS]

    def test_every_running_step_has_a_headline(self) -> None:
        for key, _ in installer.STEPS:
            headline, sub = nu.describe("running", self.steps(key), None, sample_request())
            self.assertTrue(headline and sub, key)

    def test_waiting_for_the_app_offers_force_wording(self) -> None:
        headline, _ = nu.describe("running", self.steps("wait"), None, sample_request(), need_force=True)
        self.assertIn("taking a while", headline)

    def test_outcomes_say_what_is_true(self) -> None:
        request = sample_request()
        ok = nu.describe("success", [], Outcome(ok=True, version="0.3.0", previous_version="0.2.0"), request)
        self.assertIn("0.3.0 is installed", ok[0])
        undone = nu.describe("failed", [], Outcome(ok=False, version="0.3.0", previous_version="0.2.0", rolled_back=True), request)
        self.assertIn("undone", undone[0])
        self.assertIn("0.2.0 is back", undone[1])
        untouched = nu.describe("failed", [], Outcome(ok=False, version="0.3.0", previous_version="0.2.0", step="verify"), request)
        self.assertIn("Nothing was changed", untouched[1])
        broken = nu.describe("failed", [], Outcome(ok=False, version="0.3.0", previous_version="0.2.0", rolled_back=True, restore_failed=True), request)
        self.assertIn("repair did not finish", broken[0])
        self.assertIn("backup", broken[1])
        cancelled = nu.describe("cancelled", [], Outcome(ok=False, version="0.3.0", previous_version="0.2.0", cancelled=True, step="download"), request)
        self.assertIn("cancelled", cancelled[0].lower())


class State(TempCase):
    def test_progress_text(self) -> None:
        self.assertEqual(nu.progress_text({"done": 0, "total": 0, "speed": 0}), "")
        text = nu.progress_text({"done": 10 * 1024 ** 2, "total": 80 * 1024 ** 2, "speed": 7 * 1024 ** 2})
        self.assertIn("10.0 MB of 80.0 MB", text)
        self.assertIn("MB/s", text)
        self.assertIn("s left", text)

    def test_snapshot_follows_the_steps_and_the_log_is_capped(self) -> None:
        state = nu.UpdaterState(sample_request(size=5 * 1024 ** 2), self.tmp / "u.log")
        state.step("wait", "active")
        state.step("wait", "done")
        state.step("backup", "active")
        state.progress(0, 0, 0)
        snap = state.snapshot()
        self.assertEqual(snap["phase"], "running")
        self.assertEqual(snap["active"], "backup")
        self.assertEqual([s["state"] for s in snap["steps"]][:3], ["done", "active", "pending"])
        self.assertEqual(snap["release"]["sizeText"], "5.0 MB")
        for index in range(500):
            state.log(f"line {index}")
        self.assertLessEqual(len(state.snapshot()["log"]), nu.MAX_LOG_LINES)
        self.assertIn("line 499", (self.tmp / "u.log").read_text(encoding="utf-8"))

    def test_cancel_only_while_allowed(self) -> None:
        state = nu.UpdaterState(sample_request())
        self.assertFalse(state.request_cancel())
        self.assertFalse(state.cancelled())
        state.can_cancel(True)
        self.assertTrue(state.snapshot()["canCancel"])
        self.assertTrue(state.request_cancel())
        self.assertTrue(state.cancelled())

    def test_finishing_sets_the_phase(self) -> None:
        state = nu.UpdaterState(sample_request())
        state.finish(Outcome(ok=False, version="0.3.0", previous_version="0.2.0", cancelled=True))
        self.assertEqual(state.snapshot()["phase"], "cancelled")
        self.assertFalse(state.snapshot()["canCancel"])

    def test_the_bridge_exposes_only_methods(self) -> None:
        api = nu.UpdaterApi(nu.UpdaterState(sample_request()))
        for name in dir(api):
            if not name.startswith("_"):
                self.assertTrue(callable(getattr(api, name)), name)

    def test_finish_is_refused_while_running_and_the_window_close_is_blocked(self) -> None:
        state = nu.UpdaterState(sample_request())
        api = nu.UpdaterApi(state)
        self.assertFalse(api.finish()["ok"])
        self.assertFalse(api._on_closing())
        state.finish(Outcome(ok=True, version="0.3.0", previous_version="0.2.0"))
        self.assertTrue(api._on_closing())

    def test_release_page_only_opens_github_links(self) -> None:
        with mock.patch("nextpull_updater.webbrowser.open") as opened:
            self.assertTrue(nu.UpdaterApi(nu.UpdaterState(sample_request(page="https://github.com/WikiZell/NextPull/releases/tag/v0.3.0"))).open_release_page()["ok"])
            self.assertFalse(nu.UpdaterApi(nu.UpdaterState(sample_request(page="https://evil.example.com/x"))).open_release_page()["ok"])
            self.assertEqual(opened.call_count, 1)

    def test_loopback_is_refused_unless_the_test_switch_is_set(self) -> None:
        self.assertFalse(nu.allowed_url("http://127.0.0.1:8000/x.zip"))
        self.assertTrue(nu.allowed_url("https://github.com/x.zip"))
        os.environ["NEXTPULL_UPDATER_ALLOW_LOOPBACK"] = "1"
        self.assertTrue(nu.allowed_url("http://127.0.0.1:8000/x.zip"))
        self.assertFalse(nu.allowed_url("http://evil.example.com/x.zip"))
        self.assertFalse(nu.allowed_url("https://evil.example.com/x.zip"))


class FakeLock:
    def __init__(self) -> None:
        self.events: list[str] = []

    def acquire(self) -> bool:
        self.events.append("lock")
        return True

    def release(self) -> None:
        self.events.append("unlock")


class WholeRun(ti.InstallerCase):
    """The updater driven end to end against a local fake release."""

    def test_a_good_update_through_the_window_state(self) -> None:
        os.environ["NEXTPULL_UPDATER_ALLOW_LOOPBACK"] = "1"
        request = self.request(self.release())
        state = nu.UpdaterState(request, self.tmp / "logs" / "u.log")
        lock = FakeLock()
        outcome = nu.run_update(request, state, lock, health_check=lambda p, s: {"ok": True, "version": "9.9.9"}, launch=lambda exe: ti.FakeProcess(), watch_seconds=0.1)
        self.assertTrue(outcome.ok, outcome.message)
        snap = state.snapshot()
        self.assertEqual(snap["phase"], "success")
        self.assertTrue(all(step["state"] == "done" for step in snap["steps"]))
        self.assertIn("9.9.9 is installed", snap["headline"])
        self.assertEqual(lock.events[0], "lock")
        self.assertTrue((self.tmp / "logs" / "u.log").read_text(encoding="utf-8").count("[restart] done"))

    def test_a_failed_update_through_the_window_state(self) -> None:
        os.environ["NEXTPULL_UPDATER_ALLOW_LOOPBACK"] = "1"
        request = self.request(self.release(digest="0" * 64))
        state = nu.UpdaterState(request)
        outcome = nu.run_update(request, state, FakeLock(), health_check=lambda p, s: {"ok": True, "version": "9.9.9"}, launch=lambda exe: ti.FakeProcess(), watch_seconds=0.1)
        self.assertFalse(outcome.ok)
        snap = state.snapshot()
        self.assertEqual(snap["phase"], "failed")
        self.assertIn("not installed", snap["headline"])
        self.assertTrue(any(step["state"] == "error" for step in snap["steps"]))
        states = {step["key"]: step["state"] for step in snap["steps"]}
        self.assertEqual((states["download"], states["verify"], states["install"]), ("done", "error", "pending"), "no step is left spinning after a failure")
        self.assertEqual(self.read("NextPull.exe"), b"old-exe")


    def test_cancel_while_downloading_shows_a_neutral_skipped_step(self) -> None:
        os.environ["NEXTPULL_UPDATER_ALLOW_LOOPBACK"] = "1"
        request = self.request(self.release())
        state = nu.UpdaterState(request)
        original = state.progress

        def progress(done, total, speed) -> None:
            original(done, total, speed)
            state._cancel.set()   # the user clicked Cancel
        state.progress = progress   # type: ignore[method-assign]
        outcome = nu.run_update(request, state, FakeLock(), health_check=lambda p, s: {"ok": True, "version": "9.9.9"}, launch=lambda exe: ti.FakeProcess(), watch_seconds=0.1)
        self.assertTrue(outcome.cancelled)
        snap = state.snapshot()
        self.assertEqual(snap["phase"], "cancelled")
        self.assertEqual({step["key"]: step["state"] for step in snap["steps"]}["download"], "skipped")
        self.assertEqual(self.read("NextPull.exe"), b"old-exe")


# ---------------------------------------------------------------------------------------------- NextPull's side

def release(tag: str = "v9.9.9") -> dict:
    zip_url = f"https://github.com/WikiZell/NextPull/releases/download/{tag}/NextPull-{tag[1:]}.zip"
    return {"tag_name": tag, "name": f"NextPull {tag}", "body": "- better", "html_url": f"https://github.com/WikiZell/NextPull/releases/tag/{tag}", "draft": False, "prerelease": False,
            "assets": [{"name": f"NextPull-{tag[1:]}.zip", "browser_download_url": zip_url, "size": 4321, "digest": "sha256:" + "cd" * 32}]}


class HandOver(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.program = self.tmp / "program"
        (self.program / "updater").mkdir(parents=True)
        (self.program / "updater" / "NextPullUpdater.exe").write_bytes(b"updater")
        (self.program / "updater" / "_internal").mkdir()
        (self.program / "updater" / "_internal" / "lib.dll").write_bytes(b"dll")
        self.data = self.tmp / "data"
        self.api = nextpull.NextPullApi(self.data, poll_seconds=0.05)
        self.addCleanup(self.api.shutdown)
        self.api._frozen = True
        self.api._program_dir = self.program
        self.api._quit_delay = 0.01
        self.spawned: list[tuple[Path, Path]] = []
        self.api._spawn_updater = lambda exe, request: self.spawned.append((exe, request))
        self.api._update = {"checked": "now", "error": "", "latest": updates.summarize(release())}
        self.quit_called = threading.Event()
        self.api.quit_app = lambda: self.quit_called.set() or {"ok": True}   # type: ignore[method-assign]

    def test_the_banner_says_it_can_install_only_in_the_packaged_app(self) -> None:
        self.assertTrue(self.api.status()["update"]["can_install"])
        self.api._frozen = False
        self.assertFalse(self.api.status()["update"]["can_install"])
        self.api._frozen = True
        (self.program / "updater" / "NextPullUpdater.exe").unlink()
        self.assertFalse(self.api.status()["update"]["can_install"])

    def test_install_hands_over_to_the_updater_and_quits(self) -> None:
        result = self.api.install_update()
        self.assertTrue(result["ok"], result)
        self.assertTrue(self.quit_called.wait(2))
        exe, request_file = self.spawned[0]
        self.assertEqual(exe.name, "NextPullUpdater.exe")
        self.assertTrue(exe.is_file() and (exe.parent / "_internal" / "lib.dll").is_file(), "the updater runs from a copy in the data folder")
        self.assertEqual(exe.parent.parent.name, "updater-run")
        self.assertTrue(str(exe).startswith(str(self.data)))
        request = json.loads(request_file.read_text(encoding="utf-8"))
        self.assertEqual((request["version"], request["current_version"]), ("9.9.9", nextpull.__version__))
        self.assertEqual(request["app_dir"], str(self.program))
        self.assertEqual(request["sha256"], "cd" * 32)
        self.assertEqual(request["pid"], os.getpid())
        self.assertEqual(request["launch_args"], ["--data-dir", str(self.data)], "the new version reopens the same data folder")
        self.assertNotIn("password", json.dumps(request).lower())

    def test_install_waits_for_a_running_download_unless_forced(self) -> None:
        with mock.patch.object(self.api._engine, "is_busy", return_value=True):
            refused = self.api.install_update()
            self.assertFalse(refused["ok"])
            self.assertTrue(refused["busy"])
            self.assertEqual(self.spawned, [])
            self.assertTrue(self.api.install_update(True)["ok"])
        self.assertEqual(len(self.spawned), 1)

    def test_install_refuses_when_there_is_nothing_to_install(self) -> None:
        self.api._update = {"checked": "", "error": "", "latest": None}
        self.assertFalse(self.api.install_update()["ok"])
        self.api._update = {"checked": "", "error": "", "latest": updates.summarize(release(f"v{nextpull.__version__}"))}
        self.assertFalse(self.api.install_update()["ok"])
        self.api._update = {"checked": "", "error": "", "latest": {**updates.summarize(release()), "asset": None}}
        self.assertIn("no download", self.api.install_update()["error"])
        self.assertEqual(self.spawned, [])
        self.assertFalse(self.quit_called.is_set())

    def test_install_refuses_from_source(self) -> None:
        self.api._frozen = False
        result = self.api.install_update()
        self.assertFalse(result["ok"])
        self.assertIn("release page", result["error"])

    def test_a_missing_updater_is_explained_and_nothing_quits(self) -> None:
        (self.program / "updater" / "NextPullUpdater.exe").unlink()
        self.assertFalse(self.api.install_update()["ok"])
        self.assertFalse(self.quit_called.wait(0.2))

    def test_the_result_of_an_update_shows_once_then_can_be_dismissed(self) -> None:
        self.assertIsNone(self.api.status()["update_result"])
        (self.data).mkdir(exist_ok=True)
        (self.data / installer.RESULT_FILE).write_text(json.dumps({"ok": False, "version": "0.3.0", "previous_version": "0.2.0", "message": "boom", "rolled_back": True, "restore_failed": False, "cancelled": False, "backup": "x", "step": "restart"}))
        shown = self.api.status()["update_result"]
        self.assertEqual((shown["version"], shown["rolled_back"], shown["message"]), ("0.3.0", True, "boom"))
        self.assertNotIn("step", shown)
        self.api.dismiss_update_result()
        self.assertIsNone(self.api.status()["update_result"])

    def test_leftovers_of_an_update_are_cleaned_at_start(self) -> None:
        for name in ("updater-run", "update"):
            (self.data / name / "x").mkdir(parents=True)
        for suffix in (".new", ".unpack", ".failed"):
            (self.program.with_name("program" + suffix)).mkdir()
        keep = self.program.with_name("program.previous")
        keep.mkdir()
        self.api._clean_update_leftovers()
        self.assertFalse((self.data / "updater-run").exists() or (self.data / "update").exists())
        self.assertFalse(any(self.program.with_name("program" + suffix).exists() for suffix in (".new", ".unpack", ".failed")))
        self.assertTrue(keep.is_dir(), "the previous version is kept for a manual rollback")

    def test_backups_folder_opens_only_when_it_exists(self) -> None:
        self.assertFalse(self.api.open_backups_folder()["ok"])
        (self.data / "backups").mkdir()
        with mock.patch("nextpull.winutil.open_path") as opened:
            self.assertTrue(self.api.open_backups_folder()["ok"])
            opened.assert_called_once()


class SelfCheck(TempCase):
    def test_a_healthy_program_reports_ok_and_its_version(self) -> None:
        report = self.tmp / "report.json"
        data = self.tmp / "scratch"
        data.mkdir()
        self.assertEqual(nextpull.main(["--self-check", str(report), "--data-dir", str(data)]), 0)
        result = json.loads(report.read_text(encoding="utf-8"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["version"], nextpull.__version__)

    def test_it_reads_a_copy_of_real_settings_without_changing_them(self) -> None:
        data = self.tmp / "scratch"
        data.mkdir()
        (data / "jobs.json").write_text(json.dumps({"jobs": []}), encoding="utf-8")
        before = (data / "jobs.json").read_bytes()
        report = self.tmp / "r.json"
        self.assertEqual(nextpull.self_check(report, data), 0)
        self.assertEqual((data / "jobs.json").read_bytes(), before)

    def test_a_broken_install_is_reported_not_raised(self) -> None:
        report = self.tmp / "r.json"
        with mock.patch("nextpull.winutil.app_dir", return_value=self.tmp / "nowhere"):
            code = nextpull.self_check(report, self.tmp)
        self.assertEqual(code, 1)
        result = json.loads(report.read_text(encoding="utf-8"))
        self.assertFalse(result["ok"])
        self.assertTrue(any("index.html" in problem for problem in result["problems"]))


if __name__ == "__main__":
    unittest.main()
