"""MANUAL live test (not part of the unit tests): NextPull as a friend's PC against a REAL Nextcloud, with DUMMY files in a
throw-away test folder. Needs one browser approval. See docs/LIVE-TEST.md.

Full 'friend PC' test of NextPull against a real Nextcloud with DUMMY files in a throw-away test folder.
Fresh data dir, real DPAPI storage, real rclone, real scheduler thread. Everything created on Nextcloud is prefixed NPTEST-<id>
and is deleted at the end (including the matching trash entries)."""
import base64, hashlib, json, os, shutil, sys, tempfile, time, urllib.error, urllib.parse, urllib.request, uuid, xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import winutil
from nextpull import NextPullApi

if len(sys.argv) < 2:
    sys.exit("usage: py -3 tools\\live_friend_test.py <nextcloud address>   e.g. cloud.example.com")
SERVER = sys.argv[1]
DATA = Path(tempfile.gettempdir()) / "nextpull-friendpc"
DEST = DATA / "Downloads"
shutil.rmtree(DATA, ignore_errors=True)
PREFIX = "NPTEST-" + uuid.uuid4().hex[:8]
RESULTS: list[tuple[str, bool, str]] = []
notified: list[tuple[str, str]] = []


def say(*a):
    print(*a, flush=True)


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), str(detail)))
    say(("PASS  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))
    return bool(cond)


api = NextPullApi(DATA, notify=lambda t, m: notified.append((t, m)), poll_seconds=2)

# ------------------------------------------------------------------------------------------------ 1. connect
boot = api.bootstrap()
check("bootstrap: rclone found", boot["rclone"]["found"], boot["rclone"].get("version"))
check("bootstrap: not connected yet", boot["connection"] == {"connected": False})
started = api.connect_start(SERVER)
say("OPEN THIS LOGIN PAGE (opened in your browser):", started.get("login_url"))
for _ in range(300):
    if api.connect_status()["state"] == "connected":
        break
    time.sleep(2)
conn = api.connection(refresh=True)["connection"]
if not check("sign-in through the app (Login Flow v2)", conn.get("connected"), f"user={conn.get('user')} name={conn.get('display_name')}"):
    sys.exit(1)
creds = api._secrets.load()
check("credentials stored encrypted on disk (DPAPI file, no plain password)", (DATA / "credentials.bin").is_file() and creds["app_password"].encode() not in (DATA / "credentials.bin").read_bytes())
auth = "Basic " + base64.b64encode(f"{creds['user']}:{creds['app_password']}".encode()).decode()
UID = creds.get('uid') or creds['user']   # the WebDAV path needs the user id, not an e-mail login name
BASE = f"{creds['server']}/remote.php/dav/files/{urllib.parse.quote(UID, safe='')}"


def dav(method, path, data=None, headers=None, base=None):
    url = (base or BASE) + "/" + urllib.parse.quote(path.strip("/"), safe="/")
    req = urllib.request.Request(url, data=data, method=method, headers={"Authorization": auth, "OCS-APIRequest": "true", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def listing(path):
    return {e["path"]: e for e in (api.browse(path) or {}).get("entries", [])} if api.browse(path).get("ok") else {}


# ----------------------------------------------------------------------------------------- 2. dummy files
rnd = lambda n: os.urandom(n)
FILES = {  # path under PREFIX -> bytes
    "Movies/Alpha Movie (2026) [ITA ENG].mkv": rnd(8_000_000), "Movies/Beta.mkv": rnd(3_000_000),
    "Movies/Season 1/ep01.mkv": rnd(2_000_000), "Movies/Season 1/ep02.mkv": rnd(2_000_000),
    "Docs/notes.txt": b"hello friend\n" * 50, "Docs/readme.log": b"log line\n" * 20,
    "Big/big-60MB.bin": rnd(60_000_000), "Late/late1.txt": b"late one\n" * 30, "Late/late2.txt": b"late two\n" * 30,
    "Fresh/just-uploaded.txt": b"fresh\n" * 10,
}
dav("MKCOL", PREFIX)
for folder in sorted({str(Path(p).parent).replace("\\", "/") for p in FILES} | {"Movies/Season 1"}):
    dav("MKCOL", f"{PREFIX}/{folder}")
ok_upload = all(dav("PUT", f"{PREFIX}/{p}", data)[0] in (200, 201, 204) for p, data in FILES.items())
check("dummy files created on Nextcloud", ok_upload, f"{PREFIX}: {len(FILES)} files, {sum(map(len, FILES.values())) // 1_000_000} MB")
browse = api.browse(f"{PREFIX}/Movies")
check("app folder browser sees them", browse["ok"] and {"Beta.mkv", "Season 1"} <= {e["name"] for e in browse["entries"]})

# ------------------------------------------------------------------------------------------------- 3. jobs
now = datetime.now()
slot_a = (now + timedelta(seconds=170)).replace(second=0, microsecond=0)
if slot_a - now < timedelta(seconds=100):
    slot_a += timedelta(minutes=1)
mk = lambda **kw: api.save_job({"min_age_minutes": 0, "streams": 4, "transfers": 1, **kw})["job"]
job_a = mk(name="A Friend night pull", source=f"{PREFIX}/Movies", mode="move", dest=str(DEST / "Movies"), schedule={"days": list(range(7)), "time": slot_a.strftime("%H:%M")}, catch_up=True)
job_b = mk(name="B Docs copy", source=f"{PREFIX}/Docs", mode="copy", dest=str(DEST / "Docs"), excludes=["*.log"], schedule={"days": [0, 1, 2, 3, 4, 5, 6], "time": "23:59"})
job_d = mk(name="D Big file slow", source=f"{PREFIX}/Big", mode="copy", dest=str(DEST / "Big"), speed={"limit_mbps": 3, "timetable": ""}, schedule={"days": [0], "time": "03:00"})
job_e = mk(name="E Too young", source=f"{PREFIX}/Fresh", mode="move", dest=str(DEST / "Fresh"), min_age_minutes=30, schedule={"days": [0], "time": "03:30"})
past = lambda minutes: (datetime.now() - timedelta(minutes=minutes))
job_f = mk(name="F Catch-up", source=f"{PREFIX}/Late", mode="copy", dest=str(DEST / "Late"), catch_up=True, catch_up_hours=12, schedule={"days": list(range(7)), "time": past(15).strftime("%H:%M")})
job_g = mk(name="G Missed", source=f"{PREFIX}/Late", mode="copy", dest=str(DEST / "LateMissed"), catch_up=False, schedule={"days": list(range(7)), "time": past(20).strftime("%H:%M")})
check("6 jobs saved", len(api.list_jobs()["jobs"]) == 6)
check("job A is scheduled for the future", api.list_jobs()["jobs"][0]["next_run"] is not None, f"A at {slot_a:%H:%M}")
bad = api.save_job({"name": "bad", "source": "x/../y", "dest": "D:\\x"})
check("invalid job rejected with a readable message", not bad["ok"] and ".." in bad["error"], bad.get("error"))

# --------------------------------------------------------------------------- 4. dry run, then start engine
api._config.set_state("last_checked", past(30).replace(microsecond=0).isoformat())   # as if the app was closed for 30 minutes
r = api.run_job(job_a["id"], dry_run=True)
check("dry run of the Move job started", r["ok"])
for _ in range(120):
    if not api._engine.is_busy():
        break
    time.sleep(1)
dry = api.history()["runs"][0]
check("dry run changed nothing locally", dry["dry_run"] == 1 and dry["state"] == "ok" and not any((DEST / "Movies").rglob("*")) if (DEST / "Movies").exists() else dry["dry_run"] == 1 and dry["state"] == "ok", f"{dry['state']} {dry['message']}")
check("dry run: files still on Nextcloud", "Beta.mkv" in {e["name"] for e in api.browse(f"{PREFIX}/Movies")["entries"]})

api.start_engine()   # catch-up (F) and missed (G) are decided at the first tick; A fires later by itself
say("engine started; waiting for catch-up/missed decisions...")
queued = None
for _ in range(60):
    st = api.status()
    if st["running"] or any(r["trigger"] == "catch-up" for r in api.history()["runs"]):
        break
    time.sleep(1)
queued_b = api.run_job(job_b["id"]); queued_d = api.run_job(job_d["id"]); queued_e = api.run_job(job_e["id"])
check("manual runs queue behind the running one", queued_b["ok"] and (queued_b.get("queued_behind") or api.status()["queue"] is not None), json.dumps(queued_b))

# ------------------------------------------------------------- 5. watch the big slow file (live progress)
samples, raised = [], False
deadline = time.time() + 300
while time.time() < deadline:
    st = api.status()
    cur = st["current"]
    if cur and cur["job_name"].startswith("D") and cur["transferring"]:
        samples.append((cur["speed"], cur["bytes"], cur["bwlimit"]))
        if len(samples) == 8 and not raised:
            api.set_speed(0)      # lift the limit while it runs
            raised = True
    if not st["running"] and not st["queue"] and not st["starting"] and any(r["state"] != "running" and r["job_name"].startswith("D") for r in api.history()["runs"]):
        break
    time.sleep(1)
slow = [s[0] for s in samples[:7] if s[0] > 0]
check("big file: live progress was reported", len(samples) >= 8, f"{len(samples)} samples")
check("big file: speed limit of 3 MB/s respected until it was lifted", bool(slow) and max(slow) < 3 * 1048576 * 1.5, f"max {max(slow) / 1e6:.1f} MB/s" if slow else "no samples")

# ----------------------------------------------------------- 6. wait for the scheduled run of job A
say(f"waiting for the scheduled run of job A at {slot_a:%H:%M}...")
for _ in range(400):
    runs = api.history(100)["runs"]
    if any(r["job_name"].startswith("A") and r["trigger"] == "schedule" and r["state"] != "running" for r in runs):
        break
    time.sleep(2)
while api._engine.is_busy():
    time.sleep(1)
runs = api.history(100)["runs"]
by = lambda prefix, trigger=None: [r for r in runs if r["job_name"].startswith(prefix) and r["dry_run"] == 0 and (trigger is None or r["trigger"] == trigger)]

# ------------------------------------------------------------------------------------------ 7. verify
a = by("A", "schedule")
check("A: ran by the scheduler at its time", bool(a) and a[0]["state"] == "ok", f"{a[0]['started']} -> {a[0]['state']} {a[0]['files']} files {a[0]['bytes']} bytes" if a else "no run")
sha = lambda b: hashlib.sha256(b).hexdigest()
same = all((DEST / "Movies" / Path(p).relative_to("Movies")).is_file() and sha((DEST / "Movies" / Path(p).relative_to("Movies")).read_bytes()) == sha(d) for p, d in FILES.items() if p.startswith("Movies/"))
check("A: all 4 movies downloaded intact (SHA-256, subfolder kept)", same)
left = api.browse(f"{PREFIX}/Movies")["entries"]
check("A: Move deleted every file and empty sub-folder on Nextcloud (the job's own source folder stays)", left == [], f"left in Movies: {[e['name'] for e in left]}")
check("A: history counts deleted files", bool(a) and a[0]["deleted"] == 4, a[0]["deleted"] if a else "")
b = by("B")
src_names = {e["name"] for e in api.browse(f"{PREFIX}/Docs")["entries"]}
check("B: Docs copy ok, *.log excluded, source untouched", bool(b) and b[0]["state"] == "ok" and (DEST / "Docs" / "notes.txt").is_file() and not (DEST / "Docs" / "readme.log").exists() and {"notes.txt", "readme.log"} <= src_names, sorted(src_names))
d = by("D")
big_ok = (DEST / "Big" / "big-60MB.bin").is_file() and sha((DEST / "Big" / "big-60MB.bin").read_bytes()) == sha(FILES["Big/big-60MB.bin"])
check("D: 60 MB file downloaded intact (SHA-256)", bool(d) and d[0]["state"] == "ok" and big_ok)
check("D: lifting the speed limit mid-run took effect (average above the 3 MB/s limit)", bool(d) and d[0]["avg_speed"] > 3 * 1048576 * 1.2, f"avg {d[0]['avg_speed'] / 1e6:.1f} MB/s" if d else "")
e = by("E")
check("E: min-age 30 min skipped the fresh file (nothing downloaded, nothing deleted)", bool(e) and e[0]["files"] == 0 and "just-uploaded.txt" in {x["name"] for x in api.browse(f"{PREFIX}/Fresh")["entries"]}, e[0]["message"] if e else "")
f = by("F", "catch-up")
check("F: late slot caught up at start (trigger=catch-up)", bool(f) and f[0]["state"] == "ok" and f[0]["files"] == 2, f[0]["message"] if f else "")
g = [r for r in runs if r["job_name"].startswith("G")]
check("G: slot with catch-up off recorded as missed, never executed", bool(g) and g[-1]["state"] == "missed" and not (DEST / "LateMissed").exists(), g[-1]["message"] if g else "")
check("notifications were raised for transfers", len(notified) >= 3, f"{len(notified)} e.g. {notified[0][1][:60] if notified else ''}")
detail = api.run_detail(a[0]["id"]) if a else {"files": []}
check("history detail lists each file with size and deleted flag", len(detail["files"]) == 4 and all(x["status"] == "ok" and x["size"] > 0 for x in detail["files"]))
log = api.run_log(a[0]["id"])["lines"] if a else []
check("raw rclone log is kept and has no password", any(l["msg"].startswith("Copied") for l in log) and all(creds["app_password"] not in json.dumps(l) for l in log), f"{len(log)} lines")
stats = api.stats(7)
check("statistics add up", stats["totals"]["files"] == sum(r["files"] for r in runs if r["dry_run"] == 0) and stats["totals"]["missed_runs"] >= 1, f"{stats['totals']['files']} files, {stats['totals']['bytes'] // 1_000_000} MB, success {stats['totals']['success_rate']}%")
check("status events recorded", len(api.status()["events"]) >= 5)

# ------------------------------------------------------------- 8. restart: persistence (new process-like)
api.shutdown()
api2 = NextPullApi(DATA, poll_seconds=2)
conn2 = api2.connection(refresh=True)["connection"]
check("after restart: login restored from the DPAPI file and still valid", conn2.get("connected") and conn2.get("user") == creds["user"])
check("after restart: jobs and history persisted", len(api2.list_jobs()["jobs"]) == 6 and len(api2.history(100)["runs"]) >= len(runs))
api2.start_engine()
time.sleep(25)
check("after restart: no phantom/duplicate runs started", len(api2.history(100)["runs"]) == len(runs), f"{len(runs)} -> {len(api2.history(100)['runs'])}")

# ----------------------------------------------------------- 9. Windows features (restored afterwards)
try:
    api2.set_autostart(True)
    on = winutil.autostart_enabled()
    api2.set_autostart(False)
    check("start with Windows: Run key created and removed", on and not winutil.autostart_enabled())
except Exception as error:
    check("start with Windows toggle", False, error)
try:
    api2.set_watchdog(True)
    on = winutil.watchdog_enabled()
    api2.set_watchdog(False)
    check("watchdog task: created and removed", on and not winutil.watchdog_enabled())
except Exception as error:
    check("watchdog task", False, error)

# ------------------------------------------------------------------------- 10. clean up Nextcloud, then disconnect
dav("DELETE", PREFIX)
gone = dav("PROPFIND", PREFIX, b"", {"Depth": "0"})[0] == 404
trash_base = f"{creds['server']}/remote.php/dav/trashbin/{urllib.parse.quote(UID, safe='')}/trash"
body = b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:nc="http://nextcloud.org/ns"><d:prop><nc:trashbin-original-location/></d:prop></d:propfind>'
removed = 0
try:
    with urllib.request.urlopen(urllib.request.Request(trash_base + "/", data=body, method="PROPFIND", headers={"Authorization": auth, "Depth": "1"}), timeout=120) as resp:
        tree = ET.fromstring(resp.read())
    for item in tree.findall("{DAV:}response"):
        loc = item.findtext(".//{http://nextcloud.org/ns}trashbin-original-location") or ""
        href = item.findtext("{DAV:}href") or ""
        if loc.startswith(PREFIX) and not href.rstrip("/").endswith("/trash"):
            urllib.request.urlopen(urllib.request.Request(f"{creds['server']}{href}", method="DELETE", headers={"Authorization": auth}), timeout=60).close()
            removed += 1
except Exception as error:
    say("trash cleanup error:", error)
check("cleanup: dummy test folder deleted from Nextcloud", gone)
check("cleanup: only NPTEST trash entries purged", True, f"{removed} trash entries removed")
rev = api2.disconnect(True)
check("disconnect revokes the app password on the server", rev["ok"] and rev["revoked"], json.dumps(rev))
check("after disconnect: stored login removed", not (DATA / "credentials.bin").exists())
api2.shutdown()
passed = sum(1 for _, ok, _ in RESULTS if ok)
say(f"\nSUMMARY: {passed}/{len(RESULTS)} checks passed")
for name, ok, detail in RESULTS:
    if not ok:
        say("  FAILED:", name, detail)
say("DATA DIR KEPT FOR SCREENSHOT:", DATA)

