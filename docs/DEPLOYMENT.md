# Deployment and installation

Two ways to run NextPull on the target Windows PC. The **packaged folder** is the one to give your friend: it needs no Python.

## A. Packaged folder (recommended for the friend's PC)

On the developer PC:

```powershell
powershell -ExecutionPolicy Bypass -File tools\Fetch-rclone.ps1     # once: official rclone, SHA-256 verified
powershell -ExecutionPolicy Bypass -File build\build.ps1            # builds dist\NextPull\ (about 80 MB, includes rclone if fetched)
```

Copy the whole `dist\NextPull` folder to the target PC, for example `C:\NextPull`, and run `NextPull.exe`.
The folder contains `NextPull.exe`, `_internal\` (Python runtime, the `web\` UI), `tools\rclone\rclone.exe` and `README.md`.
`NextPull.exe --background` starts hidden in the tray.

First run on the target PC:

1. **Settings -> Nextcloud connection**: type the server address, *Connect with your browser*, approve in the browser.
2. **Settings -> Behaviour**: tick *Start with Windows* and, for an always-on PC, *Restart automatically if it stops*.
3. **Jobs -> New job**: pick the Nextcloud folder, the destination, mode, start time, speed. Use **Dry run** first.

## B. From source (development, or a PC with Python 3.13)

```powershell
Launch-NextPull.bat                       # installs requirements on first run, starts without a console window
Launch-NextPull.bat --background          # hidden in the tray
```

The batch file checks for Python (`py`), installs `requirements.txt` if `webview`, `pystray` or `PIL` are missing, warns if rclone is
missing, then starts `nextpull.py` with `pyw`.

## Runtime behaviour

| Feature | How it works |
| --- | --- |
| Start with Windows | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` value `NextPull` (command ends with `--background`). No admin rights. |
| Keep running | Task Scheduler task `NextPull-Watchdog` (every 5 minutes, current user). The app's single-instance mutex makes extra launches a no-op, so it only acts after a crash or an accidental quit. |
| Tray | Closing the window hides it (if *Keep running in the tray* is on). *Quit* in the tray menu stops everything. Double-clicking the exe again raises the existing window. |
| Sleep | While a run is active the app asks Windows not to sleep (`SetThreadExecutionState`); it never keeps the display on. |
| Data | `%LOCALAPPDATA%\NextPull` (see [ARCHITECTURE.md](ARCHITECTURE.md)). Override with `--data-dir`. |

## Updating

1. Quit NextPull (tray -> Quit).
2. Replace the program folder with the new build (keep `tools\rclone` if you added your own rclone). Jobs, history and the stored login
   live in `%LOCALAPPDATA%\NextPull` and are untouched.
3. Start it again. A run that was in progress when you quit is recorded as `interrupted`.

To update rclone alone, run `tools\Fetch-rclone.ps1` again (or replace `tools\rclone\rclone.exe`).

## Uninstalling

1. *Settings -> Disconnect* (leave "also remove access on the server" ticked).
2. Untick *Start with Windows* and *Restart automatically*.
3. Quit from the tray, delete the program folder and, if you want, `%LOCALAPPDATA%\NextPull`.

## Requirements on the target PC

Windows 10/11 with the **Edge WebView2 runtime** (present on current Windows; pywebview uses it) and network access to the Nextcloud
server. The packaged build includes everything else.
