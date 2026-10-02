# Operations and troubleshooting

## First checks

1. **Dashboard banners**: *Not connected* or *rclone was not found* explain most failures.
2. **History -> the failed run -> the message and the log** (warnings/errors filter). The message is rclone's reason, not a guess.
3. **Logs folder** (Settings -> Open logs folder): one `run-<id>.jsonl` per run (JSON lines; the same content the log viewer shows).

## The Problems page and sending a report

The sidebar shows a red number when runs failed since you last pressed *Mark as seen*. The **Problems** page has:

- **Run checkup**: rclone found, Nextcloud login works, every enabled job's Nextcloud folder and destination are usable, free space, scheduler alive, data folder writable,
  start with Windows. Every problem has a "what to do" line.
- **Recent problems**: failed / partial / interrupted / missed runs of the last 7 days, grouped by job and reason.
- **Warnings from the application log** and the **Application log** viewer (filter by level, search). The file is `app.log` in the data folder
  (`%LOCALAPPDATA%\NextPull`), rotated at 1 MB (6 files); run logs stay in `logs\run-<id>.jsonl`.
- **Create report**: writes `reports\nextpull-report-<time>.zip` (the newest 5 are kept) and opens the folder. Send that file. It holds the version, Windows version,
  settings, jobs, the checkup, recent runs, `app.log` and the logs of the latest failed runs. **Passwords are never included.** File/folder names, the server and
  the account name are replaced by stable codes (`<name-3fa91c>`) unless *Include names* is ticked; the same name always gets the same code, so the report stays readable.
  Messages can still contain a name that was not recognised: open the zip and look if in doubt.

What gets logged and where: see `applog.py` (loggers `nextpull.app|engine|run|win|ui`). For a developer: set nothing, the log is always on at INFO.

## Common problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| Run `failed`: "Not connected to Nextcloud" | No login stored or it was disconnected | Settings -> connect |
| Run `failed`: "rclone was not found" | No `tools\rclone\rclone.exe` and not on PATH | `tools\Fetch-rclone.ps1`, or set the path in Settings |
| "Nextcloud rejected the saved login" | The app password was revoked/expired | Settings -> Disconnect, connect again |
| "The Nextcloud folder was not found" | Source path wrong or renamed | Edit the job; use *Browse Nextcloud...* |
| Run `ok` but "Nothing new to download" | No files older than *minimum age*, or filters exclude everything | Check minimum age and exclude/include patterns; try *Dry run* |
| Run `missed` | App not running (PC off) and catch-up off/too late | Turn on catch-up, tick *Start with Windows* and *Restart automatically* |
| Run `interrupted` | App or PC stopped mid-run | Normal after a reboot/crash; the next run continues (rclone re-checks files) |
| Run `partial` | Some files failed | Open the run: the failed files and reasons are listed; fix and re-run |
| Files not deleted on Nextcloud in Move mode and `403` in the log | The Nextcloud server user cannot write that file (see below) | Fix permissions on the server |
| Slow downloads | Speed limit, Cloudflare proxy, or the line | Check the job's limit; use a non-proxied address (see [NEXTCLOUD.md](NEXTCLOUD.md)) |
| Two windows / nothing happens on double-click | Single instance: the first copy is running in the tray | Use the tray icon; double-click raises the existing window |

## Move mode: why a delete can fail with 403

Nextcloud refuses `DELETE` on a **Local external storage** file unless its own server user can *write that file*, not just the folder.
Files that something else created as `root:root` with mode `0644` therefore download fine but cannot be deleted by a WebDAV client.
rclone then reports an error for that file and, as a safety, does not remove the now-undeletable parent folders ("not deleting
directories as there were IO errors"), so empty folders can stay behind until the files are fixed. Fix it on the server side
(make the files writable by the web user, for example `chmod 666` files / `777` folders or the right owner); NextPull itself cannot work around a 403.

## What Move leaves behind

A Move job deletes each file after it was downloaded and verified, and (if *Remove empty folders* is on) the empty folders **inside** the source. The job's own source folder is kept, so the folder you picked is never removed.

## Deleted files and the Nextcloud trash

A file deleted through Nextcloud goes to that user's trash on the server (a copy, which also slows large deletes). On the author's
server a nightly job empties that user's trash. If you do not run such a job, set a short retention in Nextcloud.

## Backups

Everything that matters is in `%LOCALAPPDATA%\NextPull`: `jobs.json` (use Settings -> Export jobs for a copy without secrets),
`history.db`, `settings.json`. The login (`credentials.bin`) cannot be moved to another PC or Windows user (DPAPI): just connect again.

## Safe testing of a new job

1. Create the job in **Copy** mode with a **Dry run** first (the run lists what would happen).
2. Run it for real in Copy mode against a small folder.
3. Switch to **Move** only when the destination, filters and permissions are proven.

## Diagnosing from the command line

```powershell
tools\rclone\rclone.exe version
# a read-only manual listing with the same remote definition NextPull uses (replace values; do not paste your app password in chat or tickets):
$env:RCLONE_CONFIG_NC_TYPE="webdav"; $env:RCLONE_CONFIG_NC_URL="https://SERVER/remote.php/dav/files/USER"; $env:RCLONE_CONFIG_NC_VENDOR="nextcloud"
$env:RCLONE_CONFIG_NC_USER="USER"; $env:RCLONE_CONFIG_NC_PASS=(rclone obscure "APP-PASSWORD")
tools\rclone\rclone.exe lsd NC: --config NUL
```
