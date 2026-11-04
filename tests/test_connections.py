"""Connections: Nextcloud (the original login) next to SFTPGo / plain WebDAV servers with a user name, a password and an optional access-key header."""
from __future__ import annotations

import os
import time
import unittest
from pathlib import Path

import applog
import connections
import diagnostics
import helpers
from connections import (clean_connection, clean_header, discover, make_invite, normalize_webdav_url, parse_invite, public_view)
from fake_nextcloud import FakeNextcloud
from helpers import FAKE_RCLONE, ROOT, TempCase, make_job, write_file
from nc_client import NextcloudError, NextcloudSession, WebdavSession
from nextpull import NextPullApi
from rclone_runner import RcloneRun, build_args, build_env, find_rclone
from store import HistoryDb, SecretStore, clean_job

RCLONE = find_rclone("", [ROOT])
PASSWORD = "sg-pass-9876-long-and-random"
KEY = ("X-Pull-Key", "ak-7f3c9e21d4b8")


def dpapi_stand_in() -> dict:
    return {"protect": lambda data: b"ENC:" + data[::-1], "unprotect": lambda blob: blob[4:][::-1]}


class Addresses(unittest.TestCase):
    def test_https_is_the_default_and_paths_are_kept(self) -> None:
        self.assertEqual(normalize_webdav_url("pull.example.com/"), "https://pull.example.com")
        self.assertEqual(normalize_webdav_url("https://pull.example.com/dl-x7/"), "https://pull.example.com/dl-x7")

    def test_plain_http_only_inside_the_local_network(self) -> None:
        for fine in ("http://192.168.1.10:8092", "http://localhost:8092", "http://10.0.0.5", "http://nas", "http://pi.local:8092"):
            self.assertTrue(normalize_webdav_url(fine).startswith("http://"), fine)
        for bad in ("http://pull.example.com", "http://8.8.8.8", "http://93.184.216.34:8092"):
            with self.assertRaisesRegex(NextcloudError, "https://"):
                normalize_webdav_url(bad)

    def test_garbage_is_refused(self) -> None:
        for bad in ("", "   ", "ftp://x.example.com", "://"):
            with self.assertRaises(NextcloudError):
                normalize_webdav_url(bad)


class Records(unittest.TestCase):
    def test_header_rules(self) -> None:
        self.assertEqual(clean_header("", ""), ("", ""))
        self.assertEqual(clean_header(" X-Pull-Key ", " abc "), ("X-Pull-Key", "abc"))
        for name, value in (("bad name", "v"), ("X-Key", ""), ("X-Key", "a,b"), ("X-Key", "line\nbreak"), ("X-Key", "x" * 300), ("", "v")):
            with self.assertRaises(ValueError):
                clean_header(name, value)

    def test_a_record_needs_user_password_and_a_safe_address(self) -> None:
        record = clean_connection({"server": "pull.example.com", "user": " ben ", "password": PASSWORD, "header_name": KEY[0], "header_value": KEY[1], "label": ""})
        self.assertEqual((record["server"], record["user"], record["type"]), ("https://pull.example.com", "ben", "sftpgo"))
        self.assertEqual(record["label"], "pull.example.com (ben)")
        self.assertTrue(record["id"].startswith("sg-"))
        for broken in ({"server": "pull.example.com", "user": "", "password": "x"}, {"server": "pull.example.com", "user": "ben", "password": ""}, {"server": "http://pull.example.com", "user": "b", "password": "x"},
                       {"server": "pull.example.com", "user": "b", "password": "x", "id": "nextcloud"}, {"server": "pull.example.com", "user": "b", "password": "x", "id": "../x"}):
            with self.assertRaises((ValueError, NextcloudError)):
                clean_connection(broken)

    def test_the_public_view_has_no_secrets(self) -> None:
        record = clean_connection({"server": "pull.example.com", "user": "ben", "password": PASSWORD, "header_name": KEY[0], "header_value": KEY[1]})
        text = repr(public_view(record))
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn(KEY[1], text)
        self.assertTrue(public_view(record)["has_access_key"])
        self.assertEqual(set(connections.secrets_of(record)), {PASSWORD, KEY[1]})

    def test_old_jobs_without_a_connection_are_nextcloud_jobs(self) -> None:
        self.assertEqual(clean_job({"name": "x", "source": "a", "dest": "D:\\x"})["connection"], "nextcloud")
        self.assertEqual(clean_job({"name": "x", "source": "a", "dest": "D:\\x", "connection": "sg-1a2b3c"})["connection"], "sg-1a2b3c")
        self.assertEqual(clean_job({"name": "x", "source": "a", "dest": "D:\\x", "connection": "../../evil"})["connection"], "nextcloud")


