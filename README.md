# NextPull

Scheduled downloads from **Nextcloud** for Windows, built on **rclone**. Pick a Nextcloud folder, a destination on this PC, *copy* or
*move*, a start time and a speed limit; NextPull runs it every day (or on chosen weekdays), catches up when the PC was off, and keeps a
history with logs and statistics. It runs for months in the tray without attention.

It replaces a hand-written nightly `rclone move` script with something you can see and change from a window.

![NextPull dashboard](docs/wiki/images/dashboard.png)

**Documentation: the [project wiki](https://github.com/WikiZell/NextPull/wiki)** (same pages in [docs/wiki](docs/wiki/Home.md)): installation, getting started, every option, troubleshooting, security.

## Features

- **Jobs**: Nextcloud folder, Copy or Move (Move deletes on Nextcloud only after rclone verified the download), destination folder,
  weekdays + start time + optional stop-by, speed limit (flat MB/s or a time-of-day timetable), files at once, streams per file,
  minimum file age, include/exclude patterns, checksums, dry run, catch-up for missed runs.
- **Live view**: progress, speed, ETA per file, change the speed limit or cancel while it runs.
- **History and logs**: every run including *missed* and *skipped* ones, per-file results, the raw rclone log.
- **Problems page**: plain-language list of what failed and what to do, a one-click checkup (rclone, login, folders, disk space, scheduler),
  a searchable application log and **Create report**: a redacted zip (no passwords; names hidden by default) to send when you need help.
- **Statistics**: GB per day, average speed, success rate, run calendar, largest files, per job.
- **Always on**: tray icon, start with Windows, optional watchdog that restarts it, one copy at a time, sleep prevention during a run.
- **Safe by design**: the Nextcloud *app password* (browser sign-in, your real password is never seen) is stored encrypted with
  Windows DPAPI and only reaches rclone through its environment; downloads are written as `.partial` and renamed; Move is rclone's
  own copy-verify-delete; Dry run changes nothing. See [docs/SECURITY.md](docs/SECURITY.md).

## Quick start

**On the PC that downloads (no Python needed)** - use the packaged folder built by `build\build.ps1`:

1. Copy `dist\NextPull` to e.g. `C:\NextPull` and run `NextPull.exe` (rclone is included if it was fetched before building).
2. **Settings** -> type the Nextcloud address -> *Connect with your browser* -> approve.
3. **Settings** -> tick *Start with Windows* (and *Restart automatically if it stops* for an always-on PC).
4. **Jobs** -> *New job* -> choose the folder (*Browse Nextcloud...*), the destination, mode and time. Try **Dry run** first.

**From source** (Python 3.13): double-click `Launch-NextPull.bat` (installs the packages on first run, starts without a console).
`Launch-NextPull.bat --background` starts hidden in the tray.

**rclone**: `powershell -ExecutionPolicy Bypass -File tools\Fetch-rclone.ps1` downloads the official build from
`downloads.rclone.org` and verifies its SHA-256 before installing it to `tools\rclone\`.

## Build

```powershell
powershell -ExecutionPolicy Bypass -File build\build.ps1      # -> dist\NextPull\NextPull.exe (about 80 MB)
```

## Develop

```powershell
py -3 -m pip install -r requirements.txt
py -3 -m unittest discover -s tests -v          # 127 tests, about 2 min; the real-rclone tests skip themselves if rclone is not installed
node --check web/app.js
py -3 nextpull.py --data-dir .\devdata          # run the real app with a throw-away data folder
```

UI work without the backend: open `web\index.html?mock=1&page=jobs` ([docs/UI.md](docs/UI.md)).

## Documentation

| Doc | Topic |
| --- | --- |
| [AGENTS.md](AGENTS.md) | Rules and map for AI agents and contributors (**read first**) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Modules, threads, one run step by step, run states, data on disk |
| [docs/SCHEDULER.md](docs/SCHEDULER.md) | Slots, catch-up, missed/skipped, stop-by, clock edge cases |
| [docs/RCLONE.md](docs/RCLONE.md) | How rclone is driven: environment remote, flags, rc API, log events, exit codes |
| [docs/NEXTCLOUD.md](docs/NEXTCLOUD.md) | Login Flow v2, WebDAV/OCS calls, errors, Cloudflare note |
| [docs/SECURITY.md](docs/SECURITY.md) | Where the secret lives and what is guaranteed |
| [docs/TESTING.md](docs/TESTING.md) | Suites, test doubles, how to add tests |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Install, build, autostart, watchdog, update, uninstall |
| [docs/UI.md](docs/UI.md) | Screens, behaviours, mock mode and screenshots |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Troubleshooting, Move-mode 403, backups, safe testing |
| [docs/LIVE-TEST.md](docs/LIVE-TEST.md) | Manual end-to-end test against a real Nextcloud with dummy files |
| [CHANGELOG.md](CHANGELOG.md) | What changed |

## Status

Version 0.1.0: all features above work. They are covered by tests against a fake rclone and a fake Nextcloud **and by 11 tests with the real rclone (v1.75.1) over WebDAV**, and the packaged `NextPull.exe` starts correctly. A live test against a real Nextcloud (dummy files, clean data folder, 37/39 first run, both misses fixed in the test) is in [docs/LIVE-TEST.md](docs/LIVE-TEST.md).

## Credits

Made by **René Girardi**.

NextPull is built on **[rclone](https://rclone.org)** (MIT, Copyright (C) 2012 Nick Craig-Wood and contributors): every transfer, retry, verification and bandwidth limit is done by rclone;
NextPull schedules it, supervises it and shows the results. Thank you to the rclone project. See [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) for the other open source components.
NextPull is an independent project and is not affiliated with rclone or Nextcloud GmbH.

## License

[MIT](LICENSE) (c) René Girardi.
