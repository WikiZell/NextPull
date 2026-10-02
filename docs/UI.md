# User interface

Static HTML/CSS/JS in `web/`, hosted by pywebview (Edge WebView2). No bundler, no npm, plain ES2020, `$()` = `getElementById`.
The UI polls `status()` once per second; there are no push events. Text is English.

## Screens

| Screen | What it shows / does |
| --- | --- |
| **Dashboard** | Connection and rclone warnings; the **live run card** (job, mode, progress bar, bytes/total, speed, ETA, per-file progress, *speed limit* box that applies immediately, *Cancel run*); 30-day tiles (downloaded, files, success rate, average speed); scheduled jobs with next/last run and *Run now*; recent activity. |
| **Jobs** | One card per job: enable switch, mode badge (Move is highlighted), Nextcloud folder -> destination, schedule text, speed, next/last run; *Run now*, *Dry run*, *Edit*, *Duplicate* (paused copy), *Delete*. |
| **Job editor** | Name; Nextcloud folder (typed or *Browse Nextcloud...* picker); destination (typed or *Choose...*, with free-space/writable check); **Copy / Move**; weekday chips; start time; stop-by; speed limit; catch-up (+ hours). *Advanced*: files at once, streams, minimum age, retries, exclude/include patterns, speed timetable, checksums, remove empty folders, permanent dry run. Saving a Move job asks for confirmation. |
| **Nextcloud files** | Read-only folder browser with breadcrumbs, sizes and dates; *Create job from this folder*. |
| **History** | Every run (including `missed`/`skipped`), filter by job. A run opens a detail window: summary, files with result/speed/"deleted on Nextcloud", and the raw log with a level filter; *Open destination folder*. |
| **Statistics** | 7/30/90/365 days: totals (volume, files, runs ok/failed/partial, missed, success rate, average speed, deleted, file errors), bar chart of GB per day, line chart of average speed, run calendar (ran/missed/failed), largest files, by job. Charts are hand-written SVG. |
| **Settings** | Connection (connect with the browser, status + quota, check, disconnect with optional server-side revoke); behaviour toggles (tray, notifications, keep awake, start with Windows, watchdog); rclone status and custom path; history/log retention, open logs folder, export/import jobs, clear history; about. |

## Behaviours worth knowing

* The live card rebuilds its skeleton only when the situation changes (idle -> running -> another run), so typing in the speed box is
  never interrupted by the 1 s refresh.
* Destructive actions (cancel run, delete job, disconnect, clear history, Move mode) go through an in-app confirmation.
* Every bridge call goes through `call()`, which shows `error` as a toast; nothing fails silently.
* The editor never trusts itself: the backend validates again (`clean_job`) and returns a readable message.

## Working on the UI without the backend

`web/mock-api.js` implements `window.pywebview.api` with the same shapes as `NextPullApi` and a simulated running download.
It loads when the URL has `?mock=1` (or on a plain `http` dev server); the real app (`file://`) never loads it.

```powershell
# open directly:   web\index.html?mock=1&page=jobs         (pages: dashboard jobs files history stats settings; add &running=1 for a live run)
# or serve:        py -3 -m http.server --directory web     then   http://localhost:8000/?mock=1
```

Screenshots with headless Edge (what was used to check every page):

```powershell
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --headless=new --disable-gpu --hide-scrollbars --window-size=1280,980 `
  --virtual-time-budget=6000 --screenshot=out.png "file:///C:/path/to/NextPull/web/index.html?mock=1&page=stats"
```

When you add a bridge method: implement it in `nextpull.py`, add a mock in `mock-api.js` with the same result shape, and a test in
`tests/test_api.py` (the "every public attribute is a method" test will catch non-methods).

## Problems page

`#page-diagnostics`: checkup card (hidden until *Run checkup*), recent problems (with *Open this run*), log warnings, *Send a report* (names off by default, optional note),
and the application log viewer. The sidebar badge and the dashboard banner come from `status().problem_count`; *Mark as seen* calls `ack_problems`. `reportUiError`
sends `window.onerror`, unhandled promise rejections and failed bridge calls to `log_ui_error` (capped at 60 per session).
