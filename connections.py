"""Connections: the servers a job can pull from.

* ``nextcloud``: the original connection (browser sign-in with an app password, see ``nc_client.LoginFlow``). It keeps its own saved login and
  has the fixed id :data:`NEXTCLOUD_ID`, so every older job and settings file keeps working unchanged.
* ``sftpgo``: an SFTPGo (or any plain WebDAV) server reached over HTTPS: user name + password, plus an optional *access key* header that a
  reverse proxy in front of the server can require so that strangers get no answer at all.

All transfers still go through rclone's ``webdav`` backend; this module only describes the connection, validates it and builds the pieces the
rest of the app needs (a session for browsing, the rclone remote, a public view without secrets, discovery and invite codes).
"""
from __future__ import annotations

import base64
import json
import re
import urllib.parse
import uuid
from typing import Any

from nc_client import NextcloudError, NextcloudSession, WebdavSession, _request, is_private_host  # noqa: F401  (is_private_host re-exported)

NEXTCLOUD_ID = "nextcloud"
SFTPGO = "sftpgo"
TYPE_LABELS = {"nextcloud": "Nextcloud", SFTPGO: "SFTPGo / WebDAV"}
INVITE_PREFIX = "nextpull-invite:"
_HEADER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")


# ---------------------------------------------------------------------------------------------------- addresses

def normalize_webdav_url(raw: str) -> str:
    """'pull.example.com' -> 'https://pull.example.com'. ``http://`` is accepted only for local-network hosts: a password or access key
    must never travel over the internet unencrypted. A path prefix is kept (a server published under a sub-path)."""
    text = str(raw or "").strip()
    if not text:
        raise NextcloudError("Enter the server address")
    if "://" not in text:
        text = "https://" + text
    parts = urllib.parse.urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise NextcloudError("That does not look like a web address")
    if parts.scheme == "http" and not is_private_host(parts.hostname):
        raise NextcloudError("Use https:// for a server on the internet. Your password and access key would be sent unencrypted otherwise")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def clean_header(name: Any, value: Any) -> tuple[str, str]:
    """The optional access-key header: both empty, or a valid name and a value without control characters or commas (rclone's list format)."""
    name, value = str(name or "").strip(), str(value or "").strip()
    if not name and not value:
        return "", ""
    if not _HEADER_NAME.match(name):
        raise ValueError("The access key name may only contain letters, digits and dashes (for example X-Pull-Key)")
    if not value or len(value) > 256 or re.search(r"[\x00-\x1f\x7f,]", value):
        raise ValueError("The access key value is empty, too long, or contains a comma or control character")
    return name, value


# ---------------------------------------------------------------------------------------------------- records

def new_connection_id() -> str:
    return "sg-" + uuid.uuid4().hex[:6]


def clean_connection(raw: Any) -> dict[str, str]:
    """Validate a connection record from the UI. Raises ValueError/NextcloudError with a message fit for the user."""
    if not isinstance(raw, dict):
        raise ValueError("Invalid connection")
    server = normalize_webdav_url(raw.get("server"))
    user = str(raw.get("user") or "").strip()
    password = str(raw.get("password") or "")
    if not user:
        raise ValueError("Enter the user name")
    if not password:
        raise ValueError("Enter the password")
    header_name, header_value = clean_header(raw.get("header_name"), raw.get("header_value"))
    host = urllib.parse.urlsplit(server).hostname or server
    label = str(raw.get("label") or "").strip()[:60] or f"{host} ({user})"
    conn_id = str(raw.get("id") or "").strip() or new_connection_id()
    if conn_id == NEXTCLOUD_ID or not re.match(r"^[A-Za-z0-9_-]{1,32}$", conn_id):
        raise ValueError("Invalid connection id")
    return {"id": conn_id, "type": SFTPGO, "label": label, "server": server, "user": user, "password": password, "header_name": header_name, "header_value": header_value}


def public_view(record: dict[str, Any]) -> dict[str, Any]:
    """What the UI may see: never the password or the access key value."""
    return {"id": record["id"], "type": record["type"], "type_label": TYPE_LABELS.get(record["type"], record["type"]), "label": record.get("label") or record["id"],
            "server": record.get("server", ""), "user": record.get("user", ""), "has_access_key": bool(record.get("header_name"))}


def secrets_of(creds: dict[str, Any]) -> list[str]:
    """Values that must never appear in a log (registered with ``applog.register_secret``)."""
    return [value for value in (creds.get("app_password"), creds.get("password"), creds.get("header_value")) if value]


