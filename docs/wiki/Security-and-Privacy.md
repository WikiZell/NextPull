# Security and privacy

## Your login

- You sign in **in your browser** (Nextcloud *Login Flow v2*). NextPull receives an **app password**, never your real password, and never your two-factor codes.
- The app password is stored **encrypted with Windows DPAPI**, which only your Windows user on this PC can decrypt (`credentials.bin` in the data folder). It cannot be copied to another PC or user.
- It reaches rclone only through the process **environment** of the run, never on a command line, and the rclone control port uses a random password per run.
- You can revoke it any time: in NextPull (*Disconnect*) or in Nextcloud under *Settings -> Security -> Devices & sessions*.

## What is logged

`app.log` and the run logs describe what the app did. **Passwords and tokens are redacted before anything is written** (registered secrets and common shapes such as `password=`, `Authorization: Basic ...`, credentials in URLs). File names do appear in run logs, because that is the point of a download log.

## What a report contains

See [Problems and reports](Problems-and-Reports). No passwords or tokens, ever; names, server and account name are replaced by codes unless you tick *Include names*.

## Network

NextPull talks only to **your Nextcloud server** (WebDAV / OCS over HTTPS if your server uses it) and, for the one-time setup, to `downloads.rclone.org` if you use `tools\Fetch-rclone.ps1`. There is no telemetry, no update check and no account.

## Safe by design

Downloads are written as `.partial` and renamed; nothing is overwritten; a Move deletes only after rclone verified the file; Dry run changes nothing. See [Move mode and safety](Move-Mode-and-Safety).

## Reporting a vulnerability

Please open a private security advisory on the GitHub repository (Security tab) instead of a public issue.
