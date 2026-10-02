# Credits and licenses

## Made by

**René Girardi**. NextPull is released under the **MIT License** (see `LICENSE` in the repository).

## Built on rclone

NextPull is a front end for **[rclone](https://rclone.org)**, "rsync for cloud storage". Every listing, download, move, delete, retry, checksum comparison and bandwidth limit is done by rclone; NextPull schedules it, supervises it through its remote-control API, parses its log and presents the results.

- rclone: https://rclone.org, source https://github.com/rclone/rclone
- License: MIT, Copyright (C) 2012 by Nick Craig-Wood
- Thank you to Nick Craig-Wood and everyone who contributes to rclone.

NextPull does not modify rclone; it runs the official release (fetched with a verified SHA-256).

## Other open source pieces

| Piece | Use | License |
| --- | --- | --- |
| [pywebview](https://github.com/r0x0r/pywebview) | The window | BSD-3-Clause |
| [pystray](https://github.com/moses-palmer/pystray) | Tray icon | LGPL-3.0-or-later |
| [Pillow](https://github.com/python-pillow/Pillow) | Tray icon drawing | MIT-CMU (HPND) |
| [PyInstaller](https://pyinstaller.org) | Builds the exe | GPL-2.0-or-later with the bundling exception |

## Disclaimer

NextPull is an independent project. It is not affiliated with, endorsed by or sponsored by rclone or Nextcloud GmbH; "rclone" and "Nextcloud" are the names of their respective owners.
