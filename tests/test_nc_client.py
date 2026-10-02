from __future__ import annotations

import unittest

import helpers
from helpers import TempCase, write_file
from fake_nextcloud import FakeNextcloud
from nc_client import LoginFlow, NextcloudError, NextcloudSession, normalize_server_url, parse_propfind


class UrlNormalising(unittest.TestCase):
    def test_variants(self) -> None:
        cases = {
            "cloud.example.com": "https://cloud.example.com",
            "https://cloud.example.com/": "https://cloud.example.com",
            "https://cloud.example.com/index.php/login": "https://cloud.example.com",
            "https://cloud.example.com/apps/files/?dir=/x": "https://cloud.example.com",
            "http://192.168.1.5:8080": "http://192.168.1.5:8080",
            "https://host.example/nextcloud/index.php/apps/files": "https://host.example/nextcloud",
            "  https://host.example/nextcloud/  ": "https://host.example/nextcloud",
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_server_url(raw), expected, raw)

    def test_rejects_garbage(self) -> None:
        for bad in ("", "   ", "ftp://x.example", "https://"):
            with self.assertRaises(NextcloudError, msg=bad):
                normalize_server_url(bad)


class PropfindParsing(unittest.TestCase):
    XML = (b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
           b'<d:response><d:href>/remote.php/dav/files/al%20ice/Films/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype><oc:size>9</oc:size></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'
           b'<d:response><d:href>/remote.php/dav/files/al%20ice/Films/Caf%C3%A9%20%26%20Bar.mkv</d:href><d:propstat><d:prop><d:resourcetype/><d:getcontentlength>2048</d:getcontentlength>'
           b'<d:getlastmodified>Wed, 01 Oct 2026 02:00:00 GMT</d:getlastmodified></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'
           b'<d:response><d:href>/remote.php/dav/files/al%20ice/Films/Sub/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype><oc:size>7</oc:size></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'
           b'<d:response><d:href>/remote.php/dav/files/al%20ice/Films/hidden</d:href><d:propstat><d:prop/><d:status>HTTP/1.1 404 Not Found</d:status></d:propstat></d:response></d:multistatus>')

    def test_entries_names_sizes_and_skipped_404_props(self) -> None:
        entries = parse_propfind(self.XML, "/remote.php/dav/files/al ice")
        by_name = {entry.name: entry for entry in entries}
        self.assertEqual(set(by_name), {"Films", "Café & Bar.mkv", "Sub"})
        self.assertEqual(by_name["Café & Bar.mkv"].path, "Films/Café & Bar.mkv")
        self.assertEqual((by_name["Café & Bar.mkv"].is_dir, by_name["Café & Bar.mkv"].size), (False, 2048))
        self.assertEqual((by_name["Sub"].is_dir, by_name["Sub"].size), (True, 7))
        self.assertTrue(by_name["Café & Bar.mkv"].modified.startswith("2026-10-01T02:00:00"))

    def test_unreadable_xml(self) -> None:
        with self.assertRaises(NextcloudError):
            parse_propfind(b"<not xml", "/x")


class AgainstFakeServer(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.files = self.tmp / "files"
        write_file(self.files / "Films" / "b.mkv", 2000)
        write_file(self.files / "Films" / "a.mkv", 1000)
        write_file(self.files / "Films" / "Sub" / "c.srt", 50)
        write_file(self.files / "readme.txt", 5)
        self.server = FakeNextcloud(self.files)
        self.base = self.server.start()
        self.addCleanup(self.server.stop)
        self.session = NextcloudSession(self.base, "alice", "app-pass-1234")

    def test_login_flow_waits_then_returns_the_app_password(self) -> None:
        flow = LoginFlow(self.base)
        self.assertTrue(flow.start().startswith(self.base + "/login/v2/flow/"))
        self.assertIsNone(flow.poll_once())
        self.server.approved.set()
        creds = flow.poll_once()
        self.assertEqual(creds, {"server": self.base, "user": "alice", "app_password": "app-pass-1234"})

    def test_login_flow_errors(self) -> None:
        with self.assertRaises(NextcloudError):
            LoginFlow(self.base).poll_once()   # not started
        self.server.stop()
        with self.assertRaises(NextcloudError) as caught:
            LoginFlow(self.base).start()
        self.assertIn("Cannot reach", str(caught.exception))

    def test_root_listing_folders_first(self) -> None:
        names = [(entry.name, entry.is_dir) for entry in self.session.list_dir("")]
        self.assertEqual(names, [("Films", True), ("readme.txt", False)])

    def test_sub_folder_listing_sorted_with_relative_paths(self) -> None:
        entries = self.session.list_dir("Films")
        self.assertEqual([entry.name for entry in entries], ["Sub", "a.mkv", "b.mkv"])
        self.assertEqual(entries[1].path, "Films/a.mkv")
        self.assertEqual(entries[2].size, 2000)
        self.assertEqual(self.session.list_dir("Films/")[0].path, "Films/Sub")
        self.assertEqual(self.session.list_dir("\\Films\\Sub")[0].name, "c.srt")

    def test_errors_map_to_clear_messages(self) -> None:
        with self.assertRaises(NextcloudError) as caught:
            self.session.list_dir("Nope")
        self.assertIn("not found", str(caught.exception))
        with self.assertRaises(NextcloudError):
            self.session.list_dir("a/../b")
        with self.assertRaises(NextcloudError) as caught:
            NextcloudSession(self.base, "alice", "wrong").list_dir("")
        self.assertIn("rejected", str(caught.exception))

    def test_user_info_and_revoke(self) -> None:
        info = self.session.user_info()
        self.assertEqual((info["display_name"], info["quota_total"]), ("Alice Example", 10 * 1024 ** 3))
        self.assertTrue(self.session.revoke())
        self.assertTrue(self.server.revoked)
        self.assertFalse(NextcloudSession(self.base, "alice", "wrong").revoke())

    def test_the_client_never_downloads_or_deletes_user_files(self) -> None:
        self.session.list_dir("Films")
        self.session.user_info()
        self.assertTrue(all(method in ("PROPFIND", "GET") for method, _ in self.server.requests))
        self.assertFalse(any(path.endswith(".mkv") for method, path in self.server.requests if method == "GET"))


if __name__ == "__main__":
    unittest.main()
