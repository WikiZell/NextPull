from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import updates
from helpers import TempCase
from nextpull import NextPullApi, __version__

ZIP = "https://github.com/WikiZell/NextPull/releases/download/v9.9.9/NextPull-9.9.9.zip"
SHA = ZIP + ".sha256"


def release(tag: str = "v9.9.9", **extra) -> dict:
    base = {"tag_name": tag, "name": f"NextPull {tag}", "body": "Notes", "html_url": f"https://github.com/WikiZell/NextPull/releases/tag/{tag}", "published_at": "2026-10-03T10:00:00Z",
            "draft": False, "prerelease": False,
            "assets": [{"name": "NextPull-9.9.9.zip", "browser_download_url": ZIP, "size": 1234, "digest": "sha256:" + "AB" * 32},
                       {"name": "NextPull-9.9.9.zip.sha256", "browser_download_url": SHA, "size": 90}]}
    base.update(extra)
    return base


class FakeGitHub:
    """Serves one canned answer for any path."""

    def __init__(self) -> None:
        self.status, self.body, self.hits = 200, json.dumps(release()).encode(), 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.hits += 1
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/latest"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Versions(unittest.TestCase):
    def test_ordering(self) -> None:
        self.assertTrue(updates.is_newer("v0.2.0", "0.1.1"))
        self.assertTrue(updates.is_newer("0.10.0", "0.9.9"))
        self.assertTrue(updates.is_newer("1.0.1", "1.0"))
        self.assertFalse(updates.is_newer("0.1.1", "0.1.1"))
        self.assertFalse(updates.is_newer("1.0", "1.0.0"))
        self.assertFalse(updates.is_newer("0.1.0", "0.1.1"))

    def test_prerelease_sorts_before_its_release(self) -> None:
        self.assertTrue(updates.is_newer("1.0.0", "1.0.0-beta.1"))
        self.assertFalse(updates.is_newer("1.0.0-beta.1", "1.0.0"))

    def test_garbage_is_never_newer(self) -> None:
        self.assertFalse(updates.is_newer("latest", "0.1.1"))
        self.assertFalse(updates.is_newer("", "0.1.1"))
        self.assertFalse(updates.is_newer("2.0", "weird"))


class Summaries(unittest.TestCase):
    def test_fields_and_asset(self) -> None:
        info = updates.summarize(release())
        self.assertEqual((info["version"], info["tag"]), ("9.9.9", "v9.9.9"))
        self.assertEqual(info["asset"]["url"], ZIP)
        self.assertEqual(info["asset"]["sha256"], "ab" * 32)
        self.assertEqual(info["asset"]["sha256_url"], SHA)
        self.assertTrue(info["page"].startswith("https://github.com/"))

    def test_drafts_prereleases_and_non_versions_are_ignored(self) -> None:
        self.assertIsNone(updates.summarize(release(draft=True)))
        self.assertIsNone(updates.summarize(release(prerelease=True)))
        self.assertIsNone(updates.summarize(release(tag="nightly")))
        self.assertIsNone(updates.summarize("nope"))

    def test_only_github_https_links_are_trusted(self) -> None:
        self.assertTrue(updates.trusted_url(ZIP))
        self.assertTrue(updates.trusted_url("https://objects.githubusercontent.com/x/y.zip"))
        for bad in ("http://github.com/x.zip", "https://evil.example.com/NextPull-1.zip", "https://github.com.evil.example/x.zip", "file:///C:/x.zip", ""):
            self.assertFalse(updates.trusted_url(bad), bad)
        hostile = release(assets=[{"name": "NextPull-9.9.9.zip", "browser_download_url": "https://evil.example.com/NextPull-9.9.9.zip", "size": 1}], html_url="https://evil.example.com/r")
        info = updates.summarize(hostile)
        self.assertIsNone(info["asset"])
        self.assertEqual(info["page"], "")

    def test_a_release_without_a_zip_has_no_asset(self) -> None:
        self.assertIsNone(updates.summarize(release(assets=[{"name": "notes.txt", "browser_download_url": ZIP, "size": 1}]))["asset"])


