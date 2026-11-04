# SFTPGo server setup (administrator guide)

NextPull can pull from an **[SFTPGo](https://github.com/drakkan/sftpgo)** server through its **WebDAV** service (any plain WebDAV server works the same way). This page is for the person who runs
the server. The user-facing side is in the wiki page *Other servers (SFTPGo)*. Everything below uses placeholders (`pull.example.com`, `192.168.1.10`); replace them.

Why WebDAV and not SFTP or SFTPGo's HTTP API: the transfers are done by rclone, which speaks WebDAV; it has no backend for SFTPGo's REST API. WebDAV is plain HTTPS, so it passes through a
normal reverse proxy on port 443 and no extra port has to be opened.

## 1. The WebDAV service

In `sftpgo.json` add a binding (the container then listens on that port; publish it **on the LAN address only**, the reverse proxy is the only thing that should reach it):

```json
"webdavd": { "bindings": [ { "port": 8092, "address": "", "enable_https": false, "prefix": "", "client_ip_proxy_header": "X-Forwarded-For", "proxy_allowed": ["192.168.1.20"] } ] }
```

```yaml
    ports:
      - "192.168.1.10:8092:8092"
```

`proxy_allowed` is the address of the reverse proxy: without it the defender sees every client as the proxy and would ban everybody after a few failed logins. Enable the defender
(`common.defender.enabled: true`) so repeated wrong passwords are blocked.

## 2. The folder and the user

Expose the folder as a **virtual folder** (for example `/Incoming`), so clients never see the real path. Give the user **only** what a pull needs:

| Setting | Value |
| --- | --- |
| permissions on `/` | `list`, `download`, `delete_files` (no upload, rename, mkdir, overwrite, delete_dirs) |
| denied protocols | `SSH`, `FTP`, `HTTP` (leave `DAV` allowed) |
| password | long and random (32+ characters) |
| max sessions | a small number (3-4) |
| download bandwidth | optional per-user cap |

If the folder is written by another program (a downloader), run SFTPGo as the **same user** as that program: then the delete after a verified copy always works (no 403 like Nextcloud's
"file not writable by the web user").

Check it with `curl -i -X PROPFIND -H "Depth: 1" -u user:password http://192.168.1.10:8092/`: 207 with the login, 401 without. `PUT`, `MKCOL` and `COPY` must answer 403.

## 3. Publishing through a reverse proxy (Nginx Proxy Manager)

Goal: strangers get **no answer at all**, only NextPull with the access key and the user's password gets through.

1. **Hostname**: a name nobody would guess (`dl-` and 12 random characters), with a **wildcard certificate** so that the certificate transparency logs do not publish the exact name.
   Use a DNS-only record if the files are big (Cloudflare's free plan limits large non-HTML transfers through its proxy).
2. **Proxy host** -> `http://192.168.1.10:8092`, Force SSL, HTTP/2. No websockets.
3. **Advanced -> custom Nginx configuration** (a template; test it on your setup):

```nginx
# Only requests that carry the access key are answered; everything else: connection closed, no page, no banner.
if ($http_x_pull_key != "REPLACE-WITH-A-LONG-RANDOM-KEY") { return 444; }
# A pull needs only these methods; uploads, renames and new folders never reach the server.
if ($request_method !~ ^(PROPFIND|GET|HEAD|DELETE|OPTIONS)$) { return 444; }
proxy_hide_header Server;
proxy_buffering off;
proxy_request_buffering off;
proxy_read_timeout 3600s;
proxy_send_timeout 3600s;
client_max_body_size 1m;
```

   Also: Force SSL and HSTS on; a **DNS-only** record if no CDN should see the traffic in clear. The hop proxy -> SFTPGo stays plain HTTP on the LAN (accepted); everything that leaves the LAN is TLS.
   NextPull's *Test* button on the connection shows the TLS version and the verified certificate.
4. Make the invite code for the person: `py -3 tools\make_invite.py pull.example.com ben X-Pull-Key <the key> "Home server"`. Send the code and, **separately**, the password.

NextPull sends the key as the header `X-Pull-Key` (any header name works if you change both sides). It refuses `http://` for any address that is not on the local network, and always verifies the TLS certificate.

## 4. Test data and the live test

`tools/live_sftpgo_test.py` runs the real app against a real server with throw-away files. Create a user `bentest` with a virtual folder `/Test` mapped to a scratch directory that holds:
`Anime/[SubsPlease] Episode 01 (1080p).mkv`, `Apostrofo's/it's.mkv`, `Ranma ½/Episodio 01 [BDRip].mkv`, `È quasi magia Johnny/film (1989).mkv`, `Film con spazi  e (parentesi).mkv`, an empty folder
and a 200 MB `big.mkv`. The Move step consumes the `Anime` file; recreate it before running again. Delete the test user when done.

```powershell
$env:NEXTPULL_TEST_PW = "<password of bentest>"
py -3 tools\live_sftpgo_test.py http://192.168.1.10:8092 bentest
```
