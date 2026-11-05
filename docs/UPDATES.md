# Updates (developer notes)

Two parts: **finding** a newer release (`updates.py`, inside NextPull) and **installing** it (`installer.py`, run by the companion program
`NextPullUpdater`). User-facing text is in [wiki/Updates.md](wiki/Updates.md).

## Finding a release (`updates.py`)

`updates.py` is pure apart from one HTTP call:

- `latest_release(url)` -> `summarize(json)`: tag, notes, release page and the program zip (`NextPull*.zip`) with its SHA-256 (GitHub's `digest` field or a `<zip>.sha256` asset). Drafts, pre-releases and non-version tags give `None`.
- `is_newer(latest, current)`: numeric compare, a pre-release sorts before its release, garbage is never newer.
- `trusted_url(url)`: HTTPS on `github.com` / `*.githubusercontent.com` only (also used for the release page that `open_release_page` opens and for every download).

`NextPullApi`: a daemon thread (`_update_loop`) checks `UPDATE_FIRST_DELAY` seconds after `start_engine`, then every `UPDATE_INTERVAL` while the `check_updates` setting is on.
The result is kept in memory (`_update`); `status()` carries `update: {version, installable, can_install}` for the banner when the version is newer, not skipped and the setting is on.
Bridge methods: `check_update` (always checks), `update_info`, `skip_update(version)` (state `update_skipped`), `open_release_page`. A notification is shown once per version (state `update_notified`).
Tests set `api._update_url` to a local fake server (`tests/test_updates.py`); never let a test reach the real GitHub.

## Installing a release

```
NextPull (banner / Settings)            NextPullUpdater (a copy in <data>/updater-run/<time>/)
  install_update()  ------------------>   --request <data>/update/request.json
  quit_app()                              1 wait      hold the single-instance mutex (the app, the watchdog and auto-start cannot start)
                                          2 backup    <data>/backups/<time>-v<old>.zip (manifest + SHA-256, read back and verified)
                                          3 download  GitHub only, every redirect hop checked, SHA-256 checked
                                          4 verify    safe unpack to <app>.new, run  NextPull.exe --self-check  on a COPY of the backup
                                          5 install   <app> -> <app>.previous, <app>.new -> <app>   (renames on one volume)
                                          6 restart   release the mutex, start the new version, watch it for 10 s
                                          failure at any step -> put the old version back, restore the settings, start it again
  (new) NextPull starts  <-------------   writes <data>/update-result.json, shows a notice on the Dashboard
```

### Why this design

- **A copy of the updater runs from the data folder.** A running exe locks its folder, and the folder is exactly what gets replaced. `install_update` copies `<app>\updater\` to `<data>\updater-run\<time>\` first; `_clean_update_leftovers` removes old copies at the next start.
- **The updater holds NextPull's single-instance mutex** (`winutil.SingleInstance`) from the moment the app has closed until the new version starts, so the watchdog task (every 5 minutes) and auto-start cannot launch a half-replaced program. Acquiring it is also how the updater knows the app has really exited.
- **The new version is tested before anything is replaced.** `--self-check <report> --data-dir <scratch>` loads the config stores, the history database and the secret store from a *copy* of the backup, checks the bundled `web/`, imports the window library and writes a JSON report (`ok`, `version`, `problems`). The version must equal the release's. A failure here leaves the install untouched.
- **Renames, not copies, make the swap.** `<app>.new` is unpacked next to `<app>` (same volume), so the swap is two renames (retried for 45 s because antivirus or the indexer may hold a folder briefly). The old folder stays as `<app>.previous` for one generation.
- **Failure handling is one function** (`Installer._recover`): stop a launched new version, take the mutex again, rename the folders back, restore every settings file whose hash differs from the backup, start the old version. If that itself fails the outcome says `restore_failed`, the old files stay on disk as `<app>.previous`, the backup is untouched and the window points at the backup folder.
- **Cancel** is possible while waiting and while downloading, never after the install step starts.

### Safety rules (tests in `tests/test_installer.py`, `tests/test_updater.py`)

1. Downloads: HTTPS on GitHub only; a redirect to any other host is refused (`_GuardedRedirects`); the SHA-256 must match GitHub's `digest` and/or the `.sha256` asset (both present must agree; neither present = refuse).
2. The zip is checked before it is unpacked: no absolute/drive/`..` paths, no symlinks, size cap, `testzip`, and it must contain `NextPull.exe`.
3. The backup is verified by reading it back; no backup, no update. The last 5 backups are kept (`BACKUPS_TO_KEEP`). A backup contains jobs, settings, state, the DPAPI blobs (`credentials.bin`, `connections.bin`) and `history.db`; it never contains logs.
4. Files the user added under `tools\` (for example `rclone.exe`) are carried over when the release does not ship them.
5. The updater never touches the data folder except `backups\`, `update\`, `updater-run\`, `logs\updater-*.log`, `update-result.json`, and a restore after a failed start.
6. `NEXTPULL_UPDATER_ALLOW_LOOPBACK=1` lets the updater download from `http://127.0.0.1` (used by the end-to-end test). Nothing else weakens the address rules.

### Files

| File | Role |
| --- | --- |
| `installer.py` | The engine: `Request`, `Installer` (steps, recovery), `backup_config` / `restore_config`, `download`, `unpack`, results file. No GUI. |
| `nextpull_updater.py` | The companion program: `UpdaterState` (reporter for the window), `describe()` (plain-language headlines), `UpdaterApi` (pywebview bridge), `--demo ok|fail`. |
| `updater_web/` | The updater window (plain HTML/CSS/JS, dark/light by system setting). `?mock=<scenario>` shows it without Python (`mock.js`). |
| `build/package.py` | Makes `dist\NextPull-<version>.zip` + `.sha256` and test-unpacks it with `installer.unpack`. |
| `build/build.ps1` | Builds `NextPull.exe`, the updater into `dist\NextPull\updater\`, then runs `package.py`. |
| `build/publish.ps1` | Creates the GitHub release with `gh` (asks first). |

### Releasing

1. Bump `__version__` in `nextpull.py`, update `CHANGELOG.md`, run the tests.
2. `powershell -ExecutionPolicy Bypass -File build\build.ps1` -> `dist\NextPull-X.Y.Z.zip` and `.sha256`.
3. `powershell -ExecutionPolicy Bypass -File build\publish.ps1` (tag `vX.Y.Z`, notes from the changelog section, both files attached). Release convention: tag `vX.Y.Z`, asset `NextPull-X.Y.Z.zip` holding the *contents* of `dist\NextPull`, plus `NextPull-X.Y.Z.zip.sha256` (`<hash>  <name>`).

### Looking at the window

```powershell
cd updater_web; py -3 -m http.server 18120      # then open http://127.0.0.1:18120/index.html?mock=download
```

Scenarios: `wait`, `force`, `backup`, `download`, `verify`, `install`, `restart`, `success`, `failed`, `rolledback`, `restorefailed`, `cancelled`. The window is designed for 580 x 700.
`NextPullUpdater.exe --demo ok` (or `fail`) runs the real window with a pretend update and touches nothing.

### Trying a real update safely

Use a throw-away data folder and install folder, never your real ones: build, unpack an older build to a scratch folder, serve `dist\` with `py -3 -m http.server`, set `NEXTPULL_UPDATER_ALLOW_LOOPBACK=1`, write a
`request.json` (see `installer.Request`) with `app_dir`, `data_dir`, `launch_args: ["--data-dir", "<scratch data>"]`, `asset_url`, `sha256_url`, `version`, and run `NextPullUpdater.exe --request <file>`.
