# Architecture

NextPull is one Windows process: a **pywebview** window (view only), a **bridge** class, and a **background engine** that runs
jobs by driving **rclone**. Closing the window can hide it to the tray; the engine keeps working.

```
 +--------------------------- NextPull.exe (one process) ---------------------------+
 |                                                                                  |
 |  web/ (HTML/CSS/JS)  <--pywebview js_api-->  NextPullApi (nextpull.py)           |
 |     polls status() ~1/s                          | every method -> {ok, error,...}|
 |                                                  v                                |
 |     tray.py (pystray)  <-- notify / quit --  Engine (engine.py, 2 threads)        |
 |                                                  | scheduler tick (10 s)          |
 |                                                  | worker thread: one run at once |
 |                                                  v                                |
 |   store.py: ConfigStore | SecretStore | HistoryDb      RcloneRun (rclone_runner)  |
 |   scheduler.py (pure)    nc_client.py (Nextcloud)            |                    |
 |   winutil.py (DPAPI, autostart, single instance)             | subprocess         |
 +--------------------------------------------------------------|--------------------+
                                                                 v
                                   rclone copy|move NC:<folder> <destination>  --rc  --log-file
                                                                 |
                                                                 v   WebDAV
                                                           Nextcloud server
```

## Modules

| Module | Responsibility | Talks to |
| --- | --- | --- |
| `nextpull.py` | `NextPullApi` (the JS bridge), `main()`, `--background`, single-instance + wake, window/tray wiring | everything below |
| `engine.py` | Scheduler tick, queue, run lifecycle, notifications, run log reading, daily housekeeping | `scheduler`, `store`, `rclone_runner` |
| `scheduler.py` | Pure maths: slots, next run, stop-by window, run / catch-up / missed classification | nothing |
| `rclone_runner.py` | Find rclone, build args/env, run one `rclone` process, poll the rc API, parse the JSON log | `store.HistoryDb`, `nc_client.dav_files_url`, `winutil` |
| `applog.py` | Rotating `app.log` + in-memory ring, redacting filter on every handler, `register_secret`, excepthooks, `read_log` | stdlib `logging` |
| `diagnostics.py` | Problem grouping and hints, self-test, `Scrubber`, `build_report` zip | `applog` |
| `nc_client.py` | Login Flow v2, WebDAV PROPFIND listing, OCS user info, revoke app password (stdlib only) | the Nextcloud server |
| `store.py` | Job/settings validation, JSON config, DPAPI secret blob, SQLite history and statistics | files in the data folder |
| `winutil.py` | DPAPI, Run-key autostart, Task Scheduler watchdog, mutex + wake event, sleep prevention, free space | Windows APIs |
| `tray.py` | Optional tray icon and balloon notifications (pystray + Pillow) | `NextPullApi` |
| `web/` | The UI: `index.html`, `style.css`, `app.js`, `mock-api.js` (development only) | `window.pywebview.api` |

## Threads

* **UI thread** (pywebview): calls bridge methods; they are short and never block on a download.
* **`nextpull-engine`**: ticks every 10 s (`Engine._loop`): decides what is due, queues it, housekeeping.
* **`nextpull-run`**: one worker per run (`Engine._execute`); only one exists at a time. Inside, `RcloneRun.run()` blocks while it
  supervises the rclone process (poll every 0.5 s) and two small drain threads read rclone's stdout/stderr.
* **`nextpull-login`**: polls Nextcloud while the user approves the browser sign-in (max 20 minutes).
* **`nextpull-show-event`**: waits for the "show window" event from a second launch.

State shared between threads is protected by locks (`Engine._lock`, `HistoryDb._lock`, `ConfigStore._lock`).

## One run, step by step

1. **Trigger**: the scheduler tick (`schedule` / `catch-up`) or the *Run now* button (`manual`).
2. **Queue**: `Engine._enqueue` refuses a job that is already running or queued. A single worker takes the next item.
3. **Checks**: connected? rclone found? Otherwise the run is recorded as `failed` with a clear message (never silent).
4. **Start**: `HistoryDb.start_run` (state `running`), log path `logs/run-<id>.jsonl`, rclone started (see [RCLONE.md](RCLONE.md)).
5. **Supervise**: every 0.5 s poll `core/stats` (live progress), read new JSON log lines (what happened to each file), honour
   *cancel* and the *stop-by* deadline (`core/quit`, killed after 10 s).
6. **Conclude**: map the exit code and per-file results to a state (`ok`, `partial`, `failed`, `cancelled`, `stopped`), write the
   aggregates, optionally notify, start the next queued run.

## Run states

| State | Meaning |
| --- | --- |
| `running` | In progress. Becomes `interrupted` if the app dies (found at the next start). |
| `ok` | rclone exited 0 (also when there was nothing new to download). |
| `partial` | Some files transferred, rclone exited with an error (some files failed). |
| `failed` | Nothing transferred and rclone failed, or the run could not start (not connected, no rclone, bad destination). |
| `cancelled` | You pressed Cancel. |
| `stopped` | The *stop-by* time was reached. |
| `missed` | A scheduled slot passed while the app was not running and could not be caught up. Never executed. |
| `skipped` | A slot came due while the previous run of the same job was still going. Never executed. |
| `interrupted` | The app (or the PC) stopped while the run was in progress. |

## Data on disk (`%LOCALAPPDATA%\NextPull`, override with `--data-dir`)

| File | Content |
| --- | --- |
| `jobs.json` | Jobs (validated by `clean_job`). No secrets. |
| `settings.json` | Settings (whitelisted by `clean_settings`). No secrets. |
| `state.json` | `last_checked`: the scheduler's marker. |
| `credentials.bin` | Server, user and app password, encrypted with Windows DPAPI (this user, this PC only). |
| `history.db` | SQLite: `runs` and `files` tables. |
| `logs/run-<id>.jsonl` | Raw rclone JSON log of each run (pruned after `log_days`). |
| `rclone.empty.conf` | An empty rclone config file (keeps rclone away from the user's own `rclone.conf`). |

Persistence is **opt-in per instance**: with `data_dir=None` every store is in memory, which is what the tests use.

## Why these choices

* **rclone as the engine**: resumable multi-stream WebDAV downloads, retries, `move` semantics and a mature log are its job; the
  app adds scheduling, history and a GUI.
* **rc API + JSON log instead of parsing console output**: structured, stable and testable with a fake.
* **Environment-defined remote**: no config file, no password on a command line (see [SECURITY.md](SECURITY.md)).
* **One process with a tray**: simplest thing that "runs forever" for a single user; the watchdog task covers crashes.
* **No dependencies beyond pywebview, pystray and Pillow**; Nextcloud and rclone are talked to with the standard library.
