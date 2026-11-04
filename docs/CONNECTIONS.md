# Connections (developer notes)

A job pulls from a **connection**. There are two types, both transferred by rclone's `webdav` backend:

| Type | Login | WebDAV URL | rclone vendor | Session class |
| --- | --- | --- | --- | --- |
| `nextcloud` (id `nextcloud`) | browser sign-in, app password (`LoginFlow`), user id looked up | `<server>/remote.php/dav/files/<uid>` | `nextcloud` | `NextcloudSession` |
| `sftpgo` (id `sg-xxxxxx`) | user name + password, optional access-key header | the address itself (root of the URL, optional path prefix) | `other` | `WebdavSession` (subclass) |

## Where things live

* `connections.py`: pure helpers. `normalize_webdav_url` (https by default, `http://` only for local-network hosts), `clean_connection`, `public_view` (no secrets), `make_session`,
  `product_name` ("Nextcloud" / "the server" for messages), `discover`, `make_invite` / `parse_invite`.
* `nc_client.py`: `NextcloudSession` takes its wording from `label` / `noun` / `fix`; `WebdavSession` overrides them, `files_url` (the server itself), `_auth` (adds the access-key header),
  `user_info` (a root listing proves the login) and `revoke` (nothing).
* `store.py`: `SecretStore` keeps the original login in `credentials.bin` (unchanged, so nothing is migrated) and the other connections in `connections.bin` (one DPAPI blob, `{id: record}`).
  `creds_for(id)` returns what a run or a browse needs, `type` included. `clean_job` gives every job a `connection` (default `nextcloud`, so older jobs keep working).
* `rclone_runner.py`: `build_env` builds the remote from the connection type (`RCLONE_CONFIG_NC_HEADERS="Name,value"` carries the access key); `RcloneRun` uses `connections.make_session` for the
  pre-check, words every message with `self._product`, registers every secret of the connection for log redaction, and passes `remove_empty_dirs=False` for `sftpgo` (such a login has no
  folder-delete right; the job's own folder is never removed anyway, hard rule 18).
* `engine.py`: `_execute` resolves `creds_for(job["connection"])`; a missing connection fails with a clear message instead of using another server.
* `nextpull.py` bridge: `list_connections`, `discover_server`, `read_invite`, `add_connection` (tests the login, saves only on success), `test_connection`, `remove_connection` (refused while a job
  uses it), `browse(path, connection)`. The self-test checks every connection and each job's own source. The report scrubs every connection's server, user and label.

## Discovery

`discover(address)`: `GET /status.php` (Nextcloud JSON) -> `nextcloud`; else an unauthenticated `PROPFIND /`: a 401 with a Basic challenge (SFTPGo also names itself in the `Server` header) -> `sftpgo`.
No answer (a server hidden behind an access-key gate) -> `unknown` with a hint to use the invite code.

## Invite code

`nextpull-invite:` + base64url(JSON `{v:1, t:"sftpgo", u:<url>, user?, l?, k?:[name,value]}`). Made by `tools/make_invite.py`. It never contains the password.

## Tests

`tests/fake_nextcloud.py` has `dav_root=True` (serves the folder at the root of its URL, with `base_path`, a `Server: SFTPGo/2.7.6` header and a Basic challenge) and `require_header=(name, value)`
(a request without it is dropped without any answer, like a gating proxy). `tests/test_connections.py` covers addresses, records, invite codes, the store (encrypted, corrupt file, memory-only),
sessions and the gate, discovery, the runner pieces, **real rclone** copy/move/failed-delete/secret-redaction against the fake server, the bridge, and a restart. `tools/live_sftpgo_test.py`
is the manual test against a real SFTPGo (see SFTPGO.md).

## Encryption rules (what the code enforces)

* `normalize_server_url` / `normalize_webdav_url`: `http://` only for local-network hosts (`nc_client.is_private_host`), so a password or key never leaves the LAN unencrypted by typing the wrong scheme.
* `nc_client._SafeRedirects`: urllib forwards every header on a redirect, `Authorization` included. A request with a login or an `X-` access key follows a redirect only to the same host, and no redirect
  goes to `http://` outside the local network (tests: `Redirects` in `tests/test_connections.py`). rclone has its own redirect handling (Go's client drops the login on a host change).
* TLS is verified everywhere (`ssl.create_default_context`, urllib's default, rclone's default). No code path disables it.
* `connections.tls_info(server)` performs a real verified handshake and reports protocol / cipher / issuer / expiry for the *Test* and *Check connection* buttons, or "not encrypted (local network)".

## Limits to remember

* rclone does not resume a download that was interrupted across runs (it starts that file again); within a run it resumes dropped connections with Range requests.
* WebDAV `vendor = other` has no checksums, so *Compare checksums* falls back to size and time.
