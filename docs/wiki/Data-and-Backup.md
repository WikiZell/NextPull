# Where NextPull keeps your data (and how to back it up)

Everything NextPull saves is **outside the program folder**, in:

```
%LOCALAPPDATA%\NextPull
```

For a normal Windows user that is `C:\Users\<name>\AppData\Local\NextPull`. Paste `%LOCALAPPDATA%\NextPull` into the Explorer address bar to open it.

| File or folder | Content |
| --- | --- |
| `jobs.json` | Your jobs |
| `settings.json` | Settings (tray, notifications, rclone path, how long to keep history and logs) |
| `state.json` | Scheduler state, such as when it last checked the clock |
| `credentials.bin` | The Nextcloud login, **encrypted** for this Windows user (DPAPI) |
| `history.db` | Run history and statistics (SQLite) |
| `app.log` (+ `app.log.1` ...) | The application log, rotated at 1 MB |
| `logs\run-<id>.jsonl` | The raw rclone log of each run |
| `reports\` | The reports you created on the [Problems page](Problems-and-Reports) (the newest 5) |
| `rclone.empty.conf` | An empty rclone config, so rclone never reads your own `rclone.conf` |

`Settings -> Data` shows the exact folder and has *Open logs folder*, *Export jobs*, *Import jobs* and *Clear history*.

## What this means in practice

- **Updating is safe.** Replace the program folder (for example `C:\NextPull`) with the new build; your jobs, history and login stay because they are not inside it.
- **It is per Windows user.** Another Windows account on the same PC starts empty.
- **Uninstalling** is deleting the program folder. Delete `%LOCALAPPDATA%\NextPull` as well if you want to remove everything. Also turn off *Start with Windows* and the watchdog in Settings first (see below).
- **Another location:** start the app with `--data-dir "D:\somewhere"` to keep the data elsewhere (the folder is created if needed).

## Backup and moving to another PC

- **Back up** `%LOCALAPPDATA%\NextPull`. The program folder holds nothing worth saving.
- **Moving the jobs:** use *Settings -> Export jobs* on the old PC and *Import jobs* on the new one (the text contains no passwords), or copy `jobs.json` and `settings.json` while NextPull is closed.
- **The login does not move.** `credentials.bin` can only be decrypted by the same Windows user on the same PC. On the new PC press *Connect with your browser* again.
- History (`history.db`) can be copied too if you want to keep it.

## Start with Windows and the watchdog are not files

They live in Windows itself, not in the data folder:

- *Start with Windows* is a per-user entry in the registry (`HKCU\...\Run`).
- *Restart automatically if it stops* is a Windows scheduled task.

Both store the **path of `NextPull.exe`**. If you move or rename the program folder, switch the two options off and on again in Settings so they point to the new place.

See also [Settings and tray](Settings-and-Tray) and [Security and privacy](Security-and-Privacy).
