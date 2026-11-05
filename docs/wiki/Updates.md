# Updates

NextPull tells you when a newer version is published on the [GitHub Releases page](https://github.com/WikiZell/NextPull/releases), and can install it for you with one click. Your jobs, history and sign-ins are backed up first and restored automatically if anything goes wrong.

## Update notification

- A few seconds after start, and then about every **6 hours**, NextPull asks GitHub for the latest release (`api.github.com/repos/WikiZell/NextPull/releases/latest`). The request is public and sends nothing but a User-Agent.
- If the release is newer than the version you run, the **Dashboard** shows a banner: "NextPull 0.3.0 is available". *Install now* starts the update, *What is new* opens the release notes in your browser, *Skip this version* hides the banner until a still newer version appears. If notifications are on, you also get one Windows notification per new version.
- **Settings -> Updates** shows your version, when it was last checked and the result. *Check now* checks immediately, even if the automatic check is off. *Install now* appears there too.
- Turn **Check for updates automatically** off to stop all update requests.
- Only published releases count: drafts and pre-releases are ignored, and links are accepted only if they are HTTPS on GitHub.

NextPull never installs anything without you clicking *Install and restart*. (Versions before 0.3.0 have no updater: install 0.3.0 by hand once, see *Installing by hand* below; every later version can update itself.)

## Installing with one click

1. Click **Install now**, read what will happen, click **Install and restart**. If a download is running you are asked first; it is stopped cleanly and starts again at its next scheduled time.
2. NextPull closes and the **NextPull Update** window opens. It shows six steps:

   | Step | What happens |
   | --- | --- |
   | Close NextPull | The updater waits until NextPull has really exited. If it takes long, a *Close NextPull now* button appears. |
   | Back up your settings | Jobs, settings, history and saved sign-ins are zipped to `%LOCALAPPDATA%\NextPull\backups\` and the zip is read back to prove it is good. The last 5 backups are kept. |
   | Download the update | From GitHub only. You can *Cancel* here; nothing has changed yet. |
   | Check the new version | The checksum is verified, the zip is unpacked next to your install, and the new program is **test-started on a copy of your settings**. |
   | Install | The old program folder is renamed to `NextPull.previous` and the new one takes its place. |
   | Start NextPull | The new version is started and watched for a few seconds. |

3. When it is done the window says *NextPull 0.3.0 is installed* and closes by itself. NextPull is open again, and the Dashboard shows a short notice that the update went through.

### If something goes wrong

- **Before the install step** (no internet, wrong checksum, the new version fails its test): nothing was changed. The old version is started again and the window says why.
- **After the install step** (the new version crashes when it starts): the updater puts the old program folder back, restores your settings from the backup, and starts the old version. The window says *The update didn't work, so it was undone*, and the Dashboard shows the reason.
- **If even that fails** (very rare, for example a file that Windows will not release): the window says so and offers *Open backup folder*. Your settings are in the newest zip there, and the old program is still on disk as `NextPull.previous` next to your install.
- *Details* in the window shows the log (also saved as `%LOCALAPPDATA%\NextPull\logs\updater-<time>.log`); *Copy details* is handy for a bug report.
- *Download manually* opens the release page.

### Good to know

- NextPull must be installed in a folder you can write to (for example `C:\NextPull`), not in `C:\Program Files`.
- *Start with Windows* and the watchdog keep working: the program path does not change.
- Files you added under `tools\` (for example your own `rclone.exe`) are kept.
- The previous version stays as `NextPull.previous` until the next update replaces it, so you can also roll back by hand: quit NextPull, rename the two folders, start it.
- Running from source (git clone)? The button is not shown. Update with `git pull`.

## Installing by hand

1. Download `NextPull-X.Y.Z.zip` from the release page and unpack it.
2. Quit NextPull from the tray menu.
3. Replace the program folder with the new one. Your jobs, history and login stay: they live in `%LOCALAPPDATA%\NextPull` ([Data and backup](Data-and-Backup)).
4. Start `NextPull.exe`.
