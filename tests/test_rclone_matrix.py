"""The rclone behaviour matrix: the REAL rclone against tests/fake_nextcloud.py with injected faults and awkward data. Each test pins
something that was observed with tools/rclone_probe.py (names, reruns, every kind of server failure, filters, interruptions) and
checks BOTH what rclone did to the files and what NextPull concluded (state, message, per-file rows). Skipped without rclone."""
from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import helpers
from helpers import ROOT, TempCase, make_job
from fake_nextcloud import FakeNextcloud
from rclone_logic import (clean_object, collision_key, find_collisions, glob_escape, humanize_error, local_component, local_relpath, valid_bwlimit)
from rclone_runner import RcloneRun, filter_args, find_rclone
from store import HistoryDb, clean_job

RCLONE = find_rclone("", [ROOT])
sha = lambda data: hashlib.sha256(data).hexdigest()


class Pure(unittest.TestCase):
    """Pure functions: no rclone needed."""

    def test_clean_object_strips_rclone_temp_names(self) -> None:
        self.assertEqual(clean_object("Sub/bad.mkv.d0ee9403.partial"), "Sub/bad.mkv")
        self.assertEqual(clean_object("normal.partial.mkv"), "normal.partial.mkv")

    def test_windows_local_names(self) -> None:
        self.assertEqual(local_component('a:b?c*d|e"f<g>h'), "a：b？c＊d｜e＂f＜g＞h")
        self.assertEqual(local_component("trailing space "), "trailing space\u2420")
        self.assertEqual(local_component("trailing.dot."), "trailing.dot\uFF0E")
        self.assertEqual(local_component("plain.mkv"), "plain.mkv")
        self.assertEqual(local_relpath("Sub/a:b.txt"), "Sub/a：b.txt")

    def test_collisions_by_case_and_by_lookalike_characters(self) -> None:
        self.assertEqual(find_collisions(["a/Movie.mkv", "a/movie.mkv", "a/other.mkv"]), [["a/Movie.mkv", "a/movie.mkv"]])
        self.assertEqual(find_collisions(["x/a:b.txt", "x/a：b.txt"]), [["x/a:b.txt", "x/a：b.txt"]])
        self.assertEqual(find_collisions(["A/x.mkv", "B/x.mkv"]), [])
        self.assertEqual(collision_key("Dir/File.MKV"), collision_key("dir/file.mkv"))

    def test_glob_escape_makes_a_literal_anchored_pattern(self) -> None:
        self.assertEqual(glob_escape("Sub/a[1]*.mkv"), "/Sub/a\\[1\\]\\*.mkv")
        self.assertEqual(glob_escape("{x}?.txt"), "/\\{x\\}\\?.txt")

    def test_humanize(self) -> None:
        cases = {
            'Failed to create file system for "NC:Src": read metadata failed: 401 Unauthorized': "Nextcloud rejected the login (HTTP 401). Disconnect and connect again",
            "Failed to copy: failed to open source object: 403 Forbidden": "Nextcloud refused access (HTTP 403 Forbidden)",
            "Couldn't delete: 404 Not Found": "not found on Nextcloud (HTTP 404)",
            "failed to open source object: 500 Internal Server Error": "the Nextcloud server had an internal error (HTTP 500)",
            "error reading source root directory: directory not found": "the Nextcloud folder was not found",
            'Propfind "http://x/remote.php": dial tcp 127.0.0.1:1: connectex: No connection could be made': "cannot reach the Nextcloud server (network or server down)",
            "read tcp 1.2.3.4:5->6.7.8.9:10: i/o timeout": "cannot reach the Nextcloud server (network or server down)",
            "corrupted on transfer: sha1 hashes differ src(webdav root 'Src') \"abc\" vs dst(Local file system) \"def\"": "the downloaded file did not match the checksum from Nextcloud (corrupted in transfer)",
            "write C:\\x: There is not enough space on the disk.": "the destination disk is full",
            "open C:\\x: Access is denied.": "Windows denied access to the destination folder",
            "not deleting directories as there were IO errors": "some files could not be deleted on Nextcloud, so their folders were kept",
            'invalid argument "x" for "--bwlimit" flag: parsing "x" as fs.BwTimetable failed': "the speed limit/timetable is not valid for rclone",
            "x509: certificate signed by unknown authority": "the server's HTTPS certificate was rejected",
        }
        for raw, expected in cases.items():
            self.assertEqual(humanize_error(raw), expected, raw)
        self.assertEqual(humanize_error("something rclone never said before"), "something rclone never said before")   # unknown text is kept, never lost

    def test_bwlimit_syntax(self) -> None:
        for ok in ("8M", "off", "2.5M", "100k", "1M:2M", "08:00,2M 23:00,off", "Mon-08:00,512k 23:00,10M", "512", "10Mi"):
            self.assertTrue(valid_bwlimit(ok), ok)
        for bad in ("garbage", "08:00,", "fast", "2X", "08:00,2M 23:00", ""):
            self.assertFalse(valid_bwlimit(bad), bad)
        with self.assertRaises(ValueError):
            clean_job({"name": "x", "source": "a", "dest": "b", "speed": {"limit_mbps": 0, "timetable": "garbage nonsense"}})
        self.assertEqual(clean_job({"name": "x", "source": "a", "dest": "b", "speed": {"limit_mbps": 0, "timetable": "08:00,2M 23:00,off"}})["speed"]["timetable"], "08:00,2M 23:00,off")

    def test_filters_are_ordered_filter_rules_with_excludes_first(self) -> None:
        job = clean_job({"name": "x", "source": "a", "dest": "b", "excludes": ["Skip/**", "*.log"], "includes": ["*.mkv"], "min_age_minutes": 7})
        self.assertEqual(filter_args(job, ["/clash.txt"]), ["--min-age", "7m", "--filter", "- /clash.txt", "--filter", "- Skip/**", "--filter", "- *.log", "--filter", "+ *.mkv", "--filter", "- **"])
        plain = clean_job({"name": "x", "source": "a", "dest": "b", "min_age_minutes": 0})
        self.assertEqual(filter_args(plain), [])


