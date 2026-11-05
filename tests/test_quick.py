"""Quick downloads (a file or folder picked on the Files page, fetched right now): job building, conflicts, the real rclone, the engine queue,
the pause switch and the bridge."""
from __future__ import annotations

import os
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import helpers
from helpers import ROOT, TempCase, make_job, write_file
from fake_nextcloud import FakeNextcloud
from nextpull import NextPullApi
from rclone_runner import RcloneRun, build_args, find_rclone
from store import ConfigStore, HistoryDb, SecretStore
import quick

RCLONE = find_rclone("", [ROOT])
NOW = datetime(2026, 11, 5, 10, 0, 0)


def item(path: str, is_dir: bool = False, size: int = 0) -> dict:
    return {"path": path, "is_dir": is_dir, "size": size}


class BuildingJobs(TempCase):
    def test_one_file_is_its_own_source(self) -> None:
        (job,) = quick.quick_jobs("nextcloud", [item("Series/ep 1.mkv", size=5)], "copy", str(self.tmp))
        self.assertEqual((job["source"], job["includes"], job["mode"], job["dest"], job["id"]), ("Series/ep 1.mkv", [], "copy", str(self.tmp), "quick"))
        self.assertEqual((job["min_age_minutes"], job["name"]), (0, "ep 1.mkv"))

    def test_several_files_of_one_folder_become_one_run_with_literal_includes(self) -> None:
        (job,) = quick.quick_jobs("sg-1", [item("Dir/a[1].mkv"), item("Dir/b*.mkv"), item("Dir/a[1].mkv")], "move", str(self.tmp))
        self.assertEqual((job["source"], job["mode"], job["connection"], job["name"]), ("Dir", "move", "sg-1", "2 files"))
        self.assertEqual(job["includes"], ["/a\\[1\\].mkv", "/b\\*.mkv"])   # duplicates dropped, every glob character escaped
        self.assertNotIn("../", " ".join(build_args(job, config_path=self.tmp / "c", rc_port=1, rc_pass="x", log_file=self.tmp / "l")))

    def test_files_in_the_root_and_in_other_folders_make_separate_runs(self) -> None:
        jobs = quick.quick_jobs("nextcloud", [item("a.mkv"), item("b.mkv"), item("X/c.mkv"), item("X/d.mkv")], "copy", str(self.tmp))
        self.assertEqual([(job["source"], len(job["includes"])) for job in jobs], [("", 2), ("X", 2)])

    def test_a_folder_goes_into_a_folder_of_its_own_name(self) -> None:
        (job,) = quick.quick_jobs("nextcloud", [item("Anime/Season: 1?", is_dir=True)], "copy", str(self.tmp / "out"))
        self.assertEqual((job["source"], job["includes"]), ("Anime/Season: 1?", []))
        self.assertEqual(Path(job["dest"]).parent, self.tmp / "out")
        self.assertNotRegex(Path(job["dest"]).name, r'[:?]')
        self.assertFalse(job["delete_empty_dirs"])
        (moved,) = quick.quick_jobs("nextcloud", [item("Anime/S1", is_dir=True)], "move", str(self.tmp))
        self.assertTrue(moved["delete_empty_dirs"])

    def test_nothing_is_lost_at_the_destination(self) -> None:
        keep = quick.quick_jobs("nextcloud", [item("a.mkv")], "copy", str(self.tmp), on_conflict="keep")[0]
        self.assertTrue(keep["ignore_existing"])
        self.assertIn("--ignore-existing", build_args(keep, config_path=self.tmp / "c", rc_port=1, rc_pass="x", log_file=self.tmp / "l"))
        replace = quick.quick_jobs("nextcloud", [item("a.mkv")], "copy", str(self.tmp), on_conflict="replace", backup_root=self.tmp / "replaced", now=NOW)[0]
        self.assertEqual(replace["backup_dir"], str(self.tmp / "replaced" / "20261105-100000"))
        args = build_args(replace, config_path=self.tmp / "c", rc_port=1, rc_pass="x", log_file=self.tmp / "l")
        self.assertEqual(args[args.index("--backup-dir") + 1], replace["backup_dir"])
        self.assertNotIn("--ignore-existing", args)

    def test_bad_input_is_refused_with_a_message(self) -> None:
        for args, text in [(([], "copy", "x"), "at least one"), (([item("a")], "sync", "x"), "Download"), (([item("a")], "copy", " "), "folder on this PC"),
                           (([item("../a")], "copy", "x"), ".."), (([item("")], "copy", "x"), "root"), (([item("a")], "copy", "x", "overwrite"), "already exist"),
                           (([item(f"f{n}") for n in range(quick.MAX_ITEMS + 1)], "copy", "x"), "at most")]:
            with self.assertRaises(ValueError, msg=text) as raised:
                quick.quick_jobs("nextcloud", *args)
            self.assertIn(text, str(raised.exception))

    def test_conflicts_are_files_that_exist_with_another_size(self) -> None:
        write_file(self.tmp / "same.mkv", 10)
        write_file(self.tmp / "other.mkv", 10)
        items = quick.clean_items([item("d/same.mkv", size=10), item("d/other.mkv", size=99), item("d/new.mkv", size=1), item("d/folder", True)])
        self.assertEqual([c["name"] for c in quick.find_conflicts(str(self.tmp), items)], ["other.mkv"])