class Invites(unittest.TestCase):
    def test_round_trip_and_no_password_inside(self) -> None:
        code = make_invite("pull.example.com", "ben", KEY[0], KEY[1], "Home server")
        self.assertTrue(code.startswith("nextpull-invite:"))
        self.assertNotIn(PASSWORD, code)
        self.assertEqual(parse_invite(code), {"server": "https://pull.example.com", "user": "ben", "label": "Home server", "header_name": KEY[0], "header_value": KEY[1]})
        self.assertEqual(parse_invite(make_invite("https://pull.example.com"))["header_name"], "")

    def test_damaged_codes_are_refused(self) -> None:
        for bad in ("", "hello", "nextpull-invite:", "nextpull-invite:%%%", "nextpull-invite:eyJ2IjoyfQ", "  nextpull-invite:e30"):
            with self.assertRaises(ValueError):
                parse_invite(bad)


class Store(TempCase):
    def test_connections_persist_encrypted_and_coexist_with_the_nextcloud_login(self) -> None:
        store = SecretStore(self.tmp, **dpapi_stand_in())
        store.save("https://cloud.example.org", "alice", "nc-app-pass-1234")
        record = clean_connection({"server": "pull.example.com", "user": "ben", "password": PASSWORD, "header_name": KEY[0], "header_value": KEY[1]})
        store.save_connection(record)
        raw = (self.tmp / "connections.bin").read_bytes()
        self.assertNotIn(PASSWORD.encode(), raw)
        self.assertNotIn(b"pull.example.com", raw)
        again = SecretStore(self.tmp, **dpapi_stand_in())
        self.assertEqual(again.creds_for(record["id"])["password"], PASSWORD)
        self.assertEqual(again.creds_for("nextcloud")["type"], "nextcloud")
        self.assertEqual(again.creds_for(None)["user"], "alice")
        self.assertIsNone(again.creds_for("sg-nope"))
        self.assertTrue(again.delete_connection(record["id"]))
        self.assertFalse(again.delete_connection(record["id"]))
        self.assertEqual(SecretStore(self.tmp, **dpapi_stand_in()).connections(), [])

    def test_a_corrupt_file_means_no_connections_not_a_crash(self) -> None:
        (self.tmp / "connections.bin").write_bytes(b"garbage")
        self.assertEqual(SecretStore(self.tmp, **dpapi_stand_in()).connections(), [])

    def test_memory_only_without_a_data_dir(self) -> None:
        store = SecretStore(None)
        store.save_connection(clean_connection({"server": "pull.example.com", "user": "b", "password": "x", "id": "sg-mem"}))
        self.assertEqual(store.creds_for("sg-mem")["user"], "b")
        self.assertEqual(SecretStore(None).connections(), [])


class WebdavCase(TempCase):
    prefix = "/dl-x7"

    def setUp(self) -> None:
        super().setUp()
        self.remote = self.tmp / "remote"
        write_file(self.remote / "Incoming" / "Ranma ½" / "Episodio 01 [BDRip].mkv", 2000)
        write_file(self.remote / "Incoming" / "È quasi magia Johnny" / "film (1989).mkv", 1500)
        write_file(self.remote / "Incoming" / "Anime" / "Sub" / "ep 2.mkv", 800)
        write_file(self.remote / "Incoming" / "it's a file (1).mkv", 700)
        os.environ["FAKE_REMOTE_ROOT"] = str(self.remote)
        self.server = FakeNextcloud(self.remote, user="ben", password=PASSWORD, dav_root=True, base_path=self.prefix, require_header=KEY)
        self.url = self.server.start() + self.prefix
        self.addCleanup(self.server.stop)

    def creds(self, **extra) -> dict:
        return {"type": "sftpgo", "id": "sg-test01", "label": "Home", "server": self.url, "user": "ben", "password": PASSWORD, "header_name": KEY[0], "header_value": KEY[1], **extra}


