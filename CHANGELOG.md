# Changelog

## 0.4.0 - 2026-11-05

### Added
- **Get a file now.** On the **Files** page tick one or several files or folders (or press *Get* on a row) and NextPull fetches them right away, outside any schedule. A small dialog asks the two things that matter:
  **Download** (the original stays on the server) or **Move** (download, verify, then remove it from the server), and **where to save** (the default folder, or any other one, with a free-space check). It runs through the
  same queue and engine as scheduled jobs, so every safety rule applies, and shows up in History as *Manual download*.
- **Nothing at the destination is lost.** If a picked file already exists in the folder with another size you choose *Keep mine* (the server copy is not taken, and not removed in Move mode) or *Replace it* (the old file is first
  moved into `%LOCALAPPDATA%\NextPull\replaced\<date>`).
- **NextPush tells NextPull what the account may do**: when the server is NextPush, *Move* is greyed out with a plain explanation for folders shared as *download only*, instead of failing after the download.
- **New Dashboard**: a live status card (what is downloading, what is next, *Get a file now*, *Pause schedules*), today / 30-day / reliability / free-disk tiles (with a warning when the disk is getting full), a 14-day chart,
  server health with response time, recently downloaded files with *Open folder*, scheduled jobs with *Run now*, and a welcome card with the first steps when nothing is set up yet.
- **Pause all schedules** (Dashboard and Settings): for holidays or maintenance. Skipped slots are written to History and nothing runs late afterwards; *Get now* and *Run now* still work.
- **Files page**: filter box (press `/`), sort (name, newest, largest), select all, a selection bar, breadcrumbs.
- **Settings**: default folder for quick downloads, appearance (follow Windows, light, dark).
- New bridge methods `dashboard`, `server_health`, `set_paused`, `quick_info`, `fetch_now`; tests in `tests/test_quick.py` (real rclone: single file, groups with glob characters, folders, keep/replace, move, root files).

### Changed
- **Redesign**: new colour system with a proper dark theme, vector icons instead of emoji, cards with depth, readable tables, clearer focus rings, animated dialogs (`Esc` closes, dialogs are announced to screen readers),
  a layout that works down to a narrow window, reduced-motion and high-contrast support.
- A quick download ignores the *skip files newer than* setting: you asked for that file, so you get it.

## 0.3.0 - 2026-11-05

### Added
- **One-click updates with automatic rollback.** *Install now* (Dashboard banner or Settings -> Updates) hands over to a new companion program, **NextPullUpdater**, which closes NextPull and then: backs up your settings
  (jobs, settings, history, saved sign-ins -> `%LOCALAPPDATA%\NextPull\backups\`, verified by reading it back, last 5 kept), downloads the release from GitHub only (redirects checked, SHA-256 verified), unpacks it
  next to the install, **test-starts the new version on a copy of your settings**, swaps the program folder (the old one stays as `NextPull.previous`), starts the new version and watches it.
  If anything fails the old version and your settings are put back and the old version is started again; a failure before the swap changes nothing.
- **Updater window**: six clear steps, live download progress with speed and time left, release notes, a details log with *Copy details*, plain-language results (installed / undone / not installed / cancelled),
  *Cancel* while downloading, *Close NextPull now* if it will not close, dark and light themes, keyboard and screen-reader friendly.
- After an update the Dashboard shows what happened (updated, or why it was undone) until dismissed; *Open backups* opens the folder.
- `NextPull.exe --self-check <report> --data-dir <folder>`: loads everything against a settings folder without a window and writes a JSON report (used by the updater).
- `build\package.py` makes `dist\NextPull-<version>.zip` + `.sha256` (test-unpacked with the updater's own code); `build\build.ps1` builds the updater into `dist\NextPull\updater\`; `build\publish.ps1` publishes a release with `gh` after asking.
- Tests: `tests/test_installer.py` (backup, restore, download guards, unpack safety, swap, every rollback path), `tests/test_updater.py` (window state, hand-over, self-check).

### Changed
- The updater holds NextPull's single-instance lock while files move, so the watchdog and *Start with Windows* cannot start a half-replaced program.
- `winutil.SingleInstance` closes its handle when another instance exists (so the mutex disappears when that instance exits) and on `release()`.
- Running from source: the *Install now* button is not shown (use `git pull`); the banner and *Check now* work as before.

## 0.2.0 - 2026-11-04

### Added
- **Connections**: jobs can pull from **SFTPGo / any plain WebDAV server** as well as Nextcloud, with the same engine, scheduler, filters, history and safety rules. A server is added with address, user
  name and password (and an optional **access key** header that a reverse proxy can require so that strangers get no answer). The login is **tested before it is saved**; secrets are stored
  encrypted (DPAPI) in `connections.bin`, never in settings, logs, reports or the UI.
- **Discovery** ("Check" in the add-server form: Nextcloud / SFTPGo / unknown) and **invite codes** (`tools/make_invite.py`): address, user and access key in one string, never the password.
- Connection selector in the job editor and on the Files page; the folder picker works for every connection; messages say "the server" instead of "Nextcloud" where it applies.
- `http://` only for local-network addresses (Nextcloud connections too: they used to accept `http://` for any host); the TLS certificate is always verified.
- Existing jobs and the saved Nextcloud login keep working unchanged (jobs without a connection are Nextcloud jobs).
- Self-test and reports cover every connection. `tests/test_connections.py` (real rclone against a fake WebDAV server with the access-key gate), `tools/live_sftpgo_test.py` (manual, real SFTPGo).
- Docs: `docs/CONNECTIONS.md`, `docs/SFTPGO.md`, wiki pages *Other servers* and *SFTPGo server setup*.

### Safety
- A `429` from a server that locks out an address after wrong passwords (NextPush) is now explained ("too many wrong passwords from this address, try again in a few minutes") instead of looking like maintenance, and is not retried.
- **NextPush discovery**: *Check* in the add-server form recognises our own server, [NextPush](https://github.com/WikiZell/NextPush), from `/.well-known/nextpush` and fills in its WebDAV address. A hostile
  `dav` path in that document is never followed. Invite codes made by NextPush are the same format as `tools/make_invite.py`.
- **Encryption evidence and redirect guard**: *Test* / *Check connection* show the real TLS handshake (protocol, issuer, verified certificate) or "not encrypted (local network)"; requests that carry a login or access key
  never follow a redirect to another host or from https to plain http; Nextcloud addresses must use `https://` unless they are on the local network.
- Pinned with real-rclone tests: a Move never removes the job's own Nextcloud folder (only emptied sub-folders). Hard rule 18 in AGENTS.md.

### Also in this version
- **Update check**: asks GitHub Releases for a newer version (at start and every 6 hours, switch in Settings -> Updates), shows a banner and a notification (once per version),
  *Check now*, *Skip this version*. Only offers published, non-pre-release versions; links must be HTTPS on GitHub. `updates.py`, `tests/test_updates.py`.
- `save_settings` now merges with the current settings, so a partial save cannot reset other options.

## 0.1.1 - 2026-10-02

### Fixed
- **Signing in with an e-mail address** made every folder "not found": the e-mail login name was used in the WebDAV path, which needs Nextcloud's user id. The id is now looked up
  (OCS `cloud/user`), saved with the login, and used for the path; the login name is still used for authentication. Older saved logins are upgraded on first use.
- Placeholders in the job editor are generic (`Documents/Photos`).

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

