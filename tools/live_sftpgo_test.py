"""MANUAL live test (not part of the unit tests): NextPull against a REAL SFTPGo / WebDAV server, with throw-away files in a test folder.

    set NEXTPULL_TEST_PW=<password of the test user>
    py -3 tools\\live_sftpgo_test.py http://192.168.1.10:8092 bentest [AccessKeyName AccessKeyValue]

It uses the real app code (bridge, engine, real rclone) with a clean in-memory profile, and checks: discovery, adding the connection (a wrong password is refused and
nothing is saved), browsing with unicode names, a dry run, a copy, a move of one sub-folder (source files deleted, the job's folder and the other folders kept),
the speed limit, and the 'job whose connection vanished' message. The server's test folder must hold the files described in docs/SFTPGO.md (section Test data).
It never uploads, renames or creates anything on the server."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

if len(sys.argv) < 3 or not os.environ.get("NEXTPULL_TEST_PW"):
    sys.exit(__doc__)
SERVER, USER, PASSWORD = sys.argv[1], sys.argv[2], os.environ["NEXTPULL_TEST_PW"]
KEY_NAME, KEY_VALUE = (sys.argv[3], sys.argv[4]) if len(sys.argv) >= 5 else ("", "")

import applog  # noqa: E402
from nextpull import NextPullApi  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(' - ' + detail) if detail and not ok else ''}")
    if not ok:
        failures.append(label)


def wait_run(api: NextPullApi, job_id: str) -> dict:
    api.run_job(job_id)
    for _ in range(600):
        time.sleep(0.5)
        runs = [run for run in api.history(20)["runs"] if run["job_id"] == job_id]
        if runs and runs[0]["state"] not in ("running",):
            return runs[0]
    raise SystemExit("run did not finish in 5 minutes")


tmp = Path(tempfile.mkdtemp(prefix="live-sftpgo-"))
api = NextPullApi(None, poll_seconds=0.05)
api._config.save_settings({"prevent_sleep": False, "check_updates": False, "notifications": False})
api.start_engine()
try:
    print("1. discovery and login")
    found = api.discover_server(SERVER)
    check("server recognised as SFTPGo / WebDAV", found.get("type") == "sftpgo", str(found))
    bad = api.add_connection({"server": SERVER, "user": USER, "password": PASSWORD + "x", "header_name": KEY_NAME, "header_value": KEY_VALUE})
    check("wrong password is refused", not bad["ok"], str(bad))
    check("nothing was saved after the refusal", api.list_connections()["connections"] == [])
    added = api.add_connection({"server": SERVER, "user": USER, "password": PASSWORD, "header_name": KEY_NAME, "header_value": KEY_VALUE, "label": "Live test"})
    check("correct login accepted and saved", added["ok"], str(added))
    conn = added["connection"]["id"]
    check("the password is not in the public view", PASSWORD not in repr(api.list_connections()))

    print("2. browsing")
    root = api.browse("", conn)
    check("root lists the virtual folder", root["ok"] and [e["name"] for e in root["entries"]] == ["Test"], str(root)[:200])
    names = {e["name"] for e in api.browse("Test", conn)["entries"]}
    for expected in ("Anime", "Apostrofo's", "Ranma ½", "È quasi magia Johnny", "big.mkv"):
        check(f"'{expected}' is listed", expected in names, str(sorted(names)))
    check("nested listing", {e["name"] for e in api.browse("Test/Ranma ½", conn)["entries"]} == {"Episodio 01 [BDRip].mkv"})

    print("3. dry run, then copy (everything except big.mkv)")
    dest = tmp / "copy"
    job = api.save_job({"name": "Live copy", "connection": conn, "source": "Test", "dest": str(dest), "mode": "copy", "excludes": ["big.mkv"], "min_age_minutes": 0, "dry_run": True})["job"]
    run = wait_run(api, job["id"])
    check("dry run finished and wrote nothing", run["state"] == "ok" and not dest.exists() or not any(dest.rglob("*")) , f"{run['state']} {run['message']}")
    job = api.save_job({**job, "dry_run": False})["job"]
    run = wait_run(api, job["id"])
    check("copy finished", run["state"] == "ok", f"{run['state']} {run['message']}")
    check("unicode and special names arrived", (dest / "Ranma ½" / "Episodio 01 [BDRip].mkv").is_file() and (dest / "È quasi magia Johnny" / "film (1989).mkv").is_file()
          and (dest / "Apostrofo's" / "it's.mkv").is_file() and (dest / "Film con spazi  e (parentesi).mkv").is_file())
    check("sizes match the server", (dest / "Ranma ½" / "Episodio 01 [BDRip].mkv").stat().st_size == 5_000_000)
    check("copy left the server untouched", "Anime" in {e["name"] for e in api.browse("Test", conn)["entries"]})

    print("4. speed limit on a 200 MB file (20 MB/s => about 10 s)")
    dest2 = tmp / "slow"
    job2 = api.save_job({"name": "Live limited", "connection": conn, "source": "Test", "dest": str(dest2), "mode": "copy", "includes": ["big.mkv"], "min_age_minutes": 0,
                         "speed": {"limit_mbps": 20, "timetable": ""}})["job"]
    started = time.time()
    run = wait_run(api, job2["id"])
    took = time.time() - started
    check("limited copy finished", run["state"] == "ok" and (dest2 / "big.mkv").stat().st_size == 200 * 1024 * 1024, f"{run['state']} {run['message']}")
    check("the limit was applied", took >= 7, f"took {took:.1f}s")

    print("5. move one sub-folder (Test/Anime)")
    dest3 = tmp / "moved"
    job3 = api.save_job({"name": "Live move", "connection": conn, "source": "Test/Anime", "dest": str(dest3), "mode": "move", "min_age_minutes": 0})["job"]
    run = wait_run(api, job3["id"])
    check("move finished", run["state"] == "ok", f"{run['state']} {run['message']}")
    check("file arrived", (dest3 / "[SubsPlease] Episode 01 (1080p).mkv").is_file())
    left = api.browse("Test/Anime", conn)
    check("source file was deleted on the server", left["ok"] and left["entries"] == [], str(left)[:200])
    check("the job's own folder was not removed", left["ok"])
    check("other folders are untouched", "Ranma ½" in {e["name"] for e in api.browse("Test", conn)["entries"]})

    print("6. messages and removal")
    check("a connection in use cannot be removed", not api.remove_connection(conn)["ok"])
    orphan = api.save_job({"name": "Orphan", "connection": "sg-gone01", "source": "Test", "dest": str(tmp / "o")})["job"]
    run = wait_run(api, orphan["id"])
    check("a job without its connection fails clearly", run["state"] == "failed" and "no longer exists" in run["message"], run["message"])
    log_text = "\n".join(e["message"] for e in applog.read_log(None, 5000))
    check("the password is not in the application log", PASSWORD not in log_text)
finally:
    api.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED: " + "; ".join(failures))
sys.exit(1 if failures else 0)
