# AGENTS.md

Guidance for AI coding agents working in this repository. Humans start with [README.md](README.md).

## What this project is

**NextPull** is a single-user **Windows** desktop app that runs *scheduled downloads from a Nextcloud server* with **rclone** under the
hood (WebDAV). It replaces a hand-written nightly `rclone move` script on a friend's PC. A *job* = Nextcloud folder + copy/move +
destination folder + start time + speed limit (+ filters). The app keeps history, per-file results, raw rclone logs and statistics.

- UI: static HTML/CSS/JS in `web/`, hosted by **pywebview** (Edge WebView2). No bundler, no npm.
- Backend: Python 3.13; one bridge class `NextPullApi` (`nextpull.py`), a background `Engine`, flat modules.
- External tool (subprocess): **rclone** (not vendored in git; `tools/rclone/rclone.exe` is fetched by `tools/Fetch-rclone.ps1`).
- Runs as one process with a tray icon; start with Windows + an optional watchdog task keep it alive.

## Status

| Area | State |
| --- | --- |
| Engine, scheduler, store, Nextcloud client, rclone driver, bridge, tray, UI, launcher, PyInstaller build | **Done**, 127 tests pass (11 use the real rclone) (`-W error::ResourceWarning` clean). |
| Packaged `dist/NextPull/NextPull.exe` | Built and smoke-tested (window, bundled `web/`, data folder, single instance). |
| Docs (`README.md`, `docs/*.md`, `CHANGELOG.md`) | Done. |
| Real rclone | Verified: v1.75.1 fetched (SHA-256 checked) into `tools/rclone/`; `tests/test_real_rclone.py` runs copy/move/dry-run/403/cancel/speed/stop-by over real WebDAV against `fake_nextcloud.py`. |
| Live test vs a real Nextcloud | Done (`tools/live_friend_test.py`, docs/LIVE-TEST.md): sign-in, scheduler, catch-up/missed, queue, move+delete, filters, speed, persistence, disconnect/revoke. |
| Real-desktop checks of tray, autostart, watchdog, Windows notifications | Only a smoke run; thin wrappers over Windows APIs. |

## Repository map

| Path | Role |
| --- | --- |
| `nextpull.py` | `NextPullApi` (JS bridge, every method returns `{ok, error, ...}`), `main()`, `--background`, `--data-dir`. |
| `engine.py` | Scheduler tick, queue (one run at a time), run lifecycle, notifications, log reading, housekeeping. |
| `scheduler.py` | Pure schedule maths: slots, next run, stop-by, run/catch-up/missed classification. |
| `rclone_runner.py` | Find rclone, build args/env, drive one run through rclone's rc API + JSON log (`RcloneRun`). |
| `applog.py` | The application log (`app.log`, rotating, in-memory ring when `data_dir=None`), **redaction** (`redact`, `register_secret`), excepthooks. |
| `diagnostics.py` | Problem grouping + plain-language hints, the self-test, `Scrubber` and `build_report` (the zip users send). |
| `nc_client.py` | Login Flow v2, WebDAV PROPFIND listing, OCS user info, revoke. stdlib only. |
| `store.py` | `clean_job`/`clean_settings`, `ConfigStore` (JSON), `SecretStore` (DPAPI), `HistoryDb` (SQLite runs/files/stats). |
| `winutil.py` | DPAPI, autostart (HKCU Run), watchdog (schtasks), single instance + wake event, sleep prevention, free space. |
| `tray.py` | Optional pystray icon (drawn in code) and notifications. |
| `web/` | `index.html`, `style.css`, `app.js`, `mock-api.js` (development only, `?mock=1`). |
| `tests/` | `unittest`; `helpers.py`, `fake_rclone.py`, `fake_nextcloud.py` and the `test_*.py` suites. |
| `build/build.ps1`, `tools/Fetch-rclone.ps1`, `Launch-NextPull.bat` | Packaging, rclone fetch (SHA-256 verified), launcher. |
| `docs/` | Architecture, scheduler, rclone, Nextcloud, security, testing, deployment, UI, operations, infrastructure. |

## Commands

```powershell
py -3 -m pip install -r requirements.txt                       # pywebview, pystray, Pillow
py -3 -m unittest discover -s tests -v                          # about 75 s; also: py -3 -m unittest tests.test_engine
py -3 -W error::ResourceWarning -m unittest discover -s tests   # must stay clean (leaked handles)
node --check web/app.js; node --check web/mock-api.js           # UI syntax
py -3 nextpull.py --data-dir .\devdata                          # run the real app with a throw-away data folder
powershell -ExecutionPolicy Bypass -File build\build.ps1        # package -> dist\NextPull\
```

There is no linter/formatter config; match the surrounding style.

## Hard rules (safety invariants)

Keep these; add a test when you touch the code that enforces them.

