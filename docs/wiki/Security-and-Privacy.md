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

## What is encrypted

| Part of the path | Encrypted? |
| --- | --- |
| This PC <-> your server over the **internet** (Nextcloud or another server) | **Yes: HTTPS (TLS)**. NextPull refuses `http://` for any address that is not on your own network, always verifies the certificate and the host name, and never follows a redirect that would send your login to another host or down to plain http |
| Between the reverse proxy and the server **inside your own network** (for example proxy -> SFTPGo) | No (plain HTTP on the LAN). That is a deliberate, common choice; it is only visible to someone already on your network |
| A server on your local network used directly (`http://192.168.x.x`) | No, and NextPull shows it as "not encrypted (local network)" |
| Files stored on the server or on this PC | Not encrypted by NextPull (use disk encryption if you need it) |

**See it for yourself:** *Settings -> Check connection* (Nextcloud) and *Test* (other servers) show the real handshake: protocol (for example TLS 1.3), who issued the certificate, and that it was verified.
If you put a CDN proxy such as Cloudflare in front of the server, the traffic is still encrypted on the wire, but that provider decrypts it. A DNS-only record avoids that.

## Network

NextPull talks only to **your Nextcloud server and the other servers you added** (see [Other servers](Other-Servers); `http://` is accepted only on your own network and certificates are always verified) (WebDAV / OCS over HTTPS if your server uses it) and, for the one-time setup, to `downloads.rclone.org` if you use `tools\Fetch-rclone.ps1`. There is no telemetry and no account. The only other request is the optional **update check**: about every 6 hours NextPull asks `api.github.com` for the latest NextPull release (a public request that sends nothing but a User-Agent). You can switch it off in *Settings -> Updates*. See [Updates](Updates).

## Safe by design

Downloads are written as `.partial` and renamed; nothing is overwritten; a Move deletes only after rclone verified the file; Dry run changes nothing. See [Move mode and safety](Move-Mode-and-Safety).

## Reporting a vulnerability

Please open a private security advisory on the GitHub repository (Security tab) instead of a public issue.