@unittest.skipUnless(RCLONE, "real rclone not available")
class RealRclone(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.remote = self.tmp / "remote"
        self.dest = self.tmp / "dest"
        self.server = FakeNextcloud(self.remote, user="alice", password="app-pass-1234")
        self.base = self.server.start()
        self.addCleanup(self.server.stop)
        self.creds = {"server": self.base, "user": "alice", "app_password": "app-pass-1234"}
        self.history = HistoryDb(None)
        self.addCleanup(self.history.close)

    def go(self, job: dict) -> dict:
        run_id = self.history.start_run(job, "quick")
        return RcloneRun(rclone=RCLONE, job=job, creds=self.creds, run_id=run_id, history=self.history, log_path=self.tmp / "logs" / f"run-{run_id}.jsonl",
                         config_path=self.tmp / "rclone.empty.conf", keep_awake=False).run()

    def pull(self, items, mode="copy", **kwargs) -> list[dict]:
        return [self.go(job) for job in quick.quick_jobs("nextcloud", items, mode, str(self.dest), **kwargs)]

    def files(self, root: Path) -> list[str]:
        return sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*") if p.is_file())

    def test_one_file_is_copied_and_the_server_keeps_it(self) -> None:
        write_file(self.remote / "Series" / "ep 1.mkv", 700, fill=b"e")
        write_file(self.remote / "Series" / "ep 2.mkv", 800)
        (result,) = self.pull([item("Series/ep 1.mkv")])
        self.assertEqual((result["state"], result["files"], result["bytes"]), ("ok", 1, 700), result)
        self.assertEqual(self.files(self.dest), ["ep 1.mkv"])
        self.assertTrue((self.remote / "Series" / "ep 1.mkv").exists())
        self.assertNotIn("DELETE", {method for method, _ in self.server.requests})
        self.assertEqual(self.history.get_run(result["run_id"])["trigger"], "quick")

    def test_a_file_that_was_uploaded_a_second_ago_is_fetched_anyway(self) -> None:
        write_file(self.remote / "fresh.mkv", 100, age_seconds=1)
        (result,) = self.pull([item("fresh.mkv")])   # a scheduled job waits min_age minutes; the user asked for this one
        self.assertEqual((result["state"], result["files"]), ("ok", 1), result)

    def test_move_of_a_single_file_removes_only_that_file(self) -> None:
        write_file(self.remote / "Series" / "ep 1.mkv", 700)
        write_file(self.remote / "Series" / "ep 2.mkv", 800)
        (result,) = self.pull([item("Series/ep 1.mkv")], mode="move")
        self.assertEqual(result["state"], "ok", result)
        self.assertFalse((self.remote / "Series" / "ep 1.mkv").exists())
        self.assertTrue((self.remote / "Series" / "ep 2.mkv").exists())
        self.assertTrue((self.remote / "Series").is_dir())
        self.assertEqual(self.history.get_run(result["run_id"])["deleted"], 1)

    def test_a_group_takes_exactly_the_picked_names_even_with_glob_characters(self) -> None:
        for name in ("a[1].mkv", "{x}.mkv", "a1.mkv", "x.mkv", "other.mkv"):   # the decoys are what an unescaped [1] / {x} pattern would also match
            write_file(self.remote / "Dir" / name, 50)
        (result,) = self.pull([item("Dir/a[1].mkv"), item("Dir/{x}.mkv")])
        self.assertEqual(result["state"], "ok", result)
        self.assertEqual(self.files(self.dest), ["a[1].mkv", "{x}.mkv"])

    def test_a_file_in_the_root_of_the_server(self) -> None:
        write_file(self.remote / "top.mkv", 300)
        write_file(self.remote / "top2.mkv", 300)
        write_file(self.remote / "Dir" / "deep.mkv", 300)
        results = self.pull([item("top.mkv"), item("top2.mkv")])
        self.assertEqual([r["state"] for r in results], ["ok"], results)
        self.assertEqual(self.files(self.dest), ["top.mkv", "top2.mkv"])

    def test_a_folder_lands_in_a_folder_of_its_own_name(self) -> None:
        write_file(self.remote / "Anime" / "S1" / "e1.mkv", 100)
        write_file(self.remote / "Anime" / "S1" / "Sub" / "e2.mkv", 100)
        (result,) = self.pull([item("Anime/S1", is_dir=True)], mode="move")
        self.assertEqual(result["state"], "ok", result)
        self.assertEqual(self.files(self.dest / "S1"), ["Sub/e2.mkv", "e1.mkv"])
        self.assertTrue((self.remote / "Anime" / "S1").is_dir(), "the picked folder itself is never removed from the server")
        self.assertFalse((self.remote / "Anime" / "S1" / "Sub").exists())

    def test_keep_leaves_the_existing_file_and_the_server_copy_alone(self) -> None:
        write_file(self.remote / "a.mkv", 500, fill=b"N")
        write_file(self.dest / "a.mkv", 200, fill=b"O")
        self.pull([item("a.mkv")], mode="move", on_conflict="keep")
        self.assertEqual((self.dest / "a.mkv").read_bytes(), b"O" * 200)
        self.assertTrue((self.remote / "a.mkv").exists(), "a file that was not taken must not be deleted on the server")

    def test_replace_keeps_the_old_copy_in_the_backup_folder(self) -> None:
        write_file(self.remote / "a.mkv", 500, fill=b"N")
        write_file(self.dest / "a.mkv", 200, fill=b"O")
        (result,) = self.pull([item("a.mkv")], on_conflict="replace", backup_root=self.tmp / "replaced", now=NOW)
        self.assertEqual(result["state"], "ok", result)
        self.assertEqual((self.dest / "a.mkv").read_bytes(), b"N" * 500)
        self.assertEqual((self.tmp / "replaced" / "20261105-100000" / "a.mkv").read_bytes(), b"O" * 200)
        self.assertFalse(list(self.dest.rglob("*.partial")))

    def test_a_missing_file_fails_with_a_clear_reason(self) -> None:
        write_file(self.remote / "x.mkv", 10)
        (result,) = self.pull([item("gone.mkv")])
        self.assertEqual(result["state"], "failed", result)
        self.assertIn("not found", result["message"])


class EngineQueue(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = ConfigStore(None)
        self.history = HistoryDb(None)
        self.addCleanup(self.history.close)
        self.secrets = SecretStore(None)
        self.secrets.save("https://cloud.example.org", "alice", "SuperSecret-Pass-42")
        self.seen: list[dict] = []
        self.release = __import__("threading").Event()
        outer = self

        class FakeRun:
            def __init__(self, **kwargs) -> None:
                self.job, self.run_id, self.history = kwargs["job"], kwargs["run_id"], kwargs["history"]
                outer.seen.append({"job": self.job, "deadline": kwargs["deadline"]})

            def run(self) -> dict:
                outer.release.wait(5)
                self.history.finish_run(self.run_id, "ok", "done")
                return {"state": "ok", "message": "done", "files": 0, "bytes": 0}

            def snapshot(self) -> dict:
                return {"job_name": self.job["name"]}

            def cancel(self) -> None:
                outer.release.set()

        from engine import Engine
        self.engine = Engine(self.config, self.history, self.secrets, logs_dir=self.tmp / "logs", rclone_finder=lambda: ["rclone"], clock=lambda: NOW, tick_seconds=10, run_factory=FakeRun)
        self.addCleanup(self.engine.stop)
        self.addCleanup(self.release.set)

    def wait_idle(self) -> None:
        for _ in range(100):
            if not self.engine.is_busy():
                return
            time.sleep(0.05)
        self.fail("engine busy")

    def test_two_quick_downloads_queue_behind_each_other_and_are_recorded_as_quick(self) -> None:
        jobs = quick.quick_jobs("nextcloud", [item("a/x.mkv"), item("b/y.mkv")], "copy", str(self.tmp / "dest"))
        first = self.engine.fetch_now(jobs)
        self.assertEqual((first["ok"], first["queued"]), (True, 2))
        time.sleep(0.2)
        self.assertEqual(len(self.engine.status()["queue"]), 1, "the second one waits: one run at a time")
        self.assertEqual(self.engine.status()["queue"][0]["trigger"], "quick")
        self.release.set()
        self.wait_idle()
        runs = sorted(self.history.list_runs(), key=lambda run: run["id"])
        self.assertEqual([(run["job_id"], run["trigger"], run["job_name"]) for run in runs], [("quick", "quick", "x.mkv"), ("quick", "quick", "y.mkv")])
        self.assertTrue(all(entry["deadline"] is None for entry in self.seen), "a quick download has no stop-by time")

    def test_quick_downloads_behind_a_running_job_report_who_they_wait_for(self) -> None:
        job = self.config.save_job(make_job(dest=str(self.tmp / "d"), name="Night pull"))
        self.engine.run_now(job["id"])
        time.sleep(0.2)
        answer = self.engine.fetch_now(quick.quick_jobs("nextcloud", [item("a.mkv")], "copy", str(self.tmp / "d")))
        self.assertEqual(answer["queued_behind"], "Night pull")
        self.release.set()
        self.wait_idle()
        self.assertEqual(len(self.history.list_runs()), 2)

    def test_pause_skips_scheduled_slots_and_records_them_but_not_quick_downloads(self) -> None:
        job = self.config.save_job(make_job(dest=str(self.tmp / "d"), schedule={"days": [0, 1, 2, 3, 4, 5, 6], "time": "02:00", "stop_by": ""}))
        self.config.save_settings({**self.config.settings(), "paused": True})
        self.config.set_state("last_checked", datetime(2026, 11, 5, 1, 59, 0).isoformat())
        self.engine.tick(datetime(2026, 11, 5, 2, 0, 5))
        runs = self.history.list_runs()
        self.assertEqual([(r["state"], r["message"]) for r in runs], [("skipped", "Skipped: scheduled downloads were paused")])
        self.assertEqual(self.seen, [])
        self.release.set()
        self.engine.fetch_now(quick.quick_jobs("nextcloud", [item("a.mkv")], "copy", str(self.tmp / "d")))
        self.wait_idle()
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(job["id"] != "quick", True)


@unittest.skipUnless(RCLONE, "real rclone not available")
class Bridge(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.remote = self.tmp / "remote"
        write_file(self.remote / "Series" / "ep 1.mkv", 2000, fill=b"z")
        os.environ["FAKE_REMOTE_ROOT"] = str(self.remote)
        self.server = FakeNextcloud(self.remote)
        self.base = self.server.start()
        self.addCleanup(self.server.stop)
        self.api = NextPullApi(None, poll_seconds=0.05)
        self.api._engine.rclone_finder = lambda: RCLONE
        self.api._config.save_settings({"prevent_sleep": False})
        self.addCleanup(self.api.shutdown)
        self.dest = self.tmp / "out"

    def connect(self) -> None:
        with mock.patch("nextpull.webbrowser.open"):
            self.api.connect_start(self.base)
        self.server.approved.set()
        for _ in range(100):
            if self.api.connect_status()["state"] == "connected":
                return
            time.sleep(0.05)
        self.fail("not connected")

    def wait_idle(self) -> None:
        for _ in range(300):
            if not self.api._engine.is_busy():
                return
            time.sleep(0.1)
        self.fail("engine busy")

    def test_fetch_now_downloads_one_file_and_it_shows_in_history_and_the_dashboard(self) -> None:
        self.connect()
        info = self.api.quick_info("nextcloud", [item("Series/ep 1.mkv", size=2000)], str(self.dest))
        self.assertEqual((info["ok"], info["conflicts"], info["dest"]), (True, [], str(self.dest)))
        answer = self.api.fetch_now("nextcloud", [item("Series/ep 1.mkv", size=2000)], "copy", str(self.dest))
        self.assertTrue(answer["ok"], answer)
        self.wait_idle()
        self.assertEqual((self.dest / "ep 1.mkv").read_bytes(), b"z" * 2000)
        board = self.api.dashboard()
        self.assertEqual((board["ok"], board["today"]["files"], board["today"]["bytes"], len(board["daily"])), (True, 1, 2000, 14))
        self.assertEqual([(f["name"], f["job_name"], f["trigger"]) for f in board["recent"]], [("ep 1.mkv", "ep 1.mkv", "quick")])
        self.assertEqual(self.api.history()["runs"][0]["job_id"], "quick")

    def test_move_asks_the_server_to_delete_only_after_the_download(self) -> None:
        self.connect()
        self.assertTrue(self.api.fetch_now("nextcloud", [item("Series/ep 1.mkv")], "move", str(self.dest))["ok"])
        self.wait_idle()
        self.assertTrue((self.dest / "ep 1.mkv").exists())
        self.assertFalse((self.remote / "Series" / "ep 1.mkv").exists())

    def test_it_refuses_clearly_without_a_login_or_with_bad_input(self) -> None:
        self.assertIn("not connected", self.api.fetch_now("nextcloud", [item("a")], "copy", str(self.dest))["error"].lower())
        self.connect()
        self.assertFalse(self.api.fetch_now("nextcloud", [], "copy", str(self.dest))["ok"])
        self.assertFalse(self.api.fetch_now("nextcloud", [item("a")], "delete", str(self.dest))["ok"])
        self.assertFalse(self.api.fetch_now("sg-missing", [item("a")], "copy", str(self.dest))["ok"])

    def test_default_folder_follows_settings_then_a_job_then_downloads(self) -> None:
        self.assertTrue(self.api.quick_info()["dest"].endswith("NextPull"))
        self.api._config.save_job(make_job(dest=str(self.tmp / "jobdest")))
        self.assertEqual(self.api.quick_info()["dest"], str(self.tmp / "jobdest"))
        self.api.save_settings({"quick_dest": str(self.tmp / "mine")})
        self.assertEqual(self.api.quick_info()["dest"], str(self.tmp / "mine"))

    def test_pause_switch_round_trip_and_health(self) -> None:
        self.assertEqual(self.api.set_paused(True), {"ok": True, "paused": True})
        self.assertTrue(self.api.status()["paused"])
        self.api.set_paused(False)
        self.assertFalse(self.api.status()["paused"])
        self.connect()
        servers = self.api.server_health(force=True)["servers"]
        self.assertEqual([(s["id"], s["ok"]) for s in servers], [("nextcloud", True)])
        self.server.stop()
        down = self.api.server_health(force=True)["servers"]
        self.assertFalse(down[0]["ok"])
        self.assertTrue(down[0]["error"])


class _MeServer:
    """A tiny stand-in for NextPush's /.well-known/nextpush/me (and nothing else)."""

    def __init__(self, answer) -> None:   # noqa: ANN001
        import json
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        outer = self
        self.seen: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_GET(self) -> None:
                outer.seen.append({"path": self.path, "headers": dict(self.headers)})
                status, payload = answer if self.path == "/.well-known/nextpush/me" else (404, {})
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class NextPushCapabilities(TempCase):
    """Move is only offered when the server says the account may delete; every other server stays "unknown" and keeps Move."""

    def api_with(self, answer):   # noqa: ANN001
        server = _MeServer(answer)
        self.addCleanup(server.stop)
        api = NextPullApi(None, poll_seconds=0.05)
        self.addCleanup(api.shutdown)
        api._engine.rclone_finder = lambda: ["rclone"]
        api._secrets.save_connection({"id": "sg-1", "type": "sftpgo", "label": "Home", "server": server.url + "/dav", "user": "bob", "password": "p@ss/word", "header_name": "X-Pull-Key", "header_value": "k3y"})
        return api, server

    def test_a_read_only_folder_blocks_move_but_not_download(self) -> None:
        api, server = self.api_with((200, {"nextpush": 1, "user": "bob", "can_delete": True, "folders": [{"name": "Incoming", "mode": "pull"}, {"name": "Anime", "mode": "read"}]}))
        info = api.quick_info("sg-1", [item("Anime/ep1.mkv"), item("Incoming/x.mkv")], str(self.tmp))
        self.assertEqual(info["move"], {"known": True, "allowed": False, "blocked": ["Anime"]})
        self.assertEqual(api.quick_info("sg-1", [item("Incoming/x.mkv")], str(self.tmp))["move"], {"known": True, "allowed": True, "blocked": []})
        refused = api.fetch_now("sg-1", [item("Anime/ep1.mkv")], "move", str(self.tmp))
        self.assertFalse(refused["ok"])
        self.assertIn("Anime", refused["error"])
        self.assertIn("Download instead", refused["error"])
        seen = server.seen[0]
        self.assertTrue(seen["headers"]["Authorization"].startswith("Basic "))
        self.assertEqual(seen["headers"]["X-Pull-Key"], "k3y", "the access key travels with the question, like on every other request")

    def test_an_answer_that_cannot_be_used_means_unknown_and_move_stays_available(self) -> None:
        for answer in ((404, {}), (200, {"nope": 1}), (200, {"folders": "x"}), (200, {"folders": [], "can_delete": None}), (500, {})):
            api, _ = self.api_with(answer)
            self.assertEqual(api.quick_info("sg-1", [item("Anime/a.mkv")], str(self.tmp))["move"], {"known": False, "allowed": True, "blocked": []}, answer)

    def test_nextcloud_connections_are_never_asked(self) -> None:
        api = NextPullApi(None)
        self.addCleanup(api.shutdown)
        api._secrets.save("https://cloud.example.org", "alice", "SuperSecret-Pass-42")
        self.assertEqual(api._move_check("nextcloud", [item("a.mkv")]), {"known": False, "allowed": True, "blocked": []})


if __name__ == "__main__":
    unittest.main()
