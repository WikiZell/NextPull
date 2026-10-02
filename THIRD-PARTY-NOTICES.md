# Third-party notices

NextPull is an independent project. It is **not affiliated with, endorsed by or sponsored by** rclone or Nextcloud GmbH.
"Nextcloud" and "rclone" are names of their respective owners.

## rclone (does all the transfers)

NextPull drives the official **[rclone](https://rclone.org)** command-line program for every listing, download, move and delete. Without rclone there is
no NextPull: all the hard work (WebDAV, retries, multi-thread downloads, verification, bandwidth limiting) is rclone's. Thank you to Nick Craig-Wood
and the rclone contributors.

- Project: https://rclone.org - source: https://github.com/rclone/rclone
- License: **MIT**, Copyright (C) 2012 by Nick Craig-Wood https://www.craig-wood.com/nick/
- NextPull does not modify rclone. `tools/Fetch-rclone.ps1` downloads the official release from `downloads.rclone.org` and verifies its SHA-256;
  the binary is not stored in this repository. A packaged build contains `rclone.exe` together with rclone's license text (`tools/rclone/`).

## Python packages (runtime)

| Package | Use | License |
| --- | --- | --- |
| [pywebview](https://github.com/r0x0r/pywebview) | window (Edge WebView2) | BSD-3-Clause |
| [pystray](https://github.com/moses-palmer/pystray) | tray icon | LGPL-3.0-or-later |
| [Pillow](https://github.com/python-pillow/Pillow) | draws the tray icon | MIT-CMU (HPND) |

## Build tool

| Package | Use | License |
| --- | --- | --- |
| [PyInstaller](https://pyinstaller.org) | builds `NextPull.exe` | GPL-2.0-or-later with a special exception that allows bundling any program |

Each package keeps its own license; follow the links above for the full texts.
