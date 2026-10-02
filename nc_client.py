"""Minimal Nextcloud client for NextPull, standard library only.

* **Login Flow v2** (browser sign-in that hands the app an *app password*; the real password is never seen).
* **WebDAV PROPFIND** to browse the user's folders for the job picker.
* **OCS** user info (display name, quota) and revoking the app password on disconnect.

The actual file transfers are done by rclone; this module never downloads or deletes user files.
"""
from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from email.utils import parsedate_to_datetime
from typing import Any

USER_AGENT = "NextPull/1.0"
DAV = "{DAV:}"
OC = "{http://owncloud.org/ns}"
PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"><d:prop>'
    "<d:resourcetype/><d:getcontentlength/><d:getlastmodified/><oc:size/></d:prop></d:propfind>")


class NextcloudError(Exception):
    """An error with a message that is fine to show to the user. ``transient`` marks failures worth retrying (network, 5xx)."""

    def __init__(self, message: str, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass
class Entry:
    name: str
    path: str          # relative to the user's Files root, no leading slash
    is_dir: bool
    size: int
    modified: str      # ISO local-agnostic (UTC offset kept) or ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_server_url(raw: str) -> str:
    """'cloud.example.com', 'https://cloud.example.com/index.php/login' ... -> 'https://cloud.example.com'.
    A sub-folder install ('https://host/nextcloud') is kept."""
    text = str(raw or "").strip()
    if not text:
        raise NextcloudError("Enter the Nextcloud address")
    if "://" not in text:
        text = "https://" + text
    parts = urllib.parse.urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise NextcloudError("That does not look like a web address")
    path = re.sub(r"/(index\.php|apps|login|remote\.php|ocs|f)(/.*)?$", "", parts.path.rstrip("/"))
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def dav_files_url(server: str, user: str) -> str:
    return f"{server.rstrip('/')}/remote.php/dav/files/{urllib.parse.quote(user, safe='')}"


def basic_auth(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")


def _request(method: str, url: str, *, headers: dict[str, str] | None = None, data: bytes | None = None, timeout: float = 20.0) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, {key.lower(): value for key, value in response.headers.items()}, response.read()
    except urllib.error.HTTPError as error:
        body = error.read() if hasattr(error, "read") else b""
        return error.code, {key.lower(): value for key, value in (error.headers or {}).items()}, body
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise NextcloudError(f"Cannot reach the Nextcloud server ({reason})", transient=True) from error


# ---------------------------------------------------------------------------------------------- Login Flow v2

class LoginFlow:
    """``start()`` returns the page the user must open in a browser; ``poll_once()`` returns the credentials once the
    user approved, or None while still waiting."""

    def __init__(self, server: str, timeout: float = 20.0) -> None:
        self.server = normalize_server_url(server)
        self.timeout = timeout
        self._token = ""
        self._endpoint = ""

    def start(self) -> str:
        status, _, body = _request("POST", f"{self.server}/index.php/login/v2", timeout=self.timeout)
        if status == 404:
            raise NextcloudError("This address is not a Nextcloud server (or it is too old for browser sign-in)")
        if status != 200:
            raise NextcloudError(f"The server refused the sign-in request (HTTP {status})")
        try:
            data = json.loads(body.decode("utf-8"))
            self._token, self._endpoint, login = data["poll"]["token"], data["poll"]["endpoint"], data["login"]
        except (ValueError, KeyError, TypeError) as error:
            raise NextcloudError("Unexpected answer from the server while starting sign-in") from error
        return login

    def poll_once(self) -> dict[str, str] | None:
        if not self._token:
            raise NextcloudError("Sign-in was not started")
        status, _, body = _request("POST", self._endpoint, data=urllib.parse.urlencode({"token": self._token}).encode("ascii"),
                                   headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=self.timeout)
        if status == 404:
            return None
        if status != 200:
            raise NextcloudError(f"Sign-in failed (HTTP {status})")
        try:
            data = json.loads(body.decode("utf-8"))
            return {"server": normalize_server_url(data.get("server") or self.server), "user": str(data["loginName"]), "app_password": str(data["appPassword"])}
        except (ValueError, KeyError, TypeError) as error:
            raise NextcloudError("Unexpected answer from the server at the end of sign-in") from error


# ----------------------------------------------------------------------------------------------------- session

def parse_propfind(xml_bytes: bytes, base_path: str) -> list[Entry]:
    """Entries of a Depth-1 multistatus, without the folder itself. ``base_path`` is the URL path of the Files root
    (``/remote.php/dav/files/<user>``) so entry paths come out relative to it."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as error:
        raise NextcloudError("The server sent an unreadable folder listing") from error
    base = base_path.rstrip("/")
    entries: list[Entry] = []
    for response in root.findall(f"{DAV}response"):
        href = urllib.parse.unquote(urllib.parse.urlsplit(response.findtext(f"{DAV}href") or "").path)
        relative = href[len(base):] if href.startswith(base) else href
        relative = relative.strip("/")
        props = None
        for propstat in response.findall(f"{DAV}propstat"):
            if "200" in (propstat.findtext(f"{DAV}status") or ""):
                props = propstat.find(f"{DAV}prop")
                break
        if props is None:
            continue
        is_dir = props.find(f"{DAV}resourcetype/{DAV}collection") is not None
        raw_size = props.findtext(f"{OC}size") if is_dir else props.findtext(f"{DAV}getcontentlength")
        modified = ""
        stamp = props.findtext(f"{DAV}getlastmodified")
        if stamp:
            try:
                modified = parsedate_to_datetime(stamp).isoformat(timespec="seconds")
            except (TypeError, ValueError):
                modified = ""
        entries.append(Entry(name=relative.rsplit("/", 1)[-1], path=relative, is_dir=is_dir, size=int(raw_size) if raw_size and raw_size.lstrip("-").isdigit() else 0, modified=modified))
    return entries


class NextcloudSession:
    def __init__(self, server: str, user: str, app_password: str, timeout: float = 30.0) -> None:
        self.server, self.user, self.password, self.timeout = normalize_server_url(server), user, app_password, timeout

    @property
    def _auth(self) -> dict[str, str]:
        return {"Authorization": basic_auth(self.user, self.password)}

    @property
    def files_url(self) -> str:
        return dav_files_url(self.server, self.user)

    def list_dir(self, path: str = "") -> list[Entry]:
        """Children of ``path`` (relative to the Files root), folders first then by name. Raises NextcloudError."""
        clean = "/".join(part for part in str(path).replace("\\", "/").split("/") if part and part != ".")
        if ".." in clean.split("/"):
            raise NextcloudError("Invalid folder")
        url = self.files_url + ("/" + urllib.parse.quote(clean, safe="/") if clean else "") + "/"
        status, _, body = _request("PROPFIND", url, headers={**self._auth, "Depth": "1", "Content-Type": "application/xml"}, data=PROPFIND_BODY.encode("utf-8"), timeout=self.timeout)
        if status in (401, 403):
            raise NextcloudError("Nextcloud rejected the saved login. Disconnect and connect again.")
        if status == 404:
            raise NextcloudError(f"Folder not found: /{clean}")
        if status not in (200, 207):
            raise NextcloudError(f"Nextcloud answered HTTP {status} for the folder listing")
        base_path = urllib.parse.urlsplit(self.files_url).path
        entries = parse_propfind(body, base_path)
        children = [entry for entry in entries if entry.path != clean]
        return sorted(children, key=lambda entry: (not entry.is_dir, entry.name.lower()))

    def check_folder(self, path: str) -> bool:
        """Pre-flight for a run: is the server really answering WebDAV, is the login accepted and does ``path`` exist?
        Returns True if it is a folder (False for a single file). Raises NextcloudError with a clear message otherwise;
        ``error.transient`` is True for failures worth retrying."""
        clean = "/".join(part for part in str(path).replace("\\", "/").split("/") if part and part != ".")
        if ".." in clean.split("/"):
            raise NextcloudError("Invalid folder")
        url = self.files_url + ("/" + urllib.parse.quote(clean, safe="/") if clean else "")
        status, headers, body = _request("PROPFIND", url, headers={**self._auth, "Depth": "0", "Content-Type": "application/xml"}, data=PROPFIND_BODY.encode("utf-8"), timeout=self.timeout)
        if status in (401, 403):
            raise NextcloudError("Nextcloud rejected the saved login. Disconnect and connect again.")
        if status == 404:
            raise NextcloudError(f"The Nextcloud folder /{clean} was not found")
        if status in (429, 500, 502, 503, 504):
            raise NextcloudError(f"Nextcloud is not available right now (HTTP {status}); it may be in maintenance", transient=True)
        if status != 207:
            raise NextcloudError(f"The server did not answer like Nextcloud WebDAV (HTTP {status}); a maintenance or proxy page may be in the way", transient=True)
        try:
            root = ET.fromstring(body)
        except ET.ParseError as error:
            raise NextcloudError("The server answered with something that is not WebDAV; a maintenance or proxy page may be in the way", transient=True) from error
        collection = root.find(f".//{DAV}resourcetype/{DAV}collection")
        return collection is not None

    def user_info(self) -> dict[str, Any]:
        """Display name and quota from the OCS API. Missing pieces come back as None, never an exception for a
        server that hides them; auth problems still raise."""
        status, _, body = _request("GET", f"{self.server}/ocs/v2.php/cloud/user?format=json", headers={**self._auth, "OCS-APIRequest": "true", "Accept": "application/json"}, timeout=self.timeout)
        if status in (401, 403):
            raise NextcloudError("Nextcloud rejected the saved login. Disconnect and connect again.")
        if status != 200:
            raise NextcloudError(f"Nextcloud answered HTTP {status}")
        try:
            data = json.loads(body.decode("utf-8"))["ocs"]["data"]
        except (ValueError, KeyError, TypeError) as error:
            raise NextcloudError("Unexpected answer from Nextcloud") from error
        quota = data.get("quota") if isinstance(data.get("quota"), dict) else {}
        return {"display_name": data.get("display-name") or data.get("displayname") or self.user, "email": data.get("email") or "",
                "quota_used": quota.get("used"), "quota_total": quota.get("total") if isinstance(quota.get("total"), int) and quota.get("total", 0) > 0 else None}

    def revoke(self) -> bool:
        """Delete this app password on the server so a disconnect really removes access. False if it could not."""
        try:
            status, _, _ = _request("DELETE", f"{self.server}/ocs/v2.php/core/apppassword", headers={**self._auth, "OCS-APIRequest": "true"}, timeout=self.timeout)
        except NextcloudError:
            return False
        return status in (200, 204)
