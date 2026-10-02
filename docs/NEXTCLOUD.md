# Nextcloud integration

Code: `nc_client.py`. Test double: `tests/fake_nextcloud.py`. The client **never downloads or deletes user files**; that is
rclone's job. It signs in, lists folders for the picker, reads the account info and revokes the app password.

## Sign-in: Login Flow v2 (app password)

1. The user types the server address (`normalize_server_url` accepts `cloud.example.com`, `https://host/index.php/login`,
   a sub-folder install `https://host/nextcloud`, ...).
2. `POST <server>/index.php/login/v2` -> `{poll: {token, endpoint}, login: <url>}`. The app opens `login` in the browser.
3. The user signs in (2FA works, because it is Nextcloud's own page) and clicks *Grant access*.
4. The app polls `endpoint` with the token every 2 s (up to 20 minutes). `404` = still waiting; `200` gives
   `{server, loginName, appPassword}`.
5. The **app password** is stored (DPAPI-encrypted) and used for WebDAV. The user's real password is never seen.

**Disconnect** deletes the stored credentials and, if ticked, calls `DELETE /ocs/v2.php/core/apppassword` so the app password is
also revoked on the server (Settings -> Security in Nextcloud shows it as "NextPull" until then).

## Endpoints used

| Purpose | Request |
| --- | --- |
| Folder listing | `PROPFIND /remote.php/dav/files/<user>/<path>/` with `Depth: 1` (props: resourcetype, getcontentlength, getlastmodified, `oc:size`) |
| Account info | `GET /ocs/v2.php/cloud/user?format=json` (header `OCS-APIRequest: true`) -> display name, e-mail, quota |
| Revoke | `DELETE /ocs/v2.php/core/apppassword` |
| Transfers | done by rclone over the same WebDAV root (`GET` with ranges, `DELETE` for move) |

Folder `size` for directories is Nextcloud's recursive `oc:size`. Paths are relative to the user's *Files* root and never contain `..`.

## Errors shown to the user

| Situation | Message |
| --- | --- |
| Server unreachable | "Cannot reach the Nextcloud server (...)" |
| Not Nextcloud / too old | "This address is not a Nextcloud server (or it is too old for browser sign-in)" |
| Stored login rejected (401/403) | "Nextcloud rejected the saved login. Disconnect and connect again." |
| Folder missing | "Folder not found: /path" |

## Server facts that matter

* The folder is usually a **Local external storage** of one user, readable and writable; with "check for changes" = once per direct access a WebDAV listing sees new files.
* **Deleting through Nextcloud needs the file itself to be writable by the web user** (`www-data`), not only the folder. Files created as
  `root:root 0644` fail with `403` on `DELETE`. See OPERATIONS.md.
* Deleted files go to the user's **Nextcloud trash** (a copy on the server's system disk); empty it regularly (`occ trashbin:cleanup <user>`) if the volume is big.

## Login name vs user id

The sign-in returns the **login name** (what was typed: a user name or an e-mail address). WebDAV paths use the **user id** (`/remote.php/dav/files/<id>/`), which is different
when the login was an e-mail address. NextPull authenticates with the login name, asks `ocs/v2.php/cloud/user` for the `id`, stores it with the login (`uid`) and uses it for the
WebDAV URL (browsing, pre-check and rclone). `NextcloudSession.files_user()` does the lookup lazily, so logins saved by an earlier version work too.

## Cloudflare note

If the Nextcloud hostname is proxied by Cloudflare (orange cloud), big transfers go through Cloudflare. The free-plan terms restrict
serving large non-HTML content through the proxy. For bulk downloads connect NextPull through a hostname that bypasses it
(a DNS-only record to the same server, or a VPN/LAN address) and use that address in the connection settings.
