# Jobs reference

A job downloads **one Nextcloud folder** to **one folder on this PC** on a schedule. You can have several jobs; they run one at a time (the others wait in a queue).

| Option | Meaning |
| --- | --- |
| **Name** | Shown everywhere. |
| **Nextcloud folder** | The source. Use *Browse Nextcloud* to pick it. Sub-folders are included. |
| **Destination folder** | Where files are written on this PC. It is created if it does not exist. |
| **Copy / Move** | Copy keeps the files on Nextcloud. Move deletes each file on Nextcloud after rclone verified the download. See [Move mode and safety](Move-Mode-and-Safety). |
| **Weekdays and Start time** | When the job starts. See [Scheduling and speed](Scheduling-and-Speed). |
| **Stop by** (optional) | End the scheduled run at this time even if it is not finished. A manual *Run now* ignores it. |
| **Speed limit (MB/s)** | Maximum total download speed. `0` means unlimited. |
| **Speed timetable** | Different limits at different times of day, for example `08:00,2M 23:00,off`. Replaces the flat limit. Checked when you save. |
| **Files at once** | How many files are transferred in parallel. |
| **Streams per file** | Connections used for one big file. Both settings share the speed limit. |
| **Skip files newer than (minutes)** | Ignore files that were modified very recently, so a file that is still being uploaded is not taken half-way. |
| **Retries** | How many times rclone retries a failed pass. |
| **Exclude patterns** | One per line, for example `*.log`. An exclude always wins over an include. |
| **Only include** | One per line, for example `*.mkv`. Everything else is skipped. |
| **Compare checksums** | Compare checksums instead of only size and time. Only effective when the server provides them. |
| **Remove empty folders on Nextcloud after a move** | Tidy up folders left empty by Move. |
| **Always dry run** | Show what would happen; change nothing. |
| **Catch up if the PC was off or asleep** | If the PC was off at the start time, run late (up to the number of hours you set), instead of recording a *missed* run. |
| **Enabled** | Turn the schedule on or off without deleting the job. |

You can **duplicate** a job (the copy starts disabled), and **export / import** jobs as text from Settings (no passwords inside).