class Fetching(unittest.TestCase):
    def setUp(self) -> None:
        self.gh = FakeGitHub()
        self.addCleanup(self.gh.stop)

    def test_ok(self) -> None:
        self.assertEqual(updates.latest_release(self.gh.url)["version"], "9.9.9")

    def test_no_release_yet(self) -> None:
        self.gh.status = 404
        self.assertIsNone(updates.latest_release(self.gh.url))

    def test_errors_have_friendly_messages(self) -> None:
        for status, text in ((403, "limiting"), (429, "limiting"), (500, "HTTP 500")):
            self.gh.status = status
            with self.assertRaisesRegex(updates.UpdateError, text):
                updates.latest_release(self.gh.url)
        self.gh.status, self.gh.body = 200, b"<html>not json"
        with self.assertRaisesRegex(updates.UpdateError, "unreadable"):
            updates.latest_release(self.gh.url)
        with self.assertRaisesRegex(updates.UpdateError, "Could not reach"):
            updates.latest_release("http://127.0.0.1:1/none", timeout=2)


class ApiCase(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.gh = FakeGitHub()
        self.addCleanup(self.gh.stop)
        self.api = NextPullApi(None, poll_seconds=0.05)
        self.api._update_url = self.gh.url
        self.addCleanup(self.api.shutdown)

    def test_nothing_new_when_versions_match(self) -> None:
        self.gh.body = json.dumps(release(tag=f"v{__version__}")).encode()
        view = self.api.check_update()
        self.assertTrue(view["ok"] and not view["available"])
        self.assertIsNone(self.api.status()["update"])

    def test_a_newer_release_shows_in_status_and_can_be_skipped(self) -> None:
        view = self.api.check_update()
        self.assertTrue(view["available"])
        self.assertEqual(view["latest"]["version"], "9.9.9")
        self.assertTrue(view["latest"]["installable"])
        self.assertEqual(self.api.status()["update"], {"version": "9.9.9", "installable": True, "can_install": False})   # from source: no self-update
        self.api.skip_update("9.9.9")
        self.assertIsNone(self.api.status()["update"])
        self.gh.body = json.dumps(release(tag="v10.0.0")).encode()
        self.api.check_update()
        self.assertEqual(self.api.status()["update"]["version"], "10.0.0")   # a newer one is shown again

    def test_switching_the_check_off_hides_the_banner_but_check_now_still_works(self) -> None:
        self.api.save_settings({"check_updates": False})
        self.assertTrue(self.api.check_update()["available"])
        self.assertIsNone(self.api.status()["update"])
        self.assertFalse(self.api.update_info()["enabled"])

    def test_saving_other_settings_keeps_the_update_choice(self) -> None:
        self.api.save_settings({"check_updates": False})
        self.api.save_settings({"notifications": False})   # the UI sends a subset
        self.assertFalse(self.api.get_settings()["settings"]["check_updates"])

    def test_a_failed_check_keeps_the_last_good_answer_and_reports_the_error(self) -> None:
        self.api.check_update()
        self.gh.status = 500
        view = self.api.check_update()
        self.assertIn("HTTP 500", view["error"])
        self.assertTrue(view["available"])

    def test_notification_once_per_version(self) -> None:
        notify = mock.Mock()
        self.api._engine.notify = notify
        self.api.check_update()
        self.api.check_update()
        self.assertEqual(notify.call_count, 1)
        self.assertIn("9.9.9", notify.call_args[0][1])

    def test_release_page_only_opens_trusted_links(self) -> None:
        self.assertFalse(self.api.open_release_page()["ok"])   # nothing checked yet
        self.api.check_update()
        with mock.patch("nextpull.webbrowser.open") as opened:
            self.assertTrue(self.api.open_release_page()["ok"])
            self.assertTrue(opened.call_args[0][0].startswith("https://github.com/"))

    def test_the_background_loop_checks_by_itself_and_stops(self) -> None:
        self.api._update_delay = 0.05
        self.api.start_engine()
        for _ in range(100):
            if self.api.update_info()["checked"]:
                break
            time.sleep(0.05)
        self.assertTrue(self.api.update_info()["available"])
        self.assertGreaterEqual(self.gh.hits, 1)


if __name__ == "__main__":
    unittest.main()
