# Updates

## Update notification

NextPull can tell you when a newer version is published on the [GitHub Releases page](https://github.com/WikiZell/NextPull/releases).

- A few seconds after start, and then about every **6 hours**, it asks GitHub for the latest release (`api.github.com/repos/WikiZell/NextPull/releases/latest`). The request is public and sends nothing but a User-Agent.
- If the release is newer than the version you run, the **Dashboard** shows a banner: "NextPull 0.2.0 is available". *What is new* opens the release notes in your browser; *Skip this version* hides the banner until a still newer version appears. If notifications are on, you also get one Windows notification per new version.
- **Settings -> Updates** shows your version, when it was last checked and the result. *Check now* checks immediately, even if the automatic check is off.
- Turn **Check for updates automatically** off to stop all update requests.
- Only published releases count: drafts and pre-releases are ignored, and links are accepted only if they are HTTPS on GitHub.

NextPull never installs anything by itself.

## Installing a new version today

1. Download the zip from the release page and unpack it.
2. Quit NextPull from the tray menu.
3. Replace the program folder with the new one. Your jobs, history and login stay: they live in `%LOCALAPPDATA%\NextPull` ([Data and backup](Data-and-Backup)).
4. Start `NextPull.exe`. If you use *Start with Windows* or the watchdog and the folder is in a new place, switch them off and on again in Settings.

A one-click installer (a small helper that closes NextPull, swaps the files, restarts it and rolls back on failure) is planned.
