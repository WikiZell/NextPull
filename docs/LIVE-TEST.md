# Live test: NextPull as a friend's PC, against a real Nextcloud

`tools/live_friend_test.py` is a **manual** end-to-end test (not part of `unittest discover`). It uses the real app code with a fresh
data folder (real DPAPI storage, real rclone, the real scheduler thread) against a real Nextcloud, with **dummy files** it creates in a
throw-away folder `NPTEST-<id>` in the signed-in account and deletes at the end.

```powershell
py -3 tools\live_friend_test.py cloud.example.com     # a browser page opens once: log in and click "Grant access"
```

Needs `tools\rclone\rclone.exe` (see `tools\Fetch-rclone.ps1`). Takes about 6 minutes (it waits for a real scheduled run).
Uses the signed-in account only; use a test account if you prefer. Nothing outside the `NPTEST-*` folder is touched. It changes
Windows settings only briefly: it creates and removes the *start with Windows* value and the watchdog task to prove they work.

## What it does

| Step | Checks |
| --- | --- |
| Connect | Bootstrap, browser sign-in (Login Flow v2), login stored encrypted (no plain password in the file) |
| Dummy data | Creates 10 files (75 MB) in nested folders; the app's folder browser sees them |
| Jobs | Saves 6 jobs (move, copy with `*.log` excluded, 60 MB at 3 MB/s, min-age 30 min, catch-up, missed); an invalid job is rejected with a readable message |
| Dry run | A Move job's dry run changes nothing and files stay on Nextcloud |
| Engine | Start: the late slot is **caught up**, the slot with catch-up off is **missed**; manual runs **queue** behind the running one |
| Big file | Live progress; the 3 MB/s limit is respected; lifting it mid-run takes effect; SHA-256 matches |
| Scheduler | A job set 2-3 minutes ahead starts by itself at its time and **moves** 4 files: SHA-256 intact, subfolders kept, every file and empty sub-folder deleted on Nextcloud |
| Filters | Copy leaves the source intact and honours the exclude; min-age skips fresh files and deletes nothing |
| History | Per-file results, deleted counts, raw log without the password, statistics, events, notifications |
| Restart | New app instance on the same folder: login restored (DPAPI), jobs and history persisted, no phantom run |
| Windows | Start with Windows and watchdog task created and removed |
| Cleanup | Deletes the test folder, purges only the `NPTEST-*` entries from the Nextcloud trash, disconnects and revokes the app password |

## Result of the first run (2026-10-02, Nextcloud 34 behind Cloudflare, rclone v1.75.1)

37 of 39 checks passed on the first run; both failures were wrong expectations in the test, corrected in the script (the speed change is
proved by the 6.9 MB/s average of a file limited to 3 MB/s; rclone deletes empty folders *inside* the source but keeps the source folder
itself). The run also found a **real bug**: the dry run of a *Move* job reported "Nothing new" because rclone words it
`Skipped move as --dry-run is set (size N)`; the parser now handles it and the history lists the files with their sizes.
