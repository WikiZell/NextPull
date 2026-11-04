# SFTPGo server setup (for the administrator)

This page is for the person who runs the server. Users: see [Other servers](Other-Servers). Placeholders: `pull.example.com`, `192.168.1.10`.

NextPull uses SFTPGo's **WebDAV** service, because the transfers are done by rclone (which speaks WebDAV, not SFTPGo's REST API) and because WebDAV is plain HTTPS that goes through a normal reverse proxy on
port 443. No extra port has to be opened.

## 1. Enable WebDAV (LAN only)

```json
"webdavd": { "bindings": [ { "port": 8092, "address": "", "enable_https": false, "prefix": "", "client_ip_proxy_header": "X-Forwarded-For", "proxy_allowed": ["192.168.1.20"] } ] }
```

Publish the port **on the LAN address only** (`192.168.1.10:8092:8092`) so only the reverse proxy reaches it. `proxy_allowed` is the proxy's address, so the defender sees real client addresses.
Turn the defender on (`common.defender.enabled`) to block repeated wrong passwords.

## 2. The user

Expose the folder as a **virtual folder** (for example `/Incoming`) and give the user only:

| Setting | Value |
| --- | --- |
| permissions on `/` | `list`, `download`, `delete_files` |
| denied protocols | `SSH`, `FTP`, `HTTP` (keep `DAV`) |
| password | 32+ random characters |
| max sessions | 3 or 4; optionally a download bandwidth cap |

If another program writes the folder, run SFTPGo as the **same user** as that program, so that deleting after a verified copy always works.
Test: `curl -i -X PROPFIND -H "Depth: 1" -u user:password http://192.168.1.10:8092/` answers 207; without the login 401; `PUT`, `MKCOL`, `COPY` answer 403.

## 3. Hide it behind a reverse proxy (Nginx Proxy Manager)

Goal: strangers get **no answer at all**.

1. A hostname nobody would guess (`dl-` + 12 random characters) and a **wildcard certificate**, so certificate transparency logs do not publish the exact name. DNS-only if the files are big.
2. Proxy host to `http://192.168.1.10:8092`, Force SSL.
3. Advanced / custom Nginx configuration (a template; test it on your setup):

```nginx
if ($http_x_pull_key != "REPLACE-WITH-A-LONG-RANDOM-KEY") { return 444; }
if ($request_method !~ ^(PROPFIND|GET|HEAD|DELETE|OPTIONS)$) { return 444; }
proxy_hide_header Server;
proxy_buffering off;
proxy_request_buffering off;
proxy_read_timeout 3600s;
proxy_send_timeout 3600s;
client_max_body_size 1m;
```

4. Make an invite code: `py -3 tools\make_invite.py pull.example.com ben X-Pull-Key <the key> "Home server"`. Send the code and, **separately**, the password.

The full version of this page, with the test data and the live test, is `docs/SFTPGO.md` in the repository.
