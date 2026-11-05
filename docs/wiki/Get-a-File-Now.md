# Get a file now

Sometimes you do not want to wait for the night job: a file is already on the server and you want it now. The **Files** page does exactly that, for any server (Nextcloud, NextPush, SFTPGo or another WebDAV server).

## How

1. Open **Files** and browse to the folder. Use the filter box (press `/`) and the sort menu to find it.
2. Tick one or several files or folders, then press **Get now** in the bar at the bottom. For a single item you can also press **Get** at the end of its row.
3. In the dialog choose:
   - **Download**: the original stays on the server.
   - **Move**: NextPull downloads, checks that the copy is complete and only then removes it from the server. The button turns red so you cannot choose it by accident.
   - **Save to**: the default folder, or any other folder (*Choose...*). The dialog shows whether the folder exists, whether you can write to it and how much space is free, and warns if the files will not fit.
4. Press the button. The download appears on the Dashboard right away; a download that is already running finishes first and yours waits behind it (one at a time keeps the line fast).

When it is done it is listed under **Recently downloaded** on the Dashboard and in **History** as *Manual download*.

## What if the file already exists?

If a file with the same name but another size is already in the folder, the dialog asks:

- **Keep mine** (default): your file is not touched. In Move mode the server copy is not removed either.
- **Replace it**: the new file is written, and the old one is first moved to `%LOCALAPPDATA%\NextPull\replaced\<date and time>`, so nothing is ever lost.

## Good to know

- A file you pick is fetched even if it was uploaded a second ago. (Scheduled jobs wait *minutes* so they never take a half-uploaded file.)
- A picked folder is saved as a folder of the same name inside the destination. It is never removed from the server, even when a Move empties it.
- With a NextPush server, folders shared as *download only* do not offer Move: the dialog says so before you start.
- **Pause schedules** (Dashboard) stops the scheduled jobs for a holiday. *Get now* and *Run now* keep working.
- Set the default folder for quick downloads in **Settings -> Downloads**.
