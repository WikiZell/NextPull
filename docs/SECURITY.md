# Security

NextPull handles one secret (the Nextcloud app password) and has one dangerous capability (deleting files on Nextcloud in *move*
mode). This page lists what protects them. The rules are enforced in code and tests; see the hard rules in [AGENTS.md](../AGENTS.md).

## The Nextcloud app password

| Where | Protection |
| --- | --- |
| At rest | One file, `credentials.bin`, encrypted with **Windows DPAPI** (`CryptProtectData`): only the same Windows user on the same PC can decrypt it. |
| In memory | `SecretStore` keeps it for the session. |
| To rclone | **Environment variables of the rclone process only** (`RCLONE_CONFIG_NC_PASS`, obscured). Never a command-line argument, never an rclone config file (the config file is empty). |
| To the UI | Never. No bridge method returns it (`connection()` returns server, user, display name, quota). A test scans every bridge result for the password. |
| In logs / history | Never. rclone's log does not contain it; a test checks the log and the history rows. |
| On the server | It is an *app password*: revocable on its own in Nextcloud, and revoked by *Disconnect* (optional checkbox). Your real password is never seen. |

## rclone's control channel

`--rc` listens on `127.0.0.1` on a random free port with a random user/password generated per run; it exists only while that run does.

## Destructive actions

* **Copy** never touches Nextcloud. **Move** only uses rclone's own copy-verify-delete; the UI asks for confirmation when you save a
  job in Move mode (unless it is a permanent dry-run).
* *Dry run* is rclone's `--dry-run` (nothing transferred, nothing deleted).
* *Delete job* never deletes files; history is kept.
* Local files are written to `name.partial` and renamed when complete; NextPull never uses `--inplace` or `sync`.

## Paths

Nextcloud paths are normalised and `..` is rejected (`clean_remote_path`, `NextcloudSession.list_dir`). The destination folder is a
plain Windows path chosen by the user.

## Process and OS

* Single instance (named mutex) so two copies never run jobs at once.
* The watchdog is a normal per-user scheduled task; start with Windows is an `HKCU\...\Run` value. No admin rights, no service.
* All subprocess I/O is UTF-8 (`errors="replace"`).

## What it does not do

* It does not verify the server's identity beyond normal HTTPS certificate validation (no certificate pinning, no "ignore TLS errors").
* The history database and logs are not encrypted (they contain file names and sizes, not secrets).
* Anyone who can run programs as the same Windows user can use the stored login (DPAPI protects against other users and offline theft).