@unittest.skipUnless(RCLONE, "real rclone not available (run tools/Fetch-rclone.ps1)")
class Matrix(TempCase):
    CREDS = {"user": "alice", "app_password": "pw-1234"}

    def setUp(self) -> None:
        super().setUp()
        self.remote = self.tmp / "remote"
        self.dest = self.tmp / "dest"
        self.srv = FakeNextcloud(self.remote, user="alice", password="pw-1234")
        self.base = self.srv.start()
        self.addCleanup(self.srv.stop)
        self.history = HistoryDb(None)
        self.addCleanup(self.history.close)
        self.n = 0
        RcloneRun.PRECHECK_DELAY = 0.2   # keep the retry tests quick
        self.addCleanup(setattr, RcloneRun, "PRECHECK_DELAY", 5.0)

    def make(self, deadline: datetime | None = None, creds: dict | None = None, **job_overrides) -> tuple[RcloneRun, int]:
        job = clean_job({"name": "t", "source": "Src", "dest": str(self.dest), "mode": "copy", "min_age_minutes": 0, **job_overrides})
        run_id = self.history.start_run(job, "manual")
        self.n += 1
        run = RcloneRun(rclone=RCLONE, job=job, creds=creds or {**self.CREDS, "server": self.base}, run_id=run_id, history=self.history,
                        log_path=self.tmp / f"log{self.n}.jsonl", config_path=self.tmp / "empty.conf", deadline=deadline, keep_awake=False)
        return run, run_id

    def go(self, **kw) -> dict:
        run, run_id = self.make(**kw)
        result = run.run()
        result["rows"] = {}
        for row in self.history.list_files(run_id):
            result["rows"].setdefault(row["name"], []).append(row)
        result["log"] = (self.tmp / f"log{self.n}.jsonl").read_text(encoding="utf-8") if (self.tmp / f"log{self.n}.jsonl").exists() else ""
        return result

    def local_files(self) -> list[str]:
        return sorted(p.relative_to(self.dest).as_posix() for p in self.dest.rglob("*") if p.is_file()) if self.dest.exists() else []

    # ------------------------------------------------------------------------------------------------ reruns
    def test_rerun_of_identical_files_is_nothing_new(self) -> None:
        self.srv.put("Src/a.mkv", b"a" * 1000)
        self.assertEqual(self.go()["files"], 1)
        again = self.go()
        self.assertEqual((again["state"], again["files"], again["message"]), ("ok", 0, "Nothing new to download"))

    def test_changed_file_is_replaced(self) -> None:
        self.srv.put("Src/a.mkv", b"new-content-longer")
        self.dest.mkdir()
        (self.dest / "a.mkv").write_bytes(b"old")
        result = self.go()
        self.assertEqual((result["state"], result["files"]), ("ok", 1))
        self.assertEqual((self.dest / "a.mkv").read_bytes(), b"new-content-longer")

    def test_move_after_an_earlier_copy_is_recorded_as_already_downloaded(self) -> None:
        """A crash between copy and delete leaves the file local but still on Nextcloud: the next move only deletes it."""
        self.srv.put("Src/a.mkv", b"a" * 1000, mtime=1_700_000_000)
        self.go(mode="copy")
        result = self.go(mode="move")
        self.assertEqual((result["state"], result["files"]), ("ok", 0))
        self.assertIn("already downloaded earlier and removed from Nextcloud", result["message"])
        (row,) = result["rows"]["a.mkv"]
        self.assertEqual((row["status"], row["size"], row["deleted"]), ("present", 1000, 1))
        self.assertEqual(self.srv.files("Src"), [])
        self.assertEqual((self.dest / "a.mkv").stat().st_size, 1000)

    def test_move_replaces_a_different_local_file_then_deletes(self) -> None:
        self.srv.put("Src/a.mkv", b"remote-version-123")
        self.dest.mkdir()
        (self.dest / "a.mkv").write_bytes(b"local")
        result = self.go(mode="move")
        self.assertEqual((result["state"], result["files"]), ("ok", 1))
        self.assertEqual(((self.dest / "a.mkv").read_bytes(), self.srv.files("Src")), (b"remote-version-123", []))

    # ----------------------------------------------------------------------------------------------- names
    def test_unusual_names_all_arrive_with_correct_sizes(self) -> None:
        names = ["Café ☕ (2026) [x]#1+2.mkv", "two  spaces.txt", "ünï.txt", "日本語.txt", "emoji 😀.txt", "semi;colon.txt", "'quote'.txt", "dollar$.txt", "at@sign.txt", "hash#.txt",
                 "percent%25.txt", "plus+plus.txt", "tilde~.txt", "amp&amp.txt", "comma,comma.txt"]
        for name in names:
            self.srv.put(f"Src/{name}", name.encode())
        result = self.go()
        self.assertEqual((result["state"], result["files"], result["bytes"]), ("ok", 15, sum(len(n.encode()) for n in names)))
        self.assertEqual(sorted(result["rows"]), sorted(names))

    @unittest.skipUnless(sys.platform == "win32", "Windows file-name rules")
    def test_names_windows_cannot_store_are_encoded_but_sized_correctly(self) -> None:
        names = ["colon:name.txt", "star*.txt", "q?.txt", "pipe|.txt", "lt<gt>.txt", 'dq".txt', "trailing.dot.", "trailing space ", "CON.txt", "nul", " leading.txt", "ok.txt"]
        for name in names:
            self.srv.put(f"Src/{name}", name.encode())
        result = self.go()
        self.assertEqual((result["state"], result["files"]), ("ok", len(names)))
        for name in names:   # History uses the REMOTE name and the true size even when the local name is encoded
            self.assertEqual(result["rows"][name][0]["size"], len(name.encode()), name)
        self.assertIn("colon：name.txt", self.local_files())

    def test_very_long_paths(self) -> None:
        deep = "/".join(f"level{n:02d}-{'x' * 22}" for n in range(12))
        self.srv.put(f"Src/{deep}/{'f' * 80}.mkv", b"deep")
        result = self.go()
        self.assertEqual((result["state"], result["files"], result["bytes"]), ("ok", 1, 4))

    def test_zero_byte_files_and_empty_folders_in_a_move(self) -> None:
        self.srv.put("Src/empty.txt", b"")
        self.srv.mkdir("Src/EmptyDir")
        result = self.go(mode="move")
        self.assertEqual((result["state"], result["files"], result["bytes"]), ("ok", 1, 0))
        self.assertEqual((self.srv.files("Src"), self.srv.folders("Src")), ([], []))

    def test_empty_source_and_single_file_source(self) -> None:
        self.srv.mkdir("Src")
        self.assertEqual(self.go()["message"], "Nothing new to download")
        self.srv.put("Src/only.txt", b"x" * 10)
        result = self.go(source="Src/only.txt")
        self.assertEqual((result["state"], result["files"]), ("ok", 1))

    # ------------------------------------------- the data-loss guard: names that clash on a case-insensitive disk
    @unittest.skipUnless(sys.platform == "win32", "case-insensitive destination")
    def test_names_that_differ_only_by_case_are_left_alone_in_copy_and_move(self) -> None:
        self.srv.put("Src/Movie.mkv", b"upper-case-version")
        self.srv.put("Src/movie.mkv", b"lower-case-version-longer")
        self.srv.put("Src/fine.mkv", b"fine")
        for mode in ("copy", "move"):
            result = self.go(mode=mode)
            self.assertEqual(result["state"], "partial", result)
            self.assertIn("2 skipped because their names clash", result["message"])
            self.assertEqual({n: r[0]["status"] for n, r in result["rows"].items() if r[0]["status"] == "skipped"}, {"Movie.mkv": "skipped", "movie.mkv": "skipped"})
            self.assertEqual(self.local_files().count("fine.mkv") + len([f for f in self.local_files() if f.lower() == "movie.mkv"]), 1 if mode == "copy" else 1)
            self.assertFalse(any(f.lower() == "movie.mkv" for f in self.local_files()))   # neither clashing file was written...
            self.assertEqual(sorted(self.srv.files("Src")), ["Movie.mkv", "fine.mkv", "movie.mkv"] if mode == "copy" else ["Movie.mkv", "movie.mkv"])   # ...and a move never deleted them
            shutil_rmtree(self.dest)

    # ----------------------------------------------------------------------------------- pre-check failures
    def test_precheck_reports_the_real_problem_before_rclone_starts(self) -> None:
        self.srv.put("Src/a.mkv", b"a")
        missing = self.go(source="Nope/Gone")
        self.assertEqual((missing["state"], missing["message"]), ("failed", "The Nextcloud folder /Nope/Gone was not found"))
        wrong = self.go(creds={**self.CREDS, "app_password": "WRONG", "server": self.base})
        self.assertEqual((wrong["state"], wrong["message"]), ("failed", "Nextcloud rejected the saved login. Disconnect and connect again."))
        self.assertEqual(self.local_files(), [])

    def test_a_200_html_page_instead_of_webdav_is_not_mistaken_for_an_empty_folder(self) -> None:
        self.srv.put("Src/a.mkv", b"a")
        self.srv.fault("PROPFIND", "Src", "status", status=200, body=b"<html><body>Maintenance</body></html>")
        result = self.go()
        self.assertEqual(result["state"], "failed")
        self.assertIn("did not answer like Nextcloud WebDAV", result["message"])

    def test_maintenance_503_is_retried_then_fails_with_a_clear_message(self) -> None:
        self.srv.put("Src/a.mkv", b"a")
        self.srv.fault("PROPFIND", "Src", "status", status=503)
        started = time.time()
        result = self.go()
        self.assertEqual(result["state"], "failed")
        self.assertIn("not available right now (HTTP 503)", result["message"])
        self.assertLess(time.time() - started, 20)
        self.assertEqual(sum(1 for m, p in self.srv.requests if m == "PROPFIND"), 3)   # three pre-check attempts

    def test_a_transient_503_in_the_precheck_recovers(self) -> None:
        self.srv.put("Src/a.mkv", b"a")
        self.srv.fault("PROPFIND", "Src", "status", status=503, times=1)
        self.assertEqual(self.go()["state"], "ok")

    def test_server_down_fails_quickly_with_a_clear_message(self) -> None:
        self.srv.put("Src/a.mkv", b"a")
        base = self.base
        self.srv.stop()
        result = self.go(creds={**self.CREDS, "server": base})
        self.assertEqual(result["state"], "failed")
        self.assertIn("Cannot reach the Nextcloud server", result["message"])

    # -------------------------------------------------------------------------- failures while transferring
    def test_one_forbidden_file_is_one_row_one_error_and_a_clear_message(self) -> None:
        self.srv.put("Src/ok.mkv", b"ok" * 100)
        self.srv.put("Src/forbidden.mkv", b"no" * 100)
        self.srv.fault("GET", "forbidden.mkv", "status", status=403)
        result = self.go()
        self.assertEqual((result["state"], result["files"], result["errors"], result["exit_code"]), ("partial", 1, 1, 1))
        self.assertEqual(result["message"], "1 file(s) transferred; problem: forbidden.mkv: Nextcloud refused access (HTTP 403 Forbidden)")
        self.assertEqual(len(result["rows"]["forbidden.mkv"]), 1)   # retries do not multiply rows
        self.assertEqual(result["rows"]["forbidden.mkv"][0]["status"], "error")

    def test_a_file_that_vanishes_after_listing(self) -> None:
        self.srv.put("Src/ok.mkv", b"ok")
        self.srv.put("Src/vanished.mkv", b"gone")
        self.srv.fault("GET", "vanished.mkv", "status", status=404)
        result = self.go()
        self.assertEqual((result["state"], result["errors"]), ("partial", 1))
        self.assertIn("vanished.mkv: not found on Nextcloud (HTTP 404)", result["message"])

    def test_several_failing_files_are_summarised(self) -> None:
        for n in range(3):
            self.srv.put(f"Src/bad{n}.mkv", b"x")
            self.srv.fault("GET", f"bad{n}.mkv", "status", status=403)
        self.srv.put("Src/good.mkv", b"g")
        result = self.go()
        self.assertEqual((result["state"], result["errors"]), ("partial", 3))
        self.assertIn("(and 2 more)", result["message"])

    def test_everything_failing_is_failed(self) -> None:
        self.srv.put("Src/bad.mkv", b"x")
        self.srv.fault("GET", "bad.mkv", "status", status=403)
        self.assertEqual(self.go()["state"], "failed")

    def test_server_hiccups_are_retried_by_rclone(self) -> None:
        self.srv.put("Src/flaky.mkv", b"f" * 5000)
        self.srv.put("Src/limited.mkv", b"l" * 5000)
        self.srv.fault("GET", "flaky.mkv", "status", status=503, times=2)
        self.srv.fault("GET", "limited.mkv", "status", status=429, times=2)
        result = self.go()
        self.assertEqual((result["state"], result["files"], result["errors"]), ("ok", 2, 0))

    def test_a_connection_dropped_mid_file_is_resumed_and_the_content_is_intact(self) -> None:
        data = os.urandom(2_000_000)
        self.srv.put("Src/dropped.mkv", data)
        self.srv.fault("GET", "dropped.mkv", "drop", after_bytes=500_000)   # EVERY request is cut after 500 KB
        result = self.go()
        self.assertEqual((result["state"], result["files"]), ("ok", 1))
        self.assertEqual(sha((self.dest / "dropped.mkv").read_bytes()), sha(data))

    def test_corruption_is_invisible_without_checksums_and_caught_with_them(self) -> None:
        data = os.urandom(50_000)
        self.srv.put("Src/bad.mkv", data)
        self.srv.fault("GET", "bad.mkv", "corrupt")
        silent = self.go()
        self.assertEqual(silent["state"], "ok")                                 # documented limitation: size-only comparison...
        self.assertNotEqual(sha((self.dest / "bad.mkv").read_bytes()), sha(data))   # ...so a corrupted download is accepted
        shutil_rmtree(self.dest)
        self.srv.checksums = True
        caught = self.go(checksum=True)
        self.assertEqual((caught["state"], caught["errors"]), ("failed", 1))
        self.assertIn("bad.mkv: the downloaded file did not match the checksum", caught["message"])
        self.assertIn("bad.mkv", caught["rows"])
        self.assertFalse(any(name.endswith(".partial") for name in caught["rows"]))      # temp names never leak into History
        self.assertEqual(self.local_files(), [])

    def test_a_persistent_500_does_not_stall_the_run_for_minutes(self) -> None:
        self.srv.put("Src/ok.mkv", b"ok")
        self.srv.put("Src/broken.mkv", b"x")
        self.srv.fault("GET", "broken.mkv", "status", status=500)
        started = time.time()
        result = self.go(retries=0)
        self.assertLess(time.time() - started, 150)                             # was 189 s per attempt with low-level-retries 10
        self.assertEqual(result["state"], "partial")
        self.assertIn("broken.mkv: the Nextcloud server had an internal error (HTTP 500)", result["message"])

    # --------------------------------------------------------------------- delete failures in Move mode
    def test_move_with_a_forbidden_delete_keeps_the_file_and_reports_it_once(self) -> None:
        self.srv.put("Src/a.mkv", b"a" * 100)
        self.srv.put("Src/locked.mkv", b"l" * 100)
        self.srv.put("Src/Sub/s.mkv", b"s" * 100)
        self.srv.forbid_delete = {"locked.mkv"}
        result = self.go(mode="move")
        self.assertEqual((result["state"], result["files"], result["errors"]), ("partial", 3, 1))
        self.assertEqual(sorted(self.local_files()), ["Sub/s.mkv", "a.mkv", "locked.mkv"])          # everything was downloaded
        self.assertEqual(self.srv.files("Src"), ["locked.mkv"])                                       # only the undeletable file is left on Nextcloud
        self.assertIn("1 file(s) downloaded but not deleted on Nextcloud, e.g. locked.mkv: Nextcloud refused access (HTTP 403 Forbidden)", result["message"])
        (row,) = result["rows"]["locked.mkv"]                                                         # ONE row: downloaded OK, with a note
        self.assertEqual((row["status"], row["deleted"]), ("ok", 0))
        self.assertIn("could not be deleted on Nextcloud", row["error"])
        self.assertEqual(result["rows"]["a.mkv"][0]["deleted"], 1)
        self.assertNotIn("webdav root", " ".join(result["rows"]))                                      # no pseudo-file rows

    def test_move_delete_failures_of_other_kinds(self) -> None:
        for code, text in ((404, "not found on Nextcloud (HTTP 404)"), (500, "the Nextcloud server had an internal error (HTTP 500)")):
            self.srv.faults.clear()
            self.srv.put("Src/a.mkv", b"a" * 100)
            self.srv.fault("DELETE", "a.mkv", "status", status=code)
            result = self.go(mode="move", retries=0)
            self.assertEqual((result["state"], result["errors"]), ("partial", 1), code)
            self.assertIn(text, result["message"])
            self.assertTrue((self.dest / "a.mkv").exists())
            shutil_rmtree(self.dest)
            self.srv.put("Src/a.mkv", b"a" * 100)

    # ------------------------------------------------------------------------------------------------ filters
    def test_filter_semantics(self) -> None:
        for name in ["a.mkv", "a.log", "Sub/b.mkv", "Sub/b.log", "Sub/Deep/c.mkv", "Skip/d.mkv", ".hidden.mkv", "Thumbs.db"]:
            self.srv.put(f"Src/{name}", name.encode())
        self.go(excludes=["*.log", "Skip/**", "Thumbs.db"])
        self.assertEqual(self.local_files(), [".hidden.mkv", "Sub/Deep/c.mkv", "Sub/b.mkv", "a.mkv"])
        shutil_rmtree(self.dest)
        self.go(includes=["*.mkv"], excludes=["Skip/**"])      # an exclude beats an include (plain --include/--exclude flags would copy Skip/d.mkv)
        self.assertEqual(self.local_files(), [".hidden.mkv", "Sub/Deep/c.mkv", "Sub/b.mkv", "a.mkv"])
        shutil_rmtree(self.dest)
        self.go(excludes=["/a.mkv"])                           # a leading slash anchors at the source root
        self.assertNotIn("a.mkv", self.local_files())
        self.assertIn("Sub/b.mkv", self.local_files())

    def test_min_age(self) -> None:
        self.srv.put("Src/old.mkv", b"o", mtime=time.time() - 7200)
        self.srv.put("Src/new.mkv", b"n")
        self.go(min_age_minutes=5)
        self.assertEqual(self.local_files(), ["old.mkv"])

    def test_excluded_files_are_never_deleted_by_a_move(self) -> None:
        self.srv.put("Src/keep.log", b"log")
        self.srv.put("Src/take.mkv", b"mkv")
        self.go(mode="move", excludes=["*.log"])
        self.assertEqual(self.srv.files("Src"), ["keep.log"])

    # ---------------------------------------------------------------------------------- bad destinations
    def test_destination_problems_fail_with_a_clear_message(self) -> None:
        self.srv.put("Src/a.mkv", b"a")
        blocker = self.tmp / "iamafile"
        blocker.write_text("x")
        result = self.go(dest=str(blocker / "sub"))
        self.assertEqual(result["state"], "failed")
        self.assertIn("Cannot use the destination folder", result["message"])
        self.assertEqual(self.srv.files("Src"), ["a.mkv"])

    # -------------------------------------------------------------------------------------- scale and speed
    def test_many_files_in_parallel_all_recorded_with_exact_totals(self) -> None:
        total = 0
        for n in range(150):
            data = os.urandom(2000 + n)
            total += len(data)
            self.srv.put(f"Src/dir{n % 7}/file{n:03d}.mkv", data)
        result = self.go(transfers=4)
        self.assertEqual((result["state"], result["files"], result["bytes"]), ("ok", 150, total))
        rows = self.history.list_files(result["run_id"])
        self.assertEqual((len(rows), sum(r["size"] for r in rows)), (150, total))

    def test_multi_stream_download_of_a_big_file_is_recorded_and_intact(self) -> None:
        """Files above rclone's multi-thread cutoff (256 MiB) are fetched with several ranged streams and logged as
        'Multi-thread Copied (new)': the exact shape of the friend's nightly 2 GB movies. It was once missed by the parser."""
        data = os.urandom(1_000_000) * 280
        self.srv.put("Src/huge.mkv", data)
        result = self.go(streams=4)
        self.assertEqual((result["state"], result["files"], result["bytes"]), ("ok", 1, len(data)))
        self.assertEqual(sha((self.dest / "huge.mkv").read_bytes()), sha(data))
        self.assertGreater(sum(1 for m, _ in self.srv.requests if m == "GET"), 1)   # ranged streams were used
        self.assertIn("Multi-thread Copied", result["log"])
        self.assertEqual(result["rows"]["huge.mkv"][0]["size"], len(data))

    def test_multi_stream_move_deletes_after_the_big_file_is_verified(self) -> None:
        data = os.urandom(1_000_000) * 280
        self.srv.put("Src/huge.mkv", data)
        result = self.go(mode="move", streams=4)
        self.assertEqual((result["state"], result["files"]), ("ok", 1))
        (row,) = result["rows"]["huge.mkv"]
        self.assertEqual((row["status"], row["deleted"]), ("ok", 1))                      # a transfer, not "Already had"
        self.assertEqual((self.srv.files("Src"), sha((self.dest / "huge.mkv").read_bytes())), ([], sha(data)))

    # ------------------------------------------------------------------------------- interruptions
    def _background(self, run: RcloneRun) -> tuple[threading.Thread, dict]:
        box: dict = {}
        thread = threading.Thread(target=lambda: box.update(run.run()), daemon=True)
        thread.start()
        return thread, box

    def _wait_transferring(self, run: RcloneRun) -> None:
        for _ in range(150):
            if run.snapshot()["transferring"]:
                return
            time.sleep(0.2)
        self.fail("transfer never started")

    def test_cancel_while_the_server_hangs_stops_quickly(self) -> None:
        self.srv.put("Src/a.mkv", b"a" * 1000)
        run, _ = self.make()
        self.srv.fault("*", "", "delay", seconds=40)
        thread, box = self._background(run)
        time.sleep(3)
        started = time.time()
        run.cancel()
        thread.join(60)
        self.assertFalse(thread.is_alive())
        self.assertEqual(box["state"], "cancelled")
        self.assertLess(time.time() - started, 20)

    def test_cancelling_a_big_move_never_deletes_the_source(self) -> None:
        self.srv.put("Src/big.mkv", os.urandom(6_000_000))
        run, _ = self.make(mode="move", speed={"limit_mbps": 0.5, "timetable": ""}, streams=1)
        thread, box = self._background(run)
        self._wait_transferring(run)
        time.sleep(1.5)
        run.cancel()
        thread.join(60)
        self.assertEqual(box["state"], "cancelled")
        self.assertEqual(self.srv.files("Src"), ["big.mkv"])
        self.assertEqual(self.local_files(), [])
        self.assertEqual(list(self.dest.rglob("*.partial")), [])

    def test_killing_rclone_midway_is_reported_and_the_rerun_completes_cleanly(self) -> None:
        import psutil
        data = os.urandom(6_000_000)
        self.srv.put("Src/big.mkv", data)
        self.srv.put("Src/small.mkv", b"s" * 100)
        run, run_id = self.make(mode="move", speed={"limit_mbps": 0.5, "timetable": ""}, streams=1)
        thread, box = self._background(run)
        self._wait_transferring(run)
        time.sleep(1.5)
        for proc in psutil.process_iter(["name", "cmdline"]):
            if proc.info["name"] and "rclone" in proc.info["name"].lower() and any(str(self.tmp) in a for a in (proc.info["cmdline"] or [])):
                proc.kill()
        thread.join(60)
        self.assertEqual(box["state"], "failed")
        self.assertIn("ended unexpectedly", box["message"])
        self.assertEqual(self.srv.files("Src"), ["big.mkv", "small.mkv"])   # nothing was deleted on Nextcloud
        result = self.go(mode="move")
        self.assertEqual((result["state"], result["files"]), ("ok", 2))
        self.assertEqual(sha((self.dest / "big.mkv").read_bytes()), sha(data))
        self.assertEqual(list(self.dest.rglob("*.partial")), [])
        self.assertEqual(self.srv.files("Src"), [])

    def test_stop_by_deadline_ends_a_slow_run(self) -> None:
        self.srv.put("Src/big.mkv", os.urandom(6_000_000))
        run, _ = self.make(deadline=datetime.now() + timedelta(seconds=3), speed={"limit_mbps": 0.5, "timetable": ""}, streams=1)
        thread, box = self._background(run)
        thread.join(60)
        self.assertEqual(box["state"], "stopped")
        self.assertEqual(self.srv.files("Src"), ["big.mkv"])

    def test_a_dry_run_lists_what_would_happen_for_copy_and_move(self) -> None:
        self.srv.put("Src/a.mkv", b"a" * 100)
        self.srv.put("Src/Sub/b.mkv", b"b" * 50)
        for mode in ("copy", "move"):
            result = self.go(mode=mode, dry_run=True)
            self.assertEqual((result["state"], result["message"]), ("ok", "2 file(s) checked (dry-run)"))
            self.assertEqual(sorted((n, r[0]["status"], r[0]["size"]) for n, r in result["rows"].items()), [("Sub/b.mkv", "dry", 50), ("a.mkv", "dry", 100)])
            self.assertEqual((self.local_files(), self.srv.files("Src")), ([], ["Sub/b.mkv", "a.mkv"]))
        self.assertNotIn("DELETE", {m for m, _ in self.srv.requests})

    def test_invalid_rclone_options_still_leave_a_log_and_a_message(self) -> None:
        """If rclone dies before writing its own log (e.g. bad flags) the History must still say why."""
        self.srv.put("Src/a.mkv", b"a")
        run, run_id = self.make()
        run.job["streams"] = 0                     # bypasses clean_job on purpose: rclone rejects --multi-thread-streams 0? (or accepts): either way no crash
        result = run.run()
        self.assertIn(result["state"], ("ok", "failed"))
        self.assertTrue(result["message"])


def shutil_rmtree(path: Path) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