1. **Secrets**: the Nextcloud app password lives only in `SecretStore` (DPAPI blob `credentials.bin`). It is never written to
   `jobs.json`, `settings.json`, the history DB, any log, or an rclone config file, and never passed on a command line. rclone gets the
   remote through `RCLONE_CONFIG_NC_*` environment variables, and the password is obscured through **stdin** (`rclone obscure -`).
   No bridge method may return a password (`test_api` scans every result).
2. **Move only after verification**: deletion on Nextcloud is rclone's own `move` (copy, verify, delete). Never delete remote files any
   other way. `mode: move` is explicit per job (the UI confirms); `dry_run` must stay a real `--dry-run`.
3. **Never lose or half-write local files**: no `--inplace`, no `sync`, nothing that deletes local data. rclone's `.partial` + rename stays.
4. **Persistence is opt-in per instance**: every store takes `data_dir=None` (memory only). Tests and mock runs never touch
   `%LOCALAPPDATA%\NextPull`.
5. **One run at a time**; the same job is never queued twice (recorded as `skipped`); a slot that cannot run is recorded as `missed`
   (the history never has silent holes); a run still `running` at startup becomes `interrupted`.
6. **UTF-8 subprocess I/O**: `encoding="utf-8", errors="replace"` with `text=True` for every subprocess.
7. **Cancellation stays responsive**: runs end through rclone `core/quit` (killed after 10 s); never block the UI thread.
8. **rc API is localhost-only** with a random port and random password per run.
9. **No `..`** in Nextcloud paths (`clean_remote_path`, `NextcloudSession.list_dir`).
10. **Bridge contract**: every public attribute of `NextPullApi` is a method (pywebview exposes all of them); methods return
    `{"ok": bool, "error": str, ...}` and never raise (the `@safe` decorator); expected errors (`NextcloudError`, `RcloneError`,
    `ValueError`, `OSError`) become messages fit for the UI.
11. **Exit code 0 is `ok`**: rclone logs retried attempts as errors; never turn a successful run into `partial` because of them.
12. **Name-clash guard**: before any run, names that collide on a case-insensitive disk are excluded (all members) and recorded as `skipped`;
    in Move mode a failed guard aborts the run. Never remove this: it prevents silent data loss.
13. **Filters are ordered `--filter` rules** (exclude before include, `- **` last), never plain `--include/--exclude`.
14. **Pre-check** the server (`check_folder`) before rclone; failures are plain-language messages. Keep `RcloneRun.PRECHECK_*` and the cancel-responsive helper thread.
15. **History stays honest**: one row per file, delete failures are a note on a downloaded file, `present` for already-downloaded files, never show `.partial` temp names.
16. **Release rclone's pipes** after each run (the ResourceWarning-clean test run guards this).
17. **Logs and reports never contain secrets**: log through `applog.get_logger(...)` only (the redacting filter sits on the handlers; a filter on the
    logger would not cover child loggers), call `applog.register_secret` for every new secret (app password, rc password), never `print` request data,
    and keep `tests/test_diagnostics.py` passing. A report hides file/folder/account names unless the user ticks the box. Bridge `@safe` logs every failure;
    new background threads must not swallow exceptions silently (log them).

## Conventions

- Python 3.13, `from __future__ import annotations`, type hints on public functions, dataclasses for value types, flat modules.
- UI text in English; plain ES2020, `$()` = `getElementById`; the UI polls `status()` about once per second (no push events).
- Adding a bridge method: implement in `nextpull.py`, mock it in `web/mock-api.js` (same shape), test it in `tests/test_api.py`.
- No new runtime dependency without updating `requirements.txt` and the README.
- `docs/` is part of the product: update the matching doc and `CHANGELOG.md` for user-visible changes.

## Definition of done

- `py -3 -m unittest discover -s tests` passes (and the `-W error::ResourceWarning` variant); new behaviour has a test.
- `node --check web/app.js` passes when JS changed; check visual changes with headless Edge screenshots ([docs/UI.md](docs/UI.md)).
- README / docs updated when behaviour, commands or invariants change; add a line to `CHANGELOG.md`.
- Do not commit `.env`, `__pycache__/`, `dist/`, `build/work/`, `tools/rclone/`, or any `credentials.bin`.

## Suggested next work

1. Try the packaged build on the friend's PC with a **Dry run** job first; check tray, start with Windows and the watchdog there.
2. Try the packaged build on the friend's PC with a **Dry run** job first; check tray, start with Windows and the watchdog there.
3. Optional features: a pause/resume that survives restarts, per-job notifications, a job "run history" sparkline on the Jobs page,
   per-day speed timetable editor, update check for rclone.

## Context: where the files come from

NextPull pulls a folder from a Nextcloud account. A permission rule makes `move` work on the server side: the files must be writable by
Nextcloud's web user, else the `DELETE` fails with `403` (see [docs/OPERATIONS.md](docs/OPERATIONS.md) and [docs/NEXTCLOUD.md](docs/NEXTCLOUD.md)).
Keep real hostnames, user names, IPs and paths out of the repository: use `cloud.example.com`, `alice`, `192.0.2.x` in docs, tests and mocks.

