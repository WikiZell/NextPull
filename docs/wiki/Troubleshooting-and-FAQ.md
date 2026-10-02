# Troubleshooting and FAQ

Start with the **Problems** page and press **Run checkup**. If that does not solve it, [create a report](Problems-and-Reports).

## Errors and what they mean

| Message / symptom | Cause | Fix |
| --- | --- | --- |
| "Not connected to Nextcloud" | No login saved, or it was removed | Settings -> Connect with your browser |
| "Nextcloud rejected the saved login" | The app password was revoked or the account changed | Settings -> Disconnect, then Connect again |
| "rclone was not found" | `tools\rclone\rclone.exe` is missing | Run `tools\Fetch-rclone.ps1`, or set a custom path in Settings |
| "The Nextcloud folder was not found" | The folder was renamed or deleted | Edit the job and pick the folder again |
| "Nextcloud could not be reached" / timeouts | No internet, server down, or a proxy blocks it | Check the connection; NextPull retries on the next schedule |
| Run is **Needs attention**, "not deleted on Nextcloud" (403) | The server will not let Nextcloud delete that file | See [Move mode and safety](Move-Mode-and-Safety): fix permissions on the server |
| Files **Skipped**: "name clashes" | Names that cannot coexist on Windows | Rename them on Nextcloud; nothing was overwritten |
| **Missed** runs | PC off or NextPull closed at the start time | Turn on *catch-up* in the job and *Start with Windows* in Settings |
| "Cannot use the destination folder" | The drive is not connected or the folder is not writable | Check the path and permissions |
| Disk full | The destination has no free space | Free space or change the destination |

## Signing in with an e-mail address

You can sign in to Nextcloud with a user name or with your e-mail address; NextPull handles both. (Version 0.1.0 reported "folder not found" for e-mail logins: update to 0.1.1 or later and, if needed, disconnect and connect again.)

## FAQ

**Does it need anything on the Nextcloud server?** No. It uses the normal WebDAV access that every Nextcloud has.

**Does it ever upload?** No.

**Can I use a Nextcloud behind Cloudflare?** Yes, but Cloudflare's free-plan terms restrict serving large non-HTML files through its proxy. For big downloads use an address that bypasses the proxy (a DNS-only record or a LAN/VPN address) in the connection settings.

**My real password?** NextPull never sees it. See [Security and privacy](Security-and-Privacy).

**Why do I see black windows flashing?** You should not: every helper process runs without a console. If you do, please send a report.

**Where are my files while downloading?** In the destination folder as `.partial` files until complete.

**Can two jobs run at the same time?** No, one at a time; the others wait in a queue.

**How do I move NextPull to another PC?** Install it there, connect to Nextcloud again (the login is bound to the Windows user and cannot be copied), then use *Export jobs* / *Import jobs*.

**How do I update?** Replace the program folder with the new one. Your data in `%LOCALAPPDATA%\NextPull` is kept.
