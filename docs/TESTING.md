# Testing

```powershell
py -3 -m unittest discover -s tests -v                       # everything (about 2 min)
py -3 -W error::ResourceWarning -m unittest discover -s tests # catches leaked handles (kept clean)
py -3 -m unittest tests.test_engine                           # one module
node --check web/app.js; node --check web/mock-api.js         # UI syntax
```

No real Nextcloud, rclone or network is needed: the suites use two test doubles.

## Test doubles

| File | What it fakes |
| --- | --- |
| `tests/fake_rclone.py` | `version`, `obscure -`, `copy`/`move` from a local folder (`NC:path` -> `$FAKE_REMOTE_ROOT/path`) with `--min-age`, `--include/--exclude`, `--bwlimit`, `--dry-run`, `--delete-empty-src-dirs`, the JSON log, and the **rc API** (`core/stats`, `core/transferred`, `core/bwlimit`, `core/quit`) with basic auth. Switches (env): `FAKE_SPEED`, `FAKE_FAIL_FILES`, `FAKE_FATAL`, `FAKE_RETRY_NOISE`, `FAKE_START_DELAY`. |
| `tests/fake_nextcloud.py` | Login Flow v2, WebDAV `PROPFIND`/`GET` (with `Range`)/`HEAD`/`DELETE`, OCS user info, app-password revoke, backed by a folder. Good enough for a real rclone WebDAV remote too. |

`tests/helpers.py` sets the import path, builds valid jobs (`make_job`), writes files with a chosen age and gives each test a temp folder.

## Suites

| File | Covers |
| --- | --- |
| `test_scheduler.py` | time parsing, slot maths, next run, weekday filter, stop-by overnight, run/catch-up/missed classification, descriptions |
| `test_store.py` | job/settings validation and clamping, path cleaning, JSON persistence round trip and memory mode, export/import (new ids, no secrets), secret store (no plain password on disk, real DPAPI on Windows), history aggregates, missed/skipped rows, statistics, prune |
| `test_nc_client.py` | URL normalisation, PROPFIND parsing (spaces, unicode, `&`), login flow, listing, error mapping, user info, revoke, "never downloads or deletes" |
| `test_rclone_runner.py` | args/env (no secrets, no `--inplace`), log parsing, discovery, and **runs against the fake rclone**: copy, move, empty dirs, min-age, include/exclude, dry run, partial/failed/fatal, retry noise, bad destination, missing binary, password never in log/history, live progress, cancel (no `.partial` left), stop-by, live bandwidth |
| `test_engine.py` | on-time slot, catch-up, missed, several missed days, disabled jobs, clock going backwards, weekday filter, not connected, rclone missing, manual dry-run, queueing, skipped, cancel, stop-by vs manual, status/log reading, notifications, interrupted rows at start, log housekeeping, the background thread |
| `test_api.py` | every public attribute is a method, errors never escape the bridge, login through the bridge, **no password in any result**, browse, job CRUD and validation, end-to-end run + history + log + stats, disconnect rules, settings whitelist, destination test, export/import, persistence opt-in |

## Rules for new tests

* Put a test next to any change to code that enforces a hard rule ([AGENTS.md](../AGENTS.md)).
* Never touch the real data folder: use `NextPullApi(None)` or a temp `data_dir`.
* Tests that need time use the engine's injectable `clock`; keep waits short and bounded (`wait_idle`).

## Real rclone (`test_real_rclone.py`)

11 tests run the **real rclone** (found through `find_rclone`: `tools\rclone\rclone.exe` or PATH) over real WebDAV against
`fake_nextcloud.py`: binary + obscure, copy (timestamps kept, source untouched, no `.partial`, unicode/space names), move (deletes
after copy, removes empty folders), the Nextcloud `403`-on-delete case (file downloaded, source kept, run `partial`), dry run (no
DELETE request at all), min-age/exclude, wrong password, missing folder, password absent from the real log, live progress + live speed
change + cancel, and stop-by. They are **skipped automatically** when rclone is not installed. Verified with rclone v1.75.1.

## Diagnostics (`test_diagnostics.py`)

Redaction shapes and registered secrets, no secret on disk even in a traceback (regression: a filter on the *logger* does not cover child loggers), log reading and tail,
thread excepthook, problem grouping and hints, report content (no password/name leaks by default, names with the flag, 5-report retention), self-test results and
that one crashing check does not hide the others, UI error cap.

## The behaviour matrix (`test_rclone_matrix.py`) and the probe

`tools/rclone_probe.py` runs the real rclone through ~40 scenarios (odd names, reruns, every server failure, filters, interruptions) and
prints rclone's raw JSON log next to what NextPull concluded; use it before changing the parser. `tests/test_rclone_matrix.py` pins each
finding (44 tests, about 4 minutes; `NEXTPULL_HEAVY=1` adds a 280 MB multi-stream download). `tests/fake_nextcloud.py` supports fault
injection (`status`, `drop`, `corrupt`, `delay`), checksums, case-differing names and names Windows cannot store.

## Live test against a real Nextcloud

`tools/live_friend_test.py` (manual, about 6 minutes, dummy files): see [LIVE-TEST.md](LIVE-TEST.md).

## Not covered yet

* The pywebview window itself (verified by smoke runs and screenshots with the mock API, see [UI.md](UI.md)).
* Tray, autostart, watchdog and Windows notifications on a real desktop (thin wrappers over Windows APIs).
* A run against a real Nextcloud server (the fake implements the subset of WebDAV rclone needs).
