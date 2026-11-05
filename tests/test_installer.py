"""The update engine: backup, download guards, safe unpack, the swap and the rollback. No GUI, no real GitHub."""
from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

import installer
from helpers import TempCase
from installer import Installer, Request, UpdateFailed

LOCAL = lambda url: urlsplit(url).hostname == "127.0.0.1"   # noqa: E731 - tests talk to a local server only


def make_zip(files: dict[str, bytes], prefix: str = "") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(prefix + name, data)
    return buffer.getvalue()


NEW_FILES = {"NextPull.exe": b"new-exe", "_internal/lib.dll": b"new-dll", "web/index.html": b"<html>new</html>", "README.md": b"new readme"}


class Release:
    """A local stand-in for GitHub: serves the zip, its checksum, and optionally redirects somewhere else."""

    def __init__(self, payload: bytes, digest: str | None = None) -> None:
        self.payload = payload
        self.digest = digest if digest is not None else hashlib.sha256(payload).hexdigest()
        self.redirect_to = ""
        self.cut_short = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_GET(self) -> None:
                if self.path.endswith(".sha256"):
                    body = f"{outer.digest}  NextPull-9.9.9.zip\n".encode()
                elif outer.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", outer.redirect_to)
                    self.end_headers()
                    return
                else:
                    body = outer.payload
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body[: len(body) // 2] if outer.cut_short and not self.path.endswith(".sha256") else body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Recorder:
    def __init__(self) -> None:
        self.steps: list[tuple[str, str]] = []
        self.logs: list[str] = []
        self.progress_calls: list[tuple[int, int]] = []
        self.cancel = False
        self.cancel_allowed: list[bool] = []

    def step(self, key, state, detail="") -> None: self.steps.append((key, state))
    def progress(self, done, total, speed) -> None: self.progress_calls.append((done, total))
    def log(self, text) -> None: self.logs.append(text)
    def cancelled(self) -> bool: return self.cancel
    def can_cancel(self, allowed) -> None: self.cancel_allowed.append(allowed)
    def need_force(self, allowed) -> None: pass


class FakeProcess:
    def __init__(self, code=None) -> None:
        self.code, self.terminated = code, False

    def poll(self): return self.code
    def terminate(self) -> None: self.terminated, self.code = True, 1
    def wait(self, timeout=None) -> None: pass
    def kill(self) -> None: pass


class InstallerCase(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.app = self.tmp / "apps" / "NextPull"
        for name, data in {"NextPull.exe": b"old-exe", "_internal/lib.dll": b"old-dll", "web/index.html": b"<html>old</html>", "tools/rclone/rclone.exe": b"rclone-binary"}.items():
            target = self.app / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        self.data = self.tmp / "data"
        (self.data / "logs").mkdir(parents=True)
        for name, data in {"jobs.json": b'{"jobs": [1]}', "credentials.bin": b"secret-blob", "history.db": b"sqlite-bytes", "state.json": b"{}", "app.log": b"log line"}.items():
            (self.data / name).write_bytes(data)
        (self.data / "logs" / "run1.log").write_text("raw rclone log")
        self.releases: list[Release] = []
        self.launched: list[Path] = []
        self.locked = False

    def tearDown(self) -> None:
        for release in self.releases:
            release.close()

    def release(self, files=None, **kw) -> Release:
        release = Release(make_zip(files if files is not None else NEW_FILES), **kw)
        self.releases.append(release)
        return release

    def request(self, release: Release, **kw) -> Request:
        base = dict(app_dir=str(self.app), data_dir=str(self.data), current_version="0.2.0", version="9.9.9", asset_name="NextPull-9.9.9.zip",
                    asset_url=release.base + "/NextPull-9.9.9.zip", size=len(release.payload), sha256="", sha256_url=release.base + "/NextPull-9.9.9.zip.sha256")
        base.update(kw)
        return Request(**base)

    def installer(self, request: Request, recorder: Recorder | None = None, **kw) -> Installer:
        process = kw.pop("process", None)

        def launch(exe: Path):
            self.launched.append(exe)
            return process or FakeProcess()
        defaults = dict(allowed=LOCAL, health_check=lambda program, scratch: {"ok": True, "version": "9.9.9"}, launch=launch, watch_seconds=0.3, lock_seconds=0.5)
        defaults.update(kw)
        return Installer(request, recorder or Recorder(), **defaults)

    def read(self, *parts: str) -> bytes:
        return self.app.joinpath(*parts).read_bytes()


class HappyPath(InstallerCase):
    def test_update_installs_backs_up_and_keeps_the_old_folder(self) -> None:
        recorder = Recorder()
        outcome = self.installer(self.request(self.release()), recorder).run()
        self.assertTrue(outcome.ok, outcome.message)
        self.assertEqual(self.read("NextPull.exe"), b"new-exe")
        self.assertEqual(self.read("web", "index.html"), b"<html>new</html>")
        previous = self.app.with_name("NextPull.previous")
        self.assertEqual((previous / "NextPull.exe").read_bytes(), b"old-exe")
        self.assertEqual(self.launched, [self.app / "NextPull.exe"])
        done = [key for key, state in recorder.steps if state == "done"]
        self.assertEqual(done, [key for key, _ in installer.STEPS])
        self.assertTrue(any(done == total > 0 for done, total in recorder.progress_calls))
        self.assertFalse(self.app.with_name("NextPull.new").exists())
        self.assertFalse((self.data / "update").exists(), "the downloaded zip is cleaned up after success")

    def test_files_added_by_the_user_under_tools_survive(self) -> None:
        self.installer(self.request(self.release())).run()
        self.assertEqual(self.read("tools", "rclone", "rclone.exe"), b"rclone-binary")

    def test_release_may_wrap_everything_in_one_folder(self) -> None:
        release = Release(make_zip(NEW_FILES, prefix="NextPull/"))
        self.releases.append(release)
        outcome = self.installer(self.request(release)).run()
        self.assertTrue(outcome.ok, outcome.message)
        self.assertEqual(self.read("NextPull.exe"), b"new-exe")

    def test_result_file_says_what_happened(self) -> None:
        self.installer(self.request(self.release())).run()
        result = installer.read_result(self.data)
        self.assertTrue(result["ok"])
        self.assertEqual((result["version"], result["previous_version"]), ("9.9.9", "0.2.0"))
        installer.clear_result(self.data)
        self.assertIsNone(installer.read_result(self.data))

    def test_settings_are_untouched_by_a_good_update(self) -> None:
        self.installer(self.request(self.release())).run()
        self.assertEqual((self.data / "jobs.json").read_bytes(), b'{"jobs": [1]}')
        self.assertEqual((self.data / "credentials.bin").read_bytes(), b"secret-blob")


class Backup(InstallerCase):
    def test_backup_holds_the_setup_but_not_logs(self) -> None:
        outcome = self.installer(self.request(self.release())).run()
        archive = zipfile.ZipFile(outcome.backup)
        names = set(archive.namelist())
        self.assertEqual(names, {"jobs.json", "credentials.bin", "history.db", "state.json", "manifest.json"})
        manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(manifest["version"], "0.2.0")
        self.assertEqual(manifest["files"]["jobs.json"]["sha256"], hashlib.sha256(b'{"jobs": [1]}').hexdigest())

    def test_backup_is_verified_and_a_damaged_one_is_detected(self) -> None:
        path = installer.backup_config(self.data, self.tmp / "b", "0.2.0")
        installer.check_backup(path)
        raw = bytearray(path.read_bytes())
        raw[60] ^= 0xFF
        path.write_bytes(bytes(raw))
        with self.assertRaises((UpdateFailed, zipfile.BadZipFile)):
            installer.check_backup(path)

    def test_only_the_newest_five_backups_are_kept(self) -> None:
        folder = self.tmp / "b"
        from datetime import datetime, timedelta
        base = datetime(2026, 1, 1, 12, 0, 0)
        for index in range(8):
            installer.backup_config(self.data, folder, "0.2.0", now=base + timedelta(minutes=index))
        names = sorted(path.name for path in folder.glob("*.zip"))
        self.assertEqual(len(names), 5)
        self.assertTrue(names[0].startswith("20260101-120300"))

    def test_restore_puts_back_only_what_changed(self) -> None:
        path = installer.backup_config(self.data, self.tmp / "b", "0.2.0")
        (self.data / "jobs.json").write_bytes(b"migrated by a bad version")
        restored = installer.restore_config(path, self.data)
        self.assertEqual(restored, ["jobs.json"])
        self.assertEqual((self.data / "jobs.json").read_bytes(), b'{"jobs": [1]}')

    def test_a_backup_that_cannot_be_written_stops_the_update(self) -> None:
        with mock.patch("installer.check_backup", side_effect=UpdateFailed("damaged", "backup")):
            outcome = self.installer(self.request(self.release())).run()
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.step, "backup")
        self.assertEqual(self.read("NextPull.exe"), b"old-exe")
        self.assertEqual(list((self.data / "backups").glob("*.partial")), [])


class RefusesBadReleases(InstallerCase):
    def assertUnchanged(self, outcome) -> None:
        self.assertFalse(outcome.ok)
        self.assertFalse(outcome.rolled_back)
        self.assertEqual(self.read("NextPull.exe"), b"old-exe")
        self.assertEqual(self.read("web", "index.html"), b"<html>old</html>")
        self.assertFalse(self.app.with_name("NextPull.previous").exists())
        self.assertFalse(self.app.with_name("NextPull.new").exists())
        self.assertFalse(self.app.with_name("NextPull.unpack").exists())

    def test_wrong_checksum_is_refused(self) -> None:
        outcome = self.installer(self.request(self.release(digest="0" * 64))).run()
        self.assertUnchanged(outcome)
        self.assertIn("checksum", outcome.message)

    def test_a_release_without_checksum_is_refused(self) -> None:
        release = self.release(digest="not-a-hash")
        outcome = self.installer(self.request(release)).run()
        self.assertUnchanged(outcome)
        self.assertIn("no checksum", outcome.message)

    def test_disagreeing_checksums_are_refused(self) -> None:
        release = self.release()
        outcome = self.installer(self.request(release, sha256="f" * 64)).run()
        self.assertUnchanged(outcome)
        self.assertIn("disagree", outcome.message)

    def test_digest_from_github_is_enough_without_the_sha_file(self) -> None:
        release = self.release()
        digest = hashlib.sha256(release.payload).hexdigest()
        outcome = self.installer(self.request(release, sha256=digest, sha256_url="")).run()
        self.assertTrue(outcome.ok, outcome.message)

    def test_path_traversal_in_the_zip_is_refused(self) -> None:
        outcome = self.installer(self.request(self.release({"NextPull.exe": b"x", "../evil.txt": b"x"}))).run()
        self.assertUnchanged(outcome)
        self.assertIn("unsafe path", outcome.message)
        self.assertFalse((self.tmp / "apps" / "evil.txt").exists())

    def test_absolute_and_drive_paths_are_refused(self) -> None:
        for bad in ("C:/Windows/x.txt", "/etc/x", "\\\\server\\share\\x"):
            with self.subTest(bad=bad), self.assertRaises(UpdateFailed):
                path = self.tmp / "z.zip"
                path.write_bytes(make_zip({"NextPull.exe": b"x", bad: b"x"}))
                installer.unpack(path, self.tmp / "out")

    def test_a_zip_without_the_program_is_refused(self) -> None:
        outcome = self.installer(self.request(self.release({"readme.txt": b"hi"}))).run()
        self.assertUnchanged(outcome)
        self.assertIn("NextPull.exe", outcome.message)

    def test_something_that_is_not_a_zip_is_refused(self) -> None:
        release = Release(b"<html>not a zip</html>")
        self.releases.append(release)
        outcome = self.installer(self.request(release)).run()
        self.assertUnchanged(outcome)

    def test_redirect_away_from_github_is_refused(self) -> None:
        release = self.release()
        release.redirect_to = "http://evil.example.net/NextPull.zip"
        outcome = self.installer(self.request(release)).run()
        self.assertUnchanged(outcome)
        self.assertIn("not GitHub", outcome.message)

    def test_a_non_github_address_is_refused_with_the_real_rules(self) -> None:
        release = self.release()
        outcome = Installer(self.request(release), Recorder(), health_check=lambda *a: {"ok": True, "version": "9.9.9"}, launch=lambda exe: FakeProcess(), watch_seconds=0.1, lock_seconds=0.2).run()
        self.assertUnchanged(outcome)

    def test_a_truncated_download_is_retried_and_then_reported(self) -> None:
        release = self.release()
        release.cut_short = True
        with mock.patch("installer.time.sleep"):
            outcome = self.installer(self.request(release)).run()
        self.assertUnchanged(outcome)
        self.assertIn("3 tries", outcome.message)
        self.assertEqual(list((self.data / "update").glob("*.part")), [])

    def test_new_version_that_fails_its_start_check_is_never_installed(self) -> None:
        outcome = self.installer(self.request(self.release()), health_check=lambda p, s: {"ok": False, "problems": ["jobs.json: unknown schema"]}).run()
        self.assertUnchanged(outcome)
        self.assertEqual(outcome.step, "verify")
        self.assertIn("unknown schema", outcome.message)

    def test_wrong_version_inside_the_zip_is_refused(self) -> None:
        outcome = self.installer(self.request(self.release()), health_check=lambda p, s: {"ok": True, "version": "1.0.0"}).run()
        self.assertUnchanged(outcome)
        self.assertIn("not 9.9.9", outcome.message)

    def test_health_check_gets_a_copy_of_the_settings_not_the_real_ones(self) -> None:
        seen = {}

        def check(program: Path, scratch: Path) -> dict:
            seen["scratch"] = scratch
            seen["files"] = sorted(path.name for path in scratch.iterdir())
            (scratch / "jobs.json").write_bytes(b"changed by the check")
            return {"ok": True, "version": "9.9.9"}
        self.installer(self.request(self.release()), health_check=check).run()
        self.assertEqual((self.data / "jobs.json").read_bytes(), b'{"jobs": [1]}')
        self.assertIn("jobs.json", seen["files"])
        self.assertNotEqual(seen["scratch"], self.data)

    def test_old_version_is_started_again_after_a_refusal(self) -> None:
        self.installer(self.request(self.release(digest="0" * 64))).run()
        self.assertEqual(self.launched, [self.app / "NextPull.exe"])
        self.assertEqual(self.read("NextPull.exe"), b"old-exe")


class Rollback(InstallerCase):
    def test_a_new_version_that_crashes_at_start_is_rolled_back(self) -> None:
        def launch(exe: Path):
            self.launched.append(exe)
            if exe.read_bytes() == b"new-exe":
                (self.data / "jobs.json").write_bytes(b"half migrated by the new version")
                return FakeProcess(code=3)
            return FakeProcess()
        recorder = Recorder()
        outcome = self.installer(self.request(self.release()), recorder, launch=launch).run()
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.rolled_back)
        self.assertFalse(outcome.restore_failed)
        self.assertEqual(outcome.step, "restart")
        self.assertEqual(self.read("NextPull.exe"), b"old-exe")
        self.assertEqual(self.read("_internal", "lib.dll"), b"old-dll")
        self.assertEqual((self.data / "jobs.json").read_bytes(), b'{"jobs": [1]}', "settings restored from the backup")
        self.assertEqual(len(self.launched), 2, "new version tried once, old version started again")
        self.assertFalse(self.app.with_name("NextPull.failed").exists())
        self.assertFalse(self.app.with_name("NextPull.previous").exists())
        self.assertIn(("restart", "error"), recorder.steps)
        result = installer.read_result(self.data)
        self.assertFalse(result["ok"])
        self.assertTrue(result["rolled_back"])

    def test_a_new_version_that_dies_while_being_watched_is_rolled_back(self) -> None:
        process = FakeProcess()
        calls = {"n": 0}

        def poll():
            calls["n"] += 1
            return 1 if calls["n"] > 2 else None
        process.poll = poll
        outcome = self.installer(self.request(self.release()), process=process, watch_seconds=2.0).run()
        self.assertTrue(outcome.rolled_back)
        self.assertEqual(self.read("NextPull.exe"), b"old-exe")

    def test_a_failed_move_into_place_leaves_the_old_install_in_place(self) -> None:
        real = os.replace
        state = {"n": 0}

        def flaky(src, dst):
            if str(dst).endswith("NextPull") and str(src).endswith("NextPull.new"):
                raise OSError("disk says no")
            return real(src, dst)
        with mock.patch("installer.os.replace", side_effect=flaky):
            outcome = self.installer(self.request(self.release())).run()
        self.assertFalse(outcome.ok)
        self.assertEqual(self.read("NextPull.exe"), b"old-exe")
        self.assertFalse(self.app.with_name("NextPull.previous").exists())
        self.assertFalse(self.app.with_name("NextPull.new").exists())
        self.assertEqual(self.launched, [self.app / "NextPull.exe"])

    def test_if_the_old_folder_cannot_be_renamed_nothing_changes(self) -> None:
        real = os.replace

        def locked(src, dst):
            if str(src).endswith("NextPull") and str(dst).endswith("NextPull.previous"):
                raise PermissionError("in use")
            return real(src, dst)
        with mock.patch("installer.os.replace", side_effect=locked), mock.patch("installer.time.sleep"), \
                mock.patch("installer.time.monotonic", side_effect=iter([0, 0, 1, 2, 3, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110] + [200] * 400)):
            outcome = self.installer(self.request(self.release())).run()
        self.assertFalse(outcome.ok)
        self.assertEqual(self.read("NextPull.exe"), b"old-exe")

    def test_rollback_that_itself_fails_is_reported_honestly(self) -> None:
        real = os.replace

        def launch(exe: Path):
            self.launched.append(exe)
            return FakeProcess(code=9)

        def broken(src, dst):
            if str(dst).endswith("NextPull") and str(src).endswith("NextPull.previous"):
                raise OSError("cannot put it back")
            return real(src, dst)
        with mock.patch("installer.os.replace", side_effect=broken), mock.patch("installer.time.sleep"):
            outcome = self.installer(self.request(self.release()), launch=launch).run()
        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.restore_failed)
        self.assertTrue(self.app.with_name("NextPull.previous").is_dir(), "the old version stays on disk for a manual repair")