# ---------------------------------------------------------------------------------------------------- sessions and remotes

def make_session(creds: dict[str, Any], timeout: float = 30.0) -> NextcloudSession:
    """The browsing/pre-check client for a connection's credentials (``creds`` as returned by ``SecretStore.creds_for``)."""
    if creds.get("type") == SFTPGO:
        headers = {creds["header_name"]: creds["header_value"]} if creds.get("header_name") else None
        return WebdavSession(creds["server"], creds["user"], creds["password"], timeout=timeout, headers=headers)
    return NextcloudSession(creds["server"], creds["user"], creds["app_password"], timeout=timeout, uid=creds.get("uid"))


def product_name(creds: dict[str, Any]) -> str:
    return "the server" if creds.get("type") == SFTPGO else "Nextcloud"


# ---------------------------------------------------------------------------------------------------- discovery

def discover(address: str, timeout: float = 8.0) -> dict[str, Any]:
    """Find out what answers at ``address`` without credentials: Nextcloud (``/status.php``), SFTPGo or another WebDAV server (a 401 with a
    ``WWW-Authenticate`` challenge, SFTPGo also names itself in the ``Server`` header), or nothing. A server hidden behind an access-key gate
    does not answer strangers, which is reported as ``unknown`` with a hint to use the invite code."""
    try:
        server = normalize_webdav_url(address)
    except NextcloudError as error:
        return {"ok": False, "type": "unknown", "error": str(error)}
    try:
        status, _, body = _request("GET", f"{server}/status.php", timeout=timeout)
        if status == 200:
            try:
                info = json.loads(body.decode("utf-8"))
            except ValueError:
                info = {}
            if isinstance(info, dict) and info.get("installed") is not None and ("version" in info or "productname" in info):
                return {"ok": True, "type": "nextcloud", "server": server, "version": str(info.get("versionstring") or info.get("version") or "")}
        status, headers, _ = _request("PROPFIND", server + "/", headers={"Depth": "0"}, timeout=timeout)
    except NextcloudError as error:
        return {"ok": False, "type": "unknown", "server": server, "error": str(error), "hint": "If the server needs an access key, use the invite code you were given."}
    banner = headers.get("server", "").strip()
    if status in (401, 403) and (banner.lower().startswith("sftpgo") or "basic" in headers.get("www-authenticate", "").lower() or status == 401):
        version = banner.split("/", 1)[1] if banner.lower().startswith("sftpgo/") else ""
        return {"ok": True, "type": SFTPGO, "server": server, "version": version, "product": "SFTPGo" if banner.lower().startswith("sftpgo") else "WebDAV server"}
    if status in (200, 207):
        return {"ok": True, "type": SFTPGO, "server": server, "version": "", "product": "WebDAV server (no login needed?)"}
    return {"ok": False, "type": "unknown", "server": server, "error": f"The server answered HTTP {status}, which is not a Nextcloud or WebDAV login.",
            "hint": "If the server needs an access key, use the invite code you were given."}


# ---------------------------------------------------------------------------------------------------- invite codes

def make_invite(server: str, user: str = "", header_name: str = "", header_value: str = "", label: str = "") -> str:
    """One string an administrator can hand over: address, optional user name, optional access key, optional label. **Never the password.**"""
    data: dict[str, Any] = {"v": 1, "t": SFTPGO, "u": normalize_webdav_url(server)}
    if user:
        data["user"] = user
    if label:
        data["l"] = label
    name, value = clean_header(header_name, header_value)
    if name:
        data["k"] = [name, value]
    return INVITE_PREFIX + base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")


def parse_invite(code: str) -> dict[str, str]:
    """The fields of an invite code, ready to prefill the form. Raises ValueError for anything else."""
    text = str(code or "").strip()
    if not text.startswith(INVITE_PREFIX):
        raise ValueError("That is not a NextPull invite code")
    payload = text[len(INVITE_PREFIX):]
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8"))
        if not isinstance(data, dict) or data.get("v") != 1 or data.get("t") != SFTPGO:
            raise ValueError("unsupported")
        server = normalize_webdav_url(data["u"])
        key = data.get("k") or ["", ""]
        name, value = clean_header(key[0], key[1])
    except (ValueError, KeyError, TypeError, IndexError, NextcloudError) as error:
        raise ValueError("The invite code is damaged or not valid") from error
    return {"server": server, "user": str(data.get("user") or ""), "label": str(data.get("l") or "")[:60], "header_name": name, "header_value": value}