class SessionAndGate(WebdavCase):
    def test_listing_with_the_access_key(self) -> None:
        session = connections.make_session(self.creds())
        self.assertIsInstance(session, WebdavSession)
        self.assertEqual([entry.name for entry in session.list_dir("")], ["Incoming"])
        self.assertEqual([entry.name for entry in session.list_dir("Incoming")], ["Anime", "Ranma ½", "È quasi magia Johnny", "it's a file (1).mkv"])
        self.assertEqual([entry.path for entry in session.list_dir("Incoming/Ranma ½")], ["Incoming/Ranma ½/Episodio 01 [BDRip].mkv"])
        self.assertTrue(session.check_folder("Incoming"))
        self.assertFalse(session.check_folder("Incoming/it's a file (1).mkv"))
        self.assertEqual(session.user_info()["display_name"], "ben")

    def test_strangers_get_no_answer_at_all(self) -> None:
        for creds in (self.creds(header_name="", header_value=""), self.creds(header_value="wrong")):
            with self.assertRaisesRegex(NextcloudError, "Cannot reach the server"):
                connections.make_session(creds).list_dir("")
        self.assertGreaterEqual(self.server.gated, 2)

    def test_wrong_password_and_missing_folder_have_server_wording(self) -> None:
        with self.assertRaisesRegex(NextcloudError, "The server rejected the saved login. Edit the connection"):
            connections.make_session(self.creds(password="wrong")).list_dir("")
        with self.assertRaisesRegex(NextcloudError, "The server folder /Nope was not found"):
            connections.make_session(self.creds()).check_folder("Nope")
        with self.assertRaisesRegex(NextcloudError, "Folder not found"):
            connections.make_session(self.creds()).list_dir("Nope")

    def test_nothing_outside_the_prefix_is_served(self) -> None:
        creds = self.creds(server=self.url.removesuffix(self.prefix))
        with self.assertRaises(NextcloudError):
            connections.make_session(creds).check_folder("Incoming")

    def test_the_client_only_reads(self) -> None:
        session = connections.make_session(self.creds())
        session.list_dir("Incoming")
        session.check_folder("Incoming")
        self.assertTrue({method for method, _ in self.server.requests} <= {"PROPFIND"}, self.server.requests)


class Discovery(WebdavCase):
    def test_a_webdav_server_is_recognised_and_named(self) -> None:
        gate_free = FakeNextcloud(self.remote, user="ben", password=PASSWORD, dav_root=True)
        base = gate_free.start()
        self.addCleanup(gate_free.stop)
        found = discover(base, timeout=5)
        self.assertEqual((found["ok"], found["type"], found["product"], found["version"]), (True, "sftpgo", "SFTPGo", "2.7.6"))

    def test_nextcloud_is_recognised(self) -> None:
        server = FakeNextcloud(self.remote)
        base = server.start()
        self.addCleanup(server.stop)
        found = discover(base, timeout=5)
        self.assertEqual((found["ok"], found["type"]), (True, "nextcloud"))
        self.assertEqual(found["version"], "34.0.0")

    def test_a_gated_server_is_unknown_with_a_hint(self) -> None:
        found = discover(self.url.removesuffix(self.prefix), timeout=5)
        self.assertEqual((found["ok"], found["type"]), (False, "unknown"))
        self.assertIn("invite", found["hint"])

    def test_nothing_listening_and_bad_input(self) -> None:
        self.assertEqual(discover("http://127.0.0.1:1", timeout=2)["type"], "unknown")
        self.assertFalse(discover("http://example.com")["ok"])   # plain http to a public host is refused before any request