class Waiting(InstallerCase):
    def test_cancel_while_waiting_for_the_app_changes_nothing_and_does_not_relaunch(self) -> None:
        recorder = Recorder()
        recorder.cancel = True
        outcome = self.installer(self.request(self.release()), recorder, acquire_lock=lambda: False).run()
        self.assertTrue(outcome.cancelled)
        self.assertEqual(self.launched, [])
        self.assertEqual(self.read("NextPull.exe"), b"old-exe")

    def test_force_close_is_offered_when_the_app_does_not_quit(self) -> None:
        offered = []

        class Rec(Recorder):
            def need_force(self, allowed) -> None:
                offered.append(allowed)
                self.cancel = allowed
        outcome = self.installer(self.request(self.release()), Rec(), acquire_lock=lambda: False, lock_seconds=0.0).run()
        self.assertTrue(offered and offered[0] is True)
        self.assertTrue(outcome.cancelled)

    def test_the_lock_is_held_from_the_start_and_released_before_the_new_version_starts(self) -> None:
        events = []
        installer_ = self.installer(self.request(self.release()), acquire_lock=lambda: events.append("lock") or True, release_lock=lambda: events.append("unlock"),
                                    launch=lambda exe: events.append("launch") or FakeProcess())
        installer_.run()
        self.assertEqual(events, ["lock", "unlock", "launch"])

    def test_cancel_during_download_is_clean(self) -> None:
        release = self.release()
        recorder = Recorder()
        original = recorder.progress

        def progress(done, total, speed) -> None:
            original(done, total, speed)
            recorder.cancel = True
        recorder.progress = progress
        outcome = self.installer(self.request(release), recorder).run()
        self.assertTrue(outcome.cancelled)
        self.assertEqual(self.read("NextPull.exe"), b"old-exe")
        self.assertEqual(self.launched, [self.app / "NextPull.exe"], "the old version is started again after a cancel")
        self.assertEqual(list((self.data / "update").glob("*.part")), [])

    def test_cancel_is_no_longer_offered_once_installing_starts(self) -> None:
        recorder = Recorder()
        self.installer(self.request(self.release()), recorder).run()
        self.assertEqual(recorder.cancel_allowed[0], True)
        self.assertEqual(recorder.cancel_allowed[-1], False)

    def test_install_folder_that_cannot_be_written_is_explained(self) -> None:
        with mock.patch("installer.Path.mkdir", side_effect=PermissionError("denied")):
            outcome = self.installer(self.request(self.release())).run()
        self.assertFalse(outcome.ok)
        self.assertIn("C:\\NextPull", outcome.message)

    def test_a_previous_leftover_from_an_old_update_is_replaced(self) -> None:
        old = self.app.with_name("NextPull.previous")
        (old / "x").mkdir(parents=True)
        outcome = self.installer(self.request(self.release())).run()
        self.assertTrue(outcome.ok, outcome.message)
        self.assertEqual((old / "NextPull.exe").read_bytes(), b"old-exe")


class RequestFile(TempCase):
    def test_round_trip_and_missing_fields(self) -> None:
        request = Request(app_dir="a", data_dir="d", current_version="0.2.0", version="0.3.0", asset_name="n.zip", asset_url="https://github.com/x.zip")
        path = self.tmp / "r.json"
        request.save(path)
        self.assertEqual(Request.load(path), request)
        path.write_text(json.dumps({"app_dir": "a"}))
        with self.assertRaises(UpdateFailed):
            Request.load(path)

    def test_helpers(self) -> None:
        self.assertEqual(installer.human_bytes(1536), "1.5 KB")
        self.assertEqual(installer.human_bytes(5), "5 B")
        self.assertEqual(installer.human_bytes(85 * 1024 ** 2), "85.0 MB")


if __name__ == "__main__":
    unittest.main()
