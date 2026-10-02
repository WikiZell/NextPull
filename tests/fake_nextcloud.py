"""A fake Nextcloud for tests: Login Flow v2, WebDAV (PROPFIND, GET with Range, HEAD, DELETE), OCS user info and app password
revoke, backed by a local directory. Good enough for NextPull's client *and* for a real rclone WebDAV remote.

Fault injection (``server.faults``, a list of rule dicts, first matching rule wins, ``times`` counts down, ``None`` = always):

    {"method": "GET", "match": "big.mkv", "action": "status", "status": 503, "times": 2}     # HTTP error (e.g. 429/500/503/403/404)
    {"method": "GET", "match": "big.mkv", "action": "drop", "after_bytes": 1000}             # connection dies mid-body
    {"method": "GET", "match": "big.mkv", "action": "corrupt"}                               # right length, wrong bytes
    {"method": "*",   "match": "",        "action": "delay", "seconds": 0.5}                 # slow server
    {"method": "PROPFIND", "match": "Locked", "action": "status", "status": 403}             # a folder that cannot be listed

``server.checksums = True`` makes PROPFIND and GET carry a SHA-1 (Nextcloud's ``oc:checksums`` / ``OC-Checksum``).
``server.requests`` is the log of ``(method, path)``; ``server.forbid_delete`` is a set of names that answer 403 to DELETE.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
import urllib.parse
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape


INVALID = "<>:\"|?*" + chr(92) + "%"   # chr(92) is the backslash
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def enc(name: str) -> str:
    """Component name -> a name any filesystem accepts (reversible): invalid characters, a trailing dot/space, a leading space,
    Windows device names and ``%`` become ``%XX``; an upper-case letter becomes ``^`` + lower-case so two names that differ only by
    case stay two files on a case-insensitive disk (a real Linux Nextcloud can hold both)."""
    out = []
    for index, ch in enumerate(name):
        bad = ch in INVALID or ch == "^" or (index == len(name) - 1 and ch in ". ") or (index == 0 and (ch == " " or (name.split(".")[0].upper() in RESERVED)))
        out.append(f"%{ord(ch):02X}" if bad else ("^" + ch.lower() if ch.isupper() else ch))
    return "".join(out)


def dec(name: str) -> str:
    return urllib.parse.unquote(re.sub(r"\^(.)", lambda m: m.group(1).upper(), name))


class FakeNextcloud:
    def __init__(self, root: Path, user: str = "alice", password: str = "app-pass-1234") -> None:
        self.root, self.user, self.password = Path(root), user, password
        self.requests: list[tuple[str, str]] = []
        self.revoked = False
        self.approved = threading.Event()
        self.forbid_delete: set[str] = set()
        self.faults: list[dict] = []
        self.checksums = False
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self.base = ""

    def resolve(self, path: str) -> Path:
        """On-disk location of a Nextcloud-style path: an existing plain name (written directly by a test) wins, else the encoded name."""
        target = self.root
        for part in [p for p in path.strip("/").split("/") if p]:
            plain = target / part
            is_device = part.split(".")[0].upper() in RESERVED   # Path('x/nul').exists() is True on Windows: it is the device
            target = plain if (plain.exists() and not is_device) else target / enc(part)
        return target

    def mkdir(self, path: str) -> Path:
        target = self.resolve(path)
        target.mkdir(parents=True, exist_ok=True)
        return target

    def put(self, path: str, data: bytes = b"", mtime: float | None = None) -> Path:
        """Create a file (any name, including ones Windows cannot store) at a Nextcloud-style path like 'Movies/a:b.mkv'."""
        target = self.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if mtime is not None:
            import os
            os.utime(target, (mtime, mtime))
        return target

    def files(self, path: str = "") -> list[str]:
        """Decoded relative paths of every file under ``path`` (what Nextcloud would list), sorted."""
        base = self.resolve(path)
        if not base.exists():
            return []
        return sorted("/".join(dec(part) for part in item.relative_to(base).parts) for item in base.rglob("*") if item.is_file())

    def folders(self, path: str = "") -> list[str]:
        base = self.resolve(path)
        return sorted("/".join(dec(part) for part in item.relative_to(base).parts) for item in base.rglob("*") if item.is_dir()) if base.exists() else []

    def fault(self, method: str, match: str, action: str, **options) -> dict:
        rule = {"method": method, "match": match, "action": action, "times": options.pop("times", None), **options}
        with self._lock:
            self.faults.append(rule)
        return rule

    def _take_fault(self, method: str, path: str) -> dict | None:
        with self._lock:
            for rule in self.faults:
                if rule["method"] not in ("*", method) or rule["match"] not in urllib.parse.unquote(path):
                    continue
                if rule["times"] is not None:
                    if rule["times"] <= 0:
                        continue
                    rule["times"] -= 1
                return rule
        return None

    # lifecycle
    def start(self) -> str:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:
                pass

            def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _authorized(self) -> bool:
                expected = "Basic " + base64.b64encode(f"{outer.user}:{outer.password}".encode()).decode()
                return self.headers.get("Authorization") == expected

            def _local(self, url_path: str) -> Path | None:
                prefix = f"/remote.php/dav/files/{urllib.parse.quote(outer.user, safe='')}"
                if not url_path.startswith(prefix):
                    return None
                relative = urllib.parse.unquote(url_path[len(prefix):]).strip("/")
                target = outer.resolve(relative).resolve()
                return target if str(target).startswith(str(outer.root.resolve())) else None

            def _read_body(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def _apply_fault(self, method: str, path: str) -> tuple[bool, dict | None]:
                """(handled, rule). ``status`` and ``delay`` are applied here; ``drop``/``corrupt`` are returned for the GET code."""
                rule = outer._take_fault(method, path)
                if not rule:
                    return False, None
                if rule["action"] == "delay":
                    time.sleep(rule.get("seconds", 0.5))
                    return False, None
                if rule["action"] == "status":
                    self._send(rule.get("status", 500), rule.get("body", b""), {"Retry-After": "1"} if rule.get("status") in (429, 503) else None)
                    return True, rule
                return False, rule

            def do_POST(self) -> None:
                outer.requests.append(("POST", self.path))
                body = self._read_body()
                if self.path == "/index.php/login/v2":
                    data = {"poll": {"token": "T0KEN", "endpoint": f"{outer.base}/index.php/login/v2/poll"}, "login": f"{outer.base}/login/v2/flow/T0KEN"}
                    return self._send(200, json.dumps(data).encode(), {"Content-Type": "application/json"})
                if self.path == "/index.php/login/v2/poll":
                    if urllib.parse.parse_qs(body.decode()).get("token") != ["T0KEN"] or not outer.approved.is_set():
                        return self._send(404)
                    data = {"server": outer.base, "loginName": outer.user, "appPassword": outer.password}
                    return self._send(200, json.dumps(data).encode(), {"Content-Type": "application/json"})
                self._send(404)

            def do_GET(self) -> None:
                path = urllib.parse.urlsplit(self.path).path
                outer.requests.append((self.command, path))
                if path == "/ocs/v2.php/cloud/user":
                    if not self._authorized():
                        return self._send(401)
                    data = {"ocs": {"data": {"id": outer.user, "display-name": "Alice Example", "email": "alice@example.org", "quota": {"used": 1024, "total": 10 * 1024 ** 3}}}}
                    return self._send(200, json.dumps(data).encode(), {"Content-Type": "application/json"})
                if not self._authorized():
                    return self._send(401)
                handled, rule = self._apply_fault(self.command, path)
                if handled:
                    return
                target = self._local(path)
                if not target or not target.is_file():
                    return self._send(404)
                size = target.stat().st_size
                start, end, status = 0, size - 1, 200
                header = self.headers.get("Range", "")
                if header.startswith("bytes="):
                    first, _, last = header[6:].partition("-")
                    if first == "":   # suffix range
                        start = max(0, size - int(last))
                    else:
                        start, end = int(first), min(int(last), size - 1) if last else size - 1
                    status = 206
                length = max(0, end - start + 1)
                headers = {"Accept-Ranges": "bytes", "Content-Length": str(length), "Last-Modified": formatdate(target.stat().st_mtime, usegmt=True)}
                if status == 206:
                    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
                if outer.checksums:
                    headers["OC-Checksum"] = "SHA1:" + hashlib.sha1(target.read_bytes()).hexdigest()
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                if self.command == "HEAD":
                    return
                sent = 0
                limit = rule.get("after_bytes", length // 2) if rule and rule["action"] == "drop" else None
                with open(target, "rb") as handle:
                    handle.seek(start)
                    while sent < length:
                        chunk = handle.read(min(256 * 1024, length - sent))
                        if not chunk:
                            break
                        if rule and rule["action"] == "corrupt":
                            chunk = bytes((b ^ 0xFF) for b in chunk)
                        if limit is not None and sent + len(chunk) > limit:
                            self.wfile.write(chunk[: max(0, limit - sent)])
                            self.wfile.flush()
                            self.close_connection = True
                            try:
                                self.connection.shutdown(2)
                            except OSError:
                                pass
                            return
                        self.wfile.write(chunk)
                        sent += len(chunk)

            do_HEAD = do_GET

            def do_DELETE(self) -> None:
                path = urllib.parse.urlsplit(self.path).path
                outer.requests.append(("DELETE", path))
                if path == "/ocs/v2.php/core/apppassword":
                    if not self._authorized():
                        return self._send(401)
                    outer.revoked = True
                    return self._send(200, b"{}", {"Content-Type": "application/json"})
                if not self._authorized():
                    return self._send(401)
                handled, _ = self._apply_fault("DELETE", path)
                if handled:
                    return
                target = self._local(path)
                if not target or not target.exists():
                    return self._send(404)
                if dec(target.name) in outer.forbid_delete:
                    return self._send(403)
                if target.is_dir():
                    import shutil
                    shutil.rmtree(target)
                else:
                    target.unlink()
                self._send(204)

            def do_PROPFIND(self) -> None:
                path = urllib.parse.urlsplit(self.path).path
                outer.requests.append(("PROPFIND", path))
                self._read_body()
                if not self._authorized():
                    return self._send(401)
                handled, _ = self._apply_fault("PROPFIND", path)
                if handled:
                    return
                target = self._local(path)
                if not target or not target.exists():
                    return self._send(404)
                depth = self.headers.get("Depth", "1")
                items = [target] + (sorted(target.iterdir()) if target.is_dir() and depth != "0" else [])
                prefix = f"/remote.php/dav/files/{urllib.parse.quote(outer.user, safe='')}"
                parts = []
                for item in items:
                    relative = "/".join(dec(part) for part in item.resolve().relative_to(outer.root.resolve()).parts)
                    href = prefix + ("/" + urllib.parse.quote(relative) if relative else "") + ("/" if item.is_dir() else "")
                    stat = item.stat()
                    if item.is_dir():
                        size = sum(f.stat().st_size for f in item.rglob("*") if f.is_file())
                        props = f"<d:resourcetype><d:collection/></d:resourcetype><oc:size>{size}</oc:size>"
                    else:
                        props = f"<d:resourcetype/><d:getcontentlength>{stat.st_size}</d:getcontentlength>"
                        if outer.checksums:
                            props += f"<oc:checksums><oc:checksum>SHA1:{hashlib.sha1(item.read_bytes()).hexdigest()}</oc:checksum></oc:checksums>"
                    props += f"<d:getlastmodified>{formatdate(stat.st_mtime, usegmt=True)}</d:getlastmodified><d:getetag>\"{int(stat.st_mtime)}\"</d:getetag>"
                    parts.append(f"<d:response><d:href>{escape(href)}</d:href><d:propstat><d:prop>{props}</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
                xml = '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">' + "".join(parts) + "</d:multistatus>"
                self._send(207, xml.encode(), {"Content-Type": "application/xml; charset=utf-8"})

        class QuietServer(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address) -> None:   # real clients (rclone) drop idle keep-alive connections: not an error
                pass

        self._server = QuietServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self.base

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