class RunnerPieces(unittest.TestCase):
    def test_env_for_a_webdav_connection(self) -> None:
        creds = {"type": "sftpgo", "server": "https://pull.example.com/dl-x7", "user": "ben", "password": PASSWORD, "header_name": KEY[0], "header_value": KEY[1]}
        env = build_env(creds, "OBSCURED", base={})
        self.assertEqual(env["RCLONE_CONFIG_NC_URL"], "https://pull.example.com/dl-x7")
        self.assertEqual((env["RCLONE_CONFIG_NC_VENDOR"], env["RCLONE_CONFIG_NC_USER"], env["RCLONE_CONFIG_NC_PASS"]), ("other", "ben", "OBSCURED"))
        self.assertEqual(env["RCLONE_CONFIG_NC_HEADERS"], "X-Pull-Key,ak-7f3c9e21d4b8")
        self.assertNotIn("RCLONE_CONFIG_NC_HEADERS", build_env({**creds, "header_name": "", "header_value": ""}, "O", base={}))
        nextcloud = build_env({"type": "nextcloud", "server": "https://cloud.example.org", "user": "alice", "app_password": "x"}, "O", base={})
        self.assertEqual((nextcloud["RCLONE_CONFIG_NC_VENDOR"], nextcloud["RCLONE_CONFIG_NC_URL"].endswith("/files/alice")), ("nextcloud", True))

    def test_such_a_login_does_not_try_to_remove_folders(self) -> None:
        job = make_job(dest="D:\\x", mode="move", delete_empty_dirs=True)
        kwargs = dict(config_path=Path("c"), rc_port=1, rc_pass="p", log_file=Path("l"))
        self.assertIn("--delete-empty-src-dirs", build_args(job, **kwargs))
        self.assertNotIn("--delete-empty-src-dirs", build_args(job, remove_empty_dirs=False, **kwargs))


@unittest.skipUnless(RCLONE, "real rclone not available (run tools/Fetch-rclone.ps1)")
class RealRcloneAgainstWebdav(WebdavCase):
    def run_job(self, creds: dict | None = None, **overrides) -> tuple[dict, HistoryDb]:
        history = HistoryDb(None)
        self.addCleanup(history.close)
        job = make_job(dest=str(self.tmp / "dest"), source="Incoming", min_age_minutes=0, **overrides)
        run_id = history.start_run(job, "manual")
        run = RcloneRun(rclone=RCLONE, job=job, creds=creds or self.creds(), run_id=run_id, history=history, log_path=self.tmp / "logs" / f"r{run_id}.jsonl",
                        config_path=self.tmp / "rclone.empty.conf", keep_awake=False)
        return run.run(), history

    def test_copy_with_unicode_names_basic_auth_prefix_and_access_key(self) -> None:
        result, _ = self.run_job()
        self.assertEqual(result["state"], "ok", result)
        dest = self.tmp / "dest"
        self.assertTrue((dest / "Ranma ½" / "Episodio 01 [BDRip].mkv").is_file())
        self.assertTrue((dest / "È quasi magia Johnny" / "film (1989).mkv").is_file())
        self.assertTrue((dest / "it's a file (1).mkv").is_file())
        self.assertTrue((self.remote / "Incoming" / "Anime" / "Sub" / "ep 2.mkv").exists(), "copy must leave the source alone")

    def test_move_deletes_files_keeps_the_job_folder_and_does_not_need_folder_rights(self) -> None:
        result, history = self.run_job(mode="move")
        self.assertEqual(result["state"], "ok", result)
        self.assertEqual(result["files"], 4)
        self.assertEqual([p for p in (self.remote / "Incoming").rglob("*") if p.is_file()], [])   # every source file is gone
        self.assertTrue((self.remote / "Incoming").is_dir(), "the job's own folder is never removed")
        self.assertTrue((self.tmp / "dest" / "Anime" / "Sub" / "ep 2.mkv").is_file())

    def test_a_stranger_gets_a_clear_failure_and_nothing_is_touched(self) -> None:
        result, _ = self.run_job(creds=self.creds(header_value="wrong"), mode="move")
        self.assertEqual(result["state"], "failed", result)
        self.assertIn("Cannot reach the server", result["message"])
        self.assertNotIn("Nextcloud", result["message"])
        self.assertEqual(len([p for p in (self.remote / "Incoming").rglob("*.mkv")]), 4)

    def test_wrong_password_is_reported_with_the_server_wording(self) -> None:
        result, _ = self.run_job(creds=self.creds(password="nope"))
        self.assertEqual(result["state"], "failed")
        self.assertIn("The server rejected the saved login", result["message"])

    def test_a_failed_delete_is_noted_for_the_server_not_nextcloud_and_the_next_run_does_not_download_again(self) -> None:
        self.server.forbid_delete = {"it's a file (1).mkv"}
        first, history = self.run_job(mode="move")
        self.assertIn(first["state"], ("partial", "failed"), first)
        self.assertTrue((self.remote / "Incoming" / "it's a file (1).mkv").exists())
        self.assertIn("on the server", first["message"])
        self.assertNotIn("Nextcloud", first["message"])
        self.server.forbid_delete = set()
        self.server.requests.clear()
        second, _ = self.run_job(mode="move")
        self.assertEqual(second["state"], "ok", second)
        self.assertFalse((self.remote / "Incoming" / "it's a file (1).mkv").exists())
        self.assertFalse([1 for method, path in self.server.requests if method == "GET" and "a%20file" in path], "the file must not be downloaded a second time")

    def test_the_secrets_never_reach_the_logs(self) -> None:
        applog.setup_logging(None)
        self.addCleanup(applog.teardown_logging)
        self.run_job()
        text = "\n".join(f"{e['name']} {e['message']}" for e in applog.read_log(None, 5000))
        log_text = "".join(p.read_text(encoding="utf-8", errors="replace") for p in (self.tmp / "logs").glob("*.jsonl"))
        for secret in (PASSWORD, KEY[1]):
            self.assertNotIn(secret, text)
            self.assertNotIn(secret, log_text)


