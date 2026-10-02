"""Signing in with an e-mail address: the login name (``alice+tv@example.org``) is not Nextcloud's user id (``alice``), and the WebDAV
path ``/remote.php/dav/files/<id>`` needs the id. Using the login name there made every folder "not found"."""
from __future__ import annotations

import os
import time
import unittest
from unittest import mock

import helpers
from helpers import FAKE_RCLONE, ROOT, TempCase, make_job, write_file
from fake_nextcloud import FakeNextcloud
from nc_client import NextcloudSession, dav_files_url
from nextpull import NextPullApi
from rclone_runner import RcloneRun, build_env, find_rclone
from store import HistoryDb

RCLONE = find_rclone("", [ROOT])
LOGIN = "alice+tv@example.org"
PASSWORD = "app-pass-1234"


class EmailLoginCase(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.remote = self.tmp / "remote"
        write_file(self.remote / "Media" / "a.mkv", 2000)
        write_file(self.remote / "Media" / "Sub" / "b.mkv", 500)
        os.environ["FAKE_REMOTE_ROOT"] = str(self.remote)
        self.server = FakeNextcloud(self.remote, user="alice", password=PASSWORD, login=LOGIN)
        self.base = self.server.start()
        self.addCleanup(self.server.stop)


class SessionUsesTheUserId(EmailLoginCase):
    def test_listing_works_with_an_email_login_and_finds_the_id(self) -> None:
        session = NextcloudSession(self.base, LOGIN, PASSWORD)
        self.assertEqual([entry.name for entry in session.list_dir("Media")], ["Sub", "a.mkv"])
        self.assertEqual(session.uid, "alice")
        self.assertTrue(session.files_url.endswith("/remote.php/dav/files/alice"))

    def test_check_folder_and_a_known_id_needs_no_lookup(self) -> None:
        session = NextcloudSession(self.base, LOGIN, PASSWORD, uid="alice")
        self.server.requests.clear()
        self.assertTrue(session.check_folder("Media"))
        self.assertFalse(any("/ocs/" in path for _, path in self.server.requests))

    def test_a_wrong_password_still_says_so(self) -> None:
        from nc_client import NextcloudError
        with self.assertRaisesRegex(NextcloudError, "rejected the saved login"):
            NextcloudSession(self.base, LOGIN, "wrong").list_dir("")

    def test_plain_user_names_still_work(self) -> None:
        server = FakeNextcloud(self.remote, user="bob", password=PASSWORD)
        base = server.start()
        self.addCleanup(server.stop)
        self.assertEqual(len(NextcloudSession(base, "bob", PASSWORD).list_dir("Media")), 2)

    def test_the_login_name_is_url_safe_in_the_old_wrong_path(self) -> None:
        # documents the failure this fixes: the e-mail in the path is quoted correctly but is simply not a user id
        self.assertIn("alice%2Btv%40example.org", dav_files_url(self.base, LOGIN))
        session = NextcloudSession(self.base, LOGIN, PASSWORD, uid=LOGIN)
        from nc_client import NextcloudError
        with self.assertRaisesRegex(NextcloudError, "not found"):
            session.list_dir("Media")


class RemoteUsesTheUserId(unittest.TestCase):
    def test_build_env_prefers_the_uid_for_the_url_and_the_login_for_auth(self) -> None:
        env = build_env({"server": "https://cloud.example.org", "user": LOGIN, "uid": "alice", "app_password": "x"}, "obscured", base={})
        self.assertEqual(env["RCLONE_CONFIG_NC_URL"], "https://cloud.example.org/remote.php/dav/files/alice")
        self.assertEqual(env["RCLONE_CONFIG_NC_USER"], LOGIN)
        legacy = build_env({"server": "https://cloud.example.org", "user": "bob", "app_password": "x"}, "o", base={})
        self.assertTrue(legacy["RCLONE_CONFIG_NC_URL"].endswith("/files/bob"))


@unittest.skipUnless(RCLONE, "real rclone not available (run tools/Fetch-rclone.ps1)")
class RealRcloneWithEmailLogin(EmailLoginCase):
    def test_a_run_with_saved_credentials_that_have_no_uid_downloads(self) -> None:
        history = HistoryDb(None)
        self.addCleanup(history.close)
        dest = self.tmp / "dest"
        job = make_job(dest=str(dest), source="Media")
        run_id = history.start_run(job, "manual")
        creds = {"server": self.base, "user": LOGIN, "app_password": PASSWORD}   # an older saved login: no uid
        run = RcloneRun(rclone=RCLONE, job=job, creds=creds, run_id=run_id, history=history, log_path=self.tmp / "logs" / "r.jsonl",
                        config_path=self.tmp / "rclone.empty.conf", keep_awake=False)
        result = run.run()
        self.assertEqual(result["state"], "ok", result)
        self.assertEqual(result["files"], 2)
        self.assertTrue((dest / "a.mkv").is_file() and (dest / "Sub" / "b.mkv").is_file())


class ApiFlow(EmailLoginCase):
    def setUp(self) -> None:
        super().setUp()
        self.api = NextPullApi(None, poll_seconds=0.05)
        self.api._engine.rclone_finder = lambda: FAKE_RCLONE
        self.addCleanup(self.api.shutdown)

    def connect(self) -> None:
        with mock.patch("nextpull.webbrowser.open"):
            self.assertTrue(self.api.connect_start(self.base)["ok"])
        self.server.approved.set()
        for _ in range(100):
            if self.api.connect_status()["state"] == "connected":
                return
            time.sleep(0.05)
        self.fail(self.api._login)

    def test_sign_in_with_an_email_stores_the_id_and_browsing_works(self) -> None:
        self.connect()
        saved = self.api._secrets.load()
        self.assertEqual(saved["user"], LOGIN)
        self.assertEqual(saved["uid"], "alice")
        result = self.api.browse("Media")
        self.assertTrue(result["ok"], result)
        self.assertEqual([entry["name"] for entry in result["entries"]], ["Sub", "a.mkv"])

    def test_an_older_saved_login_gets_its_id_on_first_browse(self) -> None:
        self.api._secrets.save(self.base, LOGIN, PASSWORD)
        self.assertNotIn("uid", self.api._secrets.load())
        self.assertTrue(self.api.browse("Media")["ok"])
        self.assertEqual(self.api._secrets.load()["uid"], "alice")


if __name__ == "__main__":
    unittest.main()
