"""Developer tool: runs the REAL rclone through many scenarios against tests/fake_nextcloud.py (with fault injection) and prints, for
each one, rclone's raw JSON log lines next to what NextPull concluded (state, message, per-file rows). Use it to see how rclone words
things before changing runner/parser code, and as the source for tests/test_rclone_matrix.py.

    py -3 tools\\rclone_probe.py                 # all light scenarios
    py -3 tools\\rclone_probe.py names fault_    # only scenarios whose name contains one of these words
    py -3 tools\\rclone_probe.py --heavy         # also the big-file scenarios
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
from fake_nextcloud import FakeNextcloud  # noqa: E402
from rclone_runner import RcloneRun, find_rclone  # noqa: E402
from store import HistoryDb, clean_job  # noqa: E402

RCLONE = find_rclone("", [ROOT])
CREDS = {"user": "alice", "app_password": "pw-1234"}
SCENARIOS: dict[str, callable] = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


class Env:
    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="probe-"))
        self.remote = self.tmp / "remote"
        self.dest = self.tmp / "dest"
        self.srv = FakeNextcloud(self.remote, user="alice", password="pw-1234")
        self.base = self.srv.start()
        self.history = HistoryDb(None)
        self.n = 0

    def job(self, **kw):
        base = {"name": "probe", "source": "Src", "dest": str(self.dest), "mode": "copy", "min_age_minutes": 0}
        return clean_job({**base, **kw})

    def make_run(self, job=None, creds=None, deadline=None, **kw):
        job = job or self.job(**kw)
        run_id = self.history.start_run(job, "manual")
        self.n += 1
        run = RcloneRun(rclone=RCLONE, job=job, creds=creds or {**CREDS, "server": self.base}, run_id=run_id, history=self.history, log_path=self.tmp / f"log{self.n}.jsonl",
                        config_path=self.tmp / "empty.conf", deadline=deadline, keep_awake=False)
        return run, run_id

    def run(self, **kw):
        run, run_id = self.make_run(**kw)
        started = time.time()
        result = run.run()
        result["seconds"] = round(time.time() - started, 1)
        result["log"] = self.tmp / f"log{self.n}.jsonl"
        return result, run_id

    def close(self) -> None:
        self.srv.stop(); self.history.close(); shutil.rmtree(self.tmp, ignore_errors=True)


def show(env: Env, result, run_id, title="") -> None:
    print(f"  -> {title}state={result['state']} exit={result.get('exit_code')} files={result['files']} bytes={result['bytes']} errors={result['errors']} {result['seconds']}s")
    print(f"     message: {result['message'][:170]}")
    for f in env.history.list_files(run_id):
        print(f"     file: {f['name']!r} {f['status']} size={f['size']} deleted={f['deleted']} err={f['error'][:90]!r}")
    try:
        for raw in Path(result["log"]).read_text(encoding="utf-8").splitlines():
            try:
                j = json.loads(raw)
                print(f"     log : {j.get('level','?'):7}| {str(j.get('msg','')).strip().splitlines()[0][:110] if str(j.get('msg','')).strip() else ''} | {j.get('object','')}")
            except ValueError:
                print(f"     raw : {raw[:110]}")
    except OSError:
        print("     (no log file)")
    left = sorted(p.relative_to(env.dest).as_posix() for p in env.dest.rglob("*") if p.is_file()) if env.dest.exists() else []
    print(f"     dest: {left[:8]}{' ...' if len(left) > 8 else ''}")


# ------------------------------------------------------------------------------------------------------------ scenarios
@scenario
def basic_copy(e: Env):
    e.srv.put("Src/a.mkv", b"a" * 1000); e.srv.put("Src/Sub/b.mkv", b"b" * 500)
    r, i = e.run(); show(e, r, i, "first run ")
    r, i = e.run(); show(e, r, i, "rerun (identical) ")


@scenario
def replaced_existing(e: Env):
    e.srv.put("Src/a.mkv", b"new-content-longer"); e.dest.mkdir(); (e.dest / "a.mkv").write_bytes(b"old")
    r, i = e.run(); show(e, r, i)


@scenario
def move_when_already_downloaded(e: Env):
    """The classic crash case: copy succeeded earlier, the source delete never happened, then a move runs."""
    e.srv.put("Src/a.mkv", b"a" * 1000, mtime=1_700_000_000)
    r, i = e.run(mode="copy"); show(e, r, i, "copy first ")
    r, i = e.run(mode="move"); show(e, r, i, "then move ")
    print("     remote left:", e.srv.files("Src"))


@scenario
def move_dest_exists_different(e: Env):
    e.srv.put("Src/a.mkv", b"remote-version-123"); e.dest.mkdir(); (e.dest / "a.mkv").write_bytes(b"local")
    r, i = e.run(mode="move"); show(e, r, i)
    print("     remote left:", e.srv.files("Src"))


@scenario
def names_unicode_special(e: Env):
    for n in ["Café ☕ (2026) [x]#1+2.mkv", "two  spaces.txt", "ünï.txt", "日本語.txt", "emoji 😀.txt", "semi;colon.txt", "'quote'.txt", "dollar$.txt", "at@sign.txt", "hash#.txt", "percent%25.txt", "plus+plus.txt", "tilde~.txt", "amp&amp.txt", "comma,comma.txt"]:
        e.srv.put(f"Src/{n}", n.encode())
    r, i = e.run(); show(e, r, i)
    got = sorted(p.name for p in e.dest.iterdir())
    print("     all 15 names present locally:", len(got) == 15)


@scenario
def names_windows_invalid(e: Env):
    for n in ["colon:name.txt", "star*.txt", "q?.txt", "pipe|.txt", "lt<gt>.txt", 'dq".txt', "trailing.dot.", "trailing space ", "CON.txt", "nul", "aux.mkv", " leading.txt", "ok.txt"]:
        e.srv.put(f"Src/{n}", n.encode())
    r, i = e.run(); show(e, r, i)
    print("     local names:", sorted(p.name for p in e.dest.iterdir()))


@scenario
def long_paths(e: Env):
    deep = "/".join(f"level{n:02d}-{'x' * 22}" for n in range(12))
    e.srv.put(f"Src/{deep}/{'f' * 80}.mkv", b"deep")
    r, i = e.run(); show(e, r, i)
    print("     path length of the destination file:", max((len(str(p)) for p in e.dest.rglob('*') if p.is_file()), default=0))


@scenario
def case_collision(e: Env):
    e.srv.put("Src/Movie.mkv", b"upper-case-version"); e.srv.put("Src/movie.mkv", b"lower-case-version-longer")
    r, i = e.run(); show(e, r, i)
    print("     local files:", [(p.name, p.stat().st_size) for p in e.dest.iterdir()])


@scenario
def case_collision_move(e: Env):
    """DANGER check: two remote files that differ only by case, MOVE to a case-insensitive (Windows) destination."""
    e.srv.put("Src/Movie.mkv", b"upper-case-version"); e.srv.put("Src/movie.mkv", b"lower-case-version-longer")
    r, i = e.run(mode="move"); show(e, r, i)
    print("     local files:", [(p.name, p.stat().st_size) for p in e.dest.iterdir()])
    print("     remote left:", e.srv.files("Src"))


@scenario
def zero_byte_and_empty_dirs(e: Env):
    e.srv.put("Src/empty.txt", b""); e.srv.mkdir("Src/EmptyDir")
    r, i = e.run(mode="move"); show(e, r, i)
    print("     remote left:", e.srv.files("Src"), "folders:", e.srv.folders("Src"))


@scenario
def empty_source(e: Env):
    e.srv.mkdir("Src")
    r, i = e.run(); show(e, r, i)


@scenario
def source_is_a_file(e: Env):
    e.srv.put("Src/only.txt", b"x" * 10)
    r, i = e.run(source="Src/only.txt"); show(e, r, i)


@scenario
def source_missing(e: Env):
    e.srv.mkdir("Other")
    r, i = e.run(source="Nope/Gone"); show(e, r, i)


@scenario
def fault_get_403(e: Env):
    e.srv.put("Src/ok.mkv", b"ok" * 100); e.srv.put("Src/forbidden.mkv", b"no" * 100)
    e.srv.fault("GET", "forbidden.mkv", "status", status=403)
    r, i = e.run(); show(e, r, i)


@scenario
def fault_get_404_vanished(e: Env):
    e.srv.put("Src/ok.mkv", b"ok" * 100); e.srv.put("Src/vanished.mkv", b"gone" * 100)
    e.srv.fault("GET", "vanished.mkv", "status", status=404)
    r, i = e.run(); show(e, r, i)


@scenario
def fault_get_500_always(e: Env):
    e.srv.put("Src/ok.mkv", b"ok" * 100); e.srv.put("Src/broken.mkv", b"x" * 100)
    e.srv.fault("GET", "broken.mkv", "status", status=500)
    r, i = e.run(retries=1); show(e, r, i)


@scenario
def fault_503_twice_then_ok(e: Env):
    e.srv.put("Src/flaky.mkv", b"f" * 5000)
    e.srv.fault("GET", "flaky.mkv", "status", status=503, times=2)
    r, i = e.run(); show(e, r, i)


@scenario
def fault_429_then_ok(e: Env):
    e.srv.put("Src/limited.mkv", b"l" * 5000)
    e.srv.fault("GET", "limited.mkv", "status", status=429, times=2)
    r, i = e.run(); show(e, r, i)


@scenario
def fault_drop_once_then_ok(e: Env):
    e.srv.put("Src/dropped.mkv", os.urandom(2_000_000))
    e.srv.fault("GET", "dropped.mkv", "drop", after_bytes=500_000, times=1)
    r, i = e.run(); show(e, r, i)


@scenario
def fault_drop_always(e: Env):
    e.srv.put("Src/dropped.mkv", os.urandom(2_000_000)); e.srv.put("Src/fine.mkv", b"f" * 100)
    e.srv.fault("GET", "dropped.mkv", "drop", after_bytes=500_000)
    r, i = e.run(retries=1); show(e, r, i)


@scenario
def fault_corrupt_without_checksum(e: Env):
    e.srv.put("Src/bad.mkv", os.urandom(50_000))
    e.srv.fault("GET", "bad.mkv", "corrupt")
    r, i = e.run(); show(e, r, i)


@scenario
def fault_corrupt_with_checksum(e: Env):
    e.srv.checksums = True
    e.srv.put("Src/bad.mkv", os.urandom(50_000)); e.srv.put("Src/good.mkv", os.urandom(50_000))
    e.srv.fault("GET", "bad.mkv", "corrupt")
    r, i = e.run(checksum=True, retries=1); show(e, r, i)


@scenario
def fault_propfind_403(e: Env):
    e.srv.put("Src/a.mkv", b"a")
    e.srv.fault("PROPFIND", "Src", "status", status=403)
    r, i = e.run(); show(e, r, i)


@scenario
def fault_propfind_500(e: Env):
    e.srv.put("Src/a.mkv", b"a")
    e.srv.fault("PROPFIND", "Src", "status", status=500)
    r, i = e.run(retries=1); show(e, r, i)


@scenario
def fault_delete_403_move(e: Env):
    e.srv.put("Src/a.mkv", b"a" * 100); e.srv.put("Src/locked.mkv", b"l" * 100); e.srv.put("Src/Sub/s.mkv", b"s" * 100)
    e.srv.forbid_delete = {"locked.mkv"}
    r, i = e.run(mode="move"); show(e, r, i)
    print("     remote left:", e.srv.files("Src"), "folders:", e.srv.folders("Src"))


@scenario
def fault_delete_404_move(e: Env):
    e.srv.put("Src/a.mkv", b"a" * 100)
    e.srv.fault("DELETE", "a.mkv", "status", status=404)
    r, i = e.run(mode="move"); show(e, r, i)


@scenario
def fault_delete_500_move(e: Env):
    e.srv.put("Src/a.mkv", b"a" * 100)
    e.srv.fault("DELETE", "a.mkv", "status", status=500)
    r, i = e.run(mode="move", retries=1); show(e, r, i)


@scenario
def fault_wrong_password(e: Env):
    e.srv.put("Src/a.mkv", b"a")
    r, i = e.run(creds={**CREDS, "app_password": "WRONG", "server": e.base}); show(e, r, i)


@scenario
def fault_server_down(e: Env):
    e.srv.put("Src/a.mkv", b"a")
    base = e.base; e.srv.stop()
    r, i = e.run(creds={**CREDS, "server": base}, retries=0); show(e, r, i)


@scenario
def fault_html_instead_of_webdav(e: Env):
    e.srv.put("Src/a.mkv", b"a")
    e.srv.fault("PROPFIND", "Src", "status", status=200, body=b"<html><body>Maintenance mode</body></html>")
    r, i = e.run(retries=0); show(e, r, i)


@scenario
def fault_maintenance_503(e: Env):
    e.srv.put("Src/a.mkv", b"a")
    e.srv.fault("*", "", "status", status=503)
    r, i = e.run(retries=1); show(e, r, i)


@scenario
def filters_semantics(e: Env):
    for n in ["a.mkv", "a.log", "Sub/b.mkv", "Sub/b.log", "Sub/Deep/c.mkv", "Skip/d.mkv", ".hidden.mkv", "Thumbs.db"]:
        e.srv.put(f"Src/{n}", n.encode())
    r, i = e.run(excludes=["*.log", "Skip/**", "Thumbs.db"]); show(e, r, i, "excludes ")
    shutil.rmtree(e.dest, ignore_errors=True)
    r, i = e.run(includes=["*.mkv"], excludes=["Skip/**"]); show(e, r, i, "includes+excludes ")
    shutil.rmtree(e.dest, ignore_errors=True)
    r, i = e.run(excludes=["/a.mkv"]); show(e, r, i, "anchored exclude ")


@scenario
def min_age(e: Env):
    e.srv.put("Src/old.mkv", b"o", mtime=time.time() - 7200); e.srv.put("Src/new.mkv", b"n")
    r, i = e.run(min_age_minutes=5); show(e, r, i)


@scenario
def bad_options(e: Env):
    e.srv.put("Src/a.mkv", b"a")
    try:
        e.job(speed={"limit_mbps": 0, "timetable": "garbage nonsense"})
    except ValueError as error:
        print("  -> bad timetable rejected before any run:", error)
    r, i = e.run(speed={"limit_mbps": 0, "timetable": "08:00,2M 23:00,off"}); show(e, r, i, "good timetable ")


@scenario
def destination_problems(e: Env):
    e.srv.put("Src/a.mkv", b"a")
    blocker = e.tmp / "iamafile"; blocker.write_text("x")
    r, i = e.run(dest=str(blocker / "sub")); show(e, r, i, "dest under a file ")
    r, i = e.run(dest="Q:\\definitely\\not\\here" if os.name == "nt" else "/proc/definitely/not/here"); show(e, r, i, "dest on a missing drive ")


@scenario
def many_files_parallel(e: Env):
    for n in range(150):
        e.srv.put(f"Src/dir{n % 7}/file{n:03d}.mkv", os.urandom(2000 + n))
    r, i = e.run(transfers=4); show(e, r, i)
    print("     history rows:", len(e.history.list_files(i)), "| sizes sum to bytes:", sum(f["size"] for f in e.history.list_files(i)) == r["bytes"])


@scenario
def slow_server_cancel(e: Env):
    e.srv.put("Src/a.mkv", b"a" * 1000)
    e.srv.fault("*", "", "delay", seconds=40)
    run, run_id = e.make_run()
    box = {}
    t = threading.Thread(target=lambda: box.update(run.run()), daemon=True); t.start()
    time.sleep(4); started = time.time(); run.cancel(); t.join(60)
    print(f"  -> cancelled while the server hangs: state={box.get('state')} took {time.time() - started:.1f}s to stop, thread alive={t.is_alive()}")


@scenario
def cancel_move_big(e: Env):
    e.srv.put("Src/big.mkv", os.urandom(6_000_000))
    run, run_id = e.make_run(mode="move", speed={"limit_mbps": 0.5, "timetable": ""}, streams=1)
    box = {}
    t = threading.Thread(target=lambda: box.update(run.run()), daemon=True); t.start()
    for _ in range(100):
        if run.snapshot()["transferring"]:
            break
        time.sleep(0.2)
    time.sleep(2); run.cancel(); t.join(60)
    print(f"  -> state={box.get('state')} exit={box.get('exit_code')}; source still on Nextcloud: {(e.remote / 'Src' / 'big.mkv').exists()}; local leftovers: {[p.name for p in e.dest.rglob('*')]}")


@scenario
def kill_rclone_midway_then_rerun(e: Env):
    e.srv.put("Src/big.mkv", os.urandom(6_000_000)); e.srv.put("Src/small.mkv", b"s" * 100)
    import psutil
    run, run_id = e.make_run(mode="move", speed={"limit_mbps": 0.5, "timetable": ""}, streams=1)
    box = {}
    t = threading.Thread(target=lambda: box.update(run.run()), daemon=True); t.start()
    for _ in range(100):
        if run.snapshot()["transferring"]:
            break
        time.sleep(0.2)
    time.sleep(2)
    for proc in psutil.process_iter(["name", "cmdline"]):
        if proc.info["name"] and "rclone" in proc.info["name"].lower() and any("probe-" in a for a in (proc.info["cmdline"] or [])):
            proc.kill()
    t.join(60)
    print(f"  -> rclone killed: state={box.get('state')} exit={box.get('exit_code')} msg={box.get('message', '')[:100]!r}")
    print("     leftovers after the kill:", sorted(p.relative_to(e.dest).as_posix() for p in e.dest.rglob("*") if p.is_file()))
    r, i = e.run(mode="move"); show(e, r, i, "rerun ")


@scenario
def heavy_multi_stream_280mb(e: Env):
    import hashlib
    data = os.urandom(1_000_000) * 280
    e.srv.put("Src/huge.mkv", data)
    r, i = e.run(streams=4); show(e, r, i)
    got = (e.dest / "huge.mkv")
    print("     intact:", got.is_file() and hashlib.sha256(got.read_bytes()).digest() == hashlib.sha256(data).digest(), "| GET requests (ranged streams):", sum(1 for m, _ in e.srv.requests if m == "GET"))


def main() -> None:
    if not RCLONE:
        sys.exit("real rclone not found (run tools\\Fetch-rclone.ps1)")
    words = [a for a in sys.argv[1:] if not a.startswith("--")]
    heavy = "--heavy" in sys.argv
    for name, fn in SCENARIOS.items():
        if words and not any(w in name for w in words):
            continue
        if name.startswith("heavy_") and not (heavy or words):
            continue
        print(f"\n=== {name}")
        env = Env()
        try:
            fn(env)
        except Exception as error:   # keep going: a probe crash is itself a finding
            import traceback
            print("  !! probe crashed:", repr(error)); traceback.print_exc()
        finally:
            env.close()


if __name__ == "__main__":
    main()
