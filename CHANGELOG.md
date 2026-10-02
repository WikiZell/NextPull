# Changelog

## 0.1.0 - first working version (2026-10-02)

### Added
- **Jobs**: Nextcloud folder + Copy/Move + destination + weekdays/time + stop-by + speed limit (flat or timetable) + files at once,
  streams per file, minimum file age, include/exclude patterns, checksums, dry run, catch-up.
- **Engine**: scheduler with run / catch-up / missed / skipped rules, one run at a time with a queue, live progress through rclone's
  rc API, per-file results from rclone's JSON log, cancel, stop-by, live speed change, interrupted-run recovery, notifications.
- **Nextcloud**: browser sign-in (Login Flow v2) with an app password, folder browser, quota, revoke on disconnect.
- **Window**: Dashboard (live run), Jobs (+ editor and Nextcloud folder picker), Nextcloud files, History (+ files and log viewer),
  Statistics (charts, calendar, top files), Settings.
- **Windows**: tray icon, start with Windows, watchdog task, single instance, sleep prevention, DPAPI-encrypted login.
- **Packaging**: `Launch-NextPull.bat`, `build/build.ps1` (PyInstaller one-folder `NextPull.exe`), `tools/Fetch-rclone.ps1` (checksum-verified).
- **Tests**: 127 tests: test doubles for rclone and Nextcloud, plus 11 tests with the **real rclone v1.75.1** over WebDAV; ResourceWarning-clean.
- **Docs**: README, AGENTS.md, and `docs/` (architecture, scheduler, rclone, Nextcloud, security, testing, deployment, UI, operations, infrastructure).

### Branding
- Version under the logo and in the window title, "Made by René Girardi" credit in the sidebar and the About card.

### Diagnostics
- **Application log** (`app.log`, rotating 6 x 1 MB): startup, scheduler decisions (queued / missed / skipped / tick gaps), every run's start, rclone exit code and result,
  bridge errors with tracebacks, unhandled exceptions in any thread, JavaScript errors from the window. Secrets are redacted on every handler.
- **Problems page**: grouped recent failures with a "what to do" hint, warnings from the log, checkup, log viewer, sidebar badge and dashboard banner ("mark as seen").
- **Create report**: zip with summary, jobs, problems, checkup, runs, `app.log` and the logs of failed runs; passwords never, names replaced by codes unless chosen.
- Tests: `tests/test_diagnostics.py` (redaction, report content, self-test, caps).

### Fixed during development
- Leaked rclone stdout/stderr pipes after every run (closed now).
- A run with exit code 0 was marked `partial` because rclone logs retried attempts as errors.

### Hardened with a systematic real-rclone test (probe + 44-test matrix)
- **Name-clash guard**: remote names that differ only by case (or by characters Windows replaces) are left untouched in Copy *and* Move, listed
  as Skipped, and the run is flagged (a Move would otherwise delete both on Nextcloud while keeping one local file).
- **Nextcloud pre-check** before rclone: wrong login, missing folder, maintenance (5xx, retried) and an HTML page answered with 200 now fail
  fast with a clear message instead of "nothing new"; Cancel works even while the server hangs.
- **Filters**: ordered `--filter` rules so an exclude beats an include (plain flags copied excluded files).
- **History accuracy**: one row per file (retries no longer multiply rows or errors), delete failures are a note on the downloaded file, files
  downloaded earlier and removed by a later move are recorded ("Already had"), true sizes for names Windows re-encodes, `.partial` temp names
  never shown, messages say which file failed and why in plain words.
- **Bounded retries** (`--low-level-retries 6`): a persistent server error no longer stalls an attempt for minutes.
- **Big downloads were not recorded**: rclone logs multi-stream files as `Multi-thread Copied (new)`; the parser now handles it (found by a 280 MB test, the friend's real workload).
- Speed timetables are validated when a job is saved; rclone's stderr is kept in the run log when it fails before logging.

### Fixed after the real-rclone run
- `tools/Fetch-rclone.ps1` could not read `SHA256SUMS` (served as raw bytes) and refused to install; it now decodes it.

### Found by the live test against a real Nextcloud
- The dry run of a **Move** job listed nothing (rclone words it "Skipped move as --dry-run is set (size N)"); it now lists the files with sizes. Directory bookkeeping lines are no longer counted as files.
- A Move job keeps its own source folder on Nextcloud and removes only empty folders inside it (documented).

### Known gaps
- Not yet tried on the friend's PC itself (the live test simulates it on a clean data folder: `tools/live_friend_test.py`, [docs/LIVE-TEST.md](docs/LIVE-TEST.md)).
- Tray, autostart and the watchdog are thin Windows wrappers verified only by a smoke run.

