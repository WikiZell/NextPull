# NextPull

**Scheduled downloads from Nextcloud for Windows, powered by [rclone](https://rclone.org).**

Pick a Nextcloud folder, a destination on your PC, *Copy* or *Move*, a start time and a speed limit. NextPull runs it every day (or on the weekdays you choose),
catches up when the PC was off, and keeps a history with logs and statistics. It sits in the tray and runs for months without attention.

![Dashboard](images/dashboard.png)

## Start here

1. [Installation](Installation): get the app running (no Python needed for the packaged build).
2. [Getting started](Getting-Started): connect to Nextcloud and create your first job in five minutes.
3. [Jobs reference](Jobs-Reference): every option of a job explained.

## Learn more

| Page | What it covers |
| --- | --- |
| [Scheduling and speed](Scheduling-and-Speed) | Start time, weekdays, stop-by, catch-up, missed runs, speed limit and timetable |
| [Move mode and safety](Move-Mode-and-Safety) | How Move deletes on Nextcloud, Dry run, name clashes, what is never overwritten |
| [History and statistics](History-and-Statistics) | Reading runs, per-file results, logs and charts |
| [Problems and reports](Problems-and-Reports) | The Problems page, the checkup, and how to send a report when something is wrong |
| [Settings and tray](Settings-and-Tray) | Connection, tray, start with Windows, watchdog |
| [Data and backup](Data-and-Backup) | Where jobs, history and the login are stored, backing up, moving to another PC |
| [Troubleshooting and FAQ](Troubleshooting-and-FAQ) | Common errors and their fixes |
| [Security and privacy](Security-and-Privacy) | Where your login is stored, what is logged, what a report contains |
| [Building and development](Building-and-Development) | Build the exe, run the tests, project layout |
| [Credits and licenses](Credits-and-Licenses) | rclone and the other open source pieces |

## What NextPull is (and is not)

- It **is** a friendly scheduler and viewer around rclone: you never type rclone commands.
- It runs **on the PC that downloads**. It does not need anything installed on the Nextcloud server.
- It signs in the safe way: you approve it in your browser and NextPull only receives an *app password* (your real password is never seen) that you can revoke.
- It is **not** a sync tool: it pulls files from Nextcloud to this PC (and optionally deletes them on Nextcloud after a verified download). It never uploads.

*Made by René Girardi. NextPull is an independent project and is not affiliated with rclone or Nextcloud.*