class ApiCase(WebdavCase):
    def setUp(self) -> None:
        super().setUp()
        self.nc_remote = self.tmp / "nc"
        write_file(self.nc_remote / "EXTRACTED" / "a.mkv", 1000)
        self.nc = FakeNextcloud(self.nc_remote)
        self.nc_base = self.nc.start()
        self.addCleanup(self.nc.stop)
        self.api = NextPullApi(None, poll_seconds=0.05)
        self.api._engine.rclone_finder = lambda: FAKE_RCLONE
        self.addCleanup(self.api.shutdown)

    def record(self, **extra) -> dict:
        return {"server": self.url, "user": "ben", "password": PASSWORD, "header_name": KEY[0], "header_value": KEY[1], "label": "Home server", **extra}

    def add(self, **extra) -> str:
        result = self.api.add_connection(self.record(**extra))
        self.assertTrue(result["ok"], result)
        return result["connection"]["id"]


class Api(ApiCase):
    def test_a_connection_is_tested_before_it_is_saved(self) -> None:
        for bad in (self.record(password="wrong"), self.record(header_value="wrong"), self.record(server="http://pull.example.com")):
            self.assertFalse(self.api.add_connection(bad)["ok"], bad)
        self.assertEqual(self.api.list_connections()["connections"], [])
        conn_id = self.add()
        listed = self.api.list_connections()["connections"]
        self.assertEqual([c["id"] for c in listed], [conn_id])
        self.assertNotIn(PASSWORD, repr(listed))
        self.assertNotIn(KEY[1], repr(listed))
        self.assertTrue(self.api.status()["connected"])
        self.assertFalse(self.api.status()["nextcloud_connected"])

    def test_browse_each_connection_independently(self) -> None:
        conn_id = self.add()
        self.api._secrets.save(self.nc_base, "alice", "app-pass-1234")
        self.assertEqual([e["name"] for e in self.api.browse("", conn_id)["entries"]], ["Incoming"])
        self.assertEqual([e["name"] for e in self.api.browse("", "nextcloud")["entries"]], ["EXTRACTED"])
        self.assertEqual([e["name"] for e in self.api.browse("")["entries"]], ["EXTRACTED"])   # default is Nextcloud
        self.assertFalse(self.api.browse("", "sg-missing")["ok"])
        kinds = [c["type"] for c in self.api.list_connections()["connections"]]
        self.assertEqual(kinds, ["nextcloud", "sftpgo"])

    def test_jobs_keep_their_connection_and_block_its_removal(self) -> None:
        conn_id = self.add()
        job = self.api.save_job({"name": "Ben pull", "source": "Incoming", "dest": str(self.tmp / "d"), "connection": conn_id})["job"]
        self.assertEqual(job["connection"], conn_id)
        listed = [j for j in self.api.list_jobs()["jobs"] if j["id"] == job["id"]][0]
        self.assertEqual(listed["connection_label"], "Home server")
        refused = self.api.remove_connection(conn_id)
        self.assertFalse(refused["ok"])
        self.assertIn("Ben pull", refused["error"])
        self.api.delete_job(job["id"])
        self.assertTrue(self.api.remove_connection(conn_id)["ok"])
        self.assertFalse(self.api.remove_connection("nextcloud")["ok"])

    def test_discover_and_invite_through_the_bridge(self) -> None:
        plain = FakeNextcloud(self.remote, user="ben", password=PASSWORD, dav_root=True)
        base = plain.start()
        self.addCleanup(plain.stop)
        self.assertEqual(self.api.discover_server(base)["type"], "sftpgo")
        code = make_invite(self.url, "ben", KEY[0], KEY[1])
        filled = self.api.read_invite(code)
        self.assertTrue(filled["ok"])
        self.assertTrue(self.api.add_connection({**filled, "password": PASSWORD})["ok"])
        self.assertFalse(self.api.read_invite("nonsense")["ok"])

    def test_a_job_whose_connection_vanished_fails_with_a_clear_message(self) -> None:
        job = self.api.save_job({"name": "Orphan", "source": "Incoming", "dest": str(self.tmp / "d"), "connection": "sg-gone01"})["job"]
        self.api.run_job(job["id"])
        for _ in range(100):
            runs = self.api.history()["runs"]
            if runs and runs[0]["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(runs[0]["state"], "failed")
        self.assertIn("no longer exists", runs[0]["message"])

    def test_self_test_checks_every_connection_and_every_jobs_own_server(self) -> None:
        conn_id = self.add()
        self.api._secrets.save(self.nc_base, "alice", "app-pass-1234")
        self.api.save_job({"name": "Ben pull", "source": "Incoming", "dest": str(self.tmp / "d"), "connection": conn_id})
        self.api.save_job({"name": "Cloud pull", "source": "EXTRACTED", "dest": str(self.tmp / "d2")})
        by_id = {item["id"]: item for item in self.api.self_test()["results"]}
        self.assertEqual(by_id["nextcloud"]["status"], "ok")
        self.assertEqual(by_id[f"connection:{conn_id}"]["status"], "ok")
        jobs = [item for key, item in by_id.items() if key.startswith("job:") and not key.endswith(":space")]
        self.assertEqual([item["status"] for item in jobs], ["ok", "ok"])

    def test_the_report_hides_the_servers_and_never_holds_the_secrets(self) -> None:
        import zipfile
        self.add()
        result = self.api.export_report(False)
        with zipfile.ZipFile(result["path"]) as archive:
            blob = "\n".join(archive.read(name).decode("utf-8", "replace") for name in archive.namelist())
        for secret in (PASSWORD, KEY[1], "Home server"):
            self.assertNotIn(secret, blob, secret)


class Restart(TempCase):
    def test_connections_and_the_job_choice_survive_a_restart(self) -> None:
        api = NextPullApi(self.tmp / "data", poll_seconds=0.05)
        record = clean_connection({"server": "http://192.168.1.10:8092", "user": "ben", "password": PASSWORD, "header_name": KEY[0], "header_value": KEY[1], "id": "sg-keep01", "label": "Pi"})
        api._secrets.save_connection(record)
        api.save_job({"name": "Keep", "source": "Incoming", "dest": str(self.tmp / "d"), "connection": "sg-keep01"})
        api.shutdown()
        again = NextPullApi(self.tmp / "data", poll_seconds=0.05)
        self.addCleanup(again.shutdown)
        self.assertEqual([c["id"] for c in again.list_connections()["connections"]], ["sg-keep01"])
        self.assertEqual(again._config.jobs()[0]["connection"], "sg-keep01")
        self.assertEqual(again._secrets.creds_for("sg-keep01")["header_value"], KEY[1])


if __name__ == "__main__":
    unittest.main()
