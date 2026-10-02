# Building and development

## Build the program folder

```powershell
powershell -ExecutionPolicy Bypass -File tools\Fetch-rclone.ps1     # once: official rclone, checksum verified
powershell -ExecutionPolicy Bypass -File build\build.ps1            # -> dist\NextPull\NextPull.exe (about 80 MB)
```

The result is a PyInstaller *one-folder* build: copy `dist\NextPull` to the target PC. rclone and its license are included when it was fetched before building.

## Run from source

```powershell
py -3 -m pip install -r requirements.txt     # pywebview, pystray, Pillow
py -3 nextpull.py                            # or Launch-NextPull.bat
```

## Tests

```powershell
py -3 -W error::ResourceWarning -m unittest discover -s tests
node --check web/app.js
```

About 190 tests. They use a **fake rclone** and a **fake Nextcloud** (with fault injection: HTTP errors, dropped connections, corrupt data, slow responses), and many tests run the **real rclone** against the fake Nextcloud over WebDAV (they skip themselves if rclone is not installed). The run is ResourceWarning-clean, which guards against leaked pipes. A manual end-to-end test against a real Nextcloud with dummy files is `tools/live_friend_test.py`.

## Working on the UI without Windows pieces

The UI is plain HTML/CSS/JS in `web/` (no bundler). Serve it and open it with the mock backend:

```powershell
cd web; py -3 -m http.server 8000
# open http://localhost:8000/index.html?mock=1&page=dashboard&running=1
```

`page` is one of `dashboard`, `jobs`, `files`, `history`, `stats`, `diagnostics`, `settings`.

## Layout

| File | Role |
| --- | --- |
| `nextpull.py` | Entry point and the JS bridge (`NextPullApi`) |
| `engine.py`, `scheduler.py` | The tick loop, the queue, the run lifecycle; the pure schedule maths |
| `rclone_runner.py`, `rclone_logic.py` | Drives one rclone process through its remote-control API and JSON log; pure parsing helpers |
| `nc_client.py` | Login Flow v2, WebDAV listing, OCS user info |
| `store.py` | Job and settings validation, JSON config, DPAPI secret, SQLite history |
| `applog.py`, `diagnostics.py` | Application log with redaction; problems, checkup, report |
| `winutil.py`, `tray.py` | Windows features and the tray icon |

`AGENTS.md` in the repository lists the hard rules (never overwrite, honest history, logs without secrets, ...). Please keep real hostnames, user names and addresses out of commits: use `cloud.example.com` and `alice`.
