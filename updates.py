"""Update check: is a newer NextPull published as a GitHub Release?

Pure functions plus one HTTP call (``latest_release``). Nothing here installs anything; downloading and installing is ``installer.py`` (see docs/UPDATES.md).

* The only request is ``GET https://api.github.com/repos/<repo>/releases/latest`` (public, no login, no data sent but a User-Agent).
* Drafts and pre-releases are never offered (the ``latest`` endpoint skips them; they are also ignored here).
* Download links must be HTTPS on GitHub's own hosts (:func:`trusted_url`).
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

REPO = "WikiZell/NextPull"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
USER_AGENT = "NextPull-update-check"
TRUSTED_HOSTS = ("github.com", "githubusercontent.com")
ASSET_PATTERN = re.compile(r"^NextPull[-_ .\w]*\.zip$", re.IGNORECASE)


class UpdateError(Exception):
    """A message that is fine to show to the user."""


def parse_version(text: str) -> tuple[tuple[int, ...], bool]:
    """'v0.2.0' -> ((0, 2, 0), False); '1.0.0-beta.1' -> ((1, 0, 0), True) (a pre-release sorts before its release)."""
    match = re.match(r"^\s*[vV]?(\d+(?:\.\d+)*)(.*)$", str(text or ""))
    if not match:
        raise UpdateError(f"Unrecognised version: {text!r}")
    numbers = tuple(int(part) for part in match.group(1).split("."))
    return numbers, match.group(2).strip().startswith(("-", "_"))


def is_newer(latest: str, current: str) -> bool:
    """True if ``latest`` is a later release than ``current``. A garbage tag is never "newer"."""
    try:
        (a, a_pre), (b, b_pre) = parse_version(latest), parse_version(current)
    except UpdateError:
        return False
    width = max(len(a), len(b))
    a, b = a + (0,) * (width - len(a)), b + (0,) * (width - len(b))
    if a != b:
        return a > b
    return b_pre and not a_pre


def trusted_url(url: str) -> bool:
    parts = urllib.parse.urlsplit(str(url or ""))
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == name or host.endswith("." + name) for name in TRUSTED_HOSTS)


def pick_asset(assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The program zip of a release plus its SHA-256 (from GitHub's ``digest`` field or a ``<zip>.sha256`` asset)."""
    for asset in assets:
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if not ASSET_PATTERN.match(name) or not trusted_url(url):
            continue
        digest = str(asset.get("digest") or "")
        sha_url = ""
        for other in assets:
            if str(other.get("name") or "").lower() == name.lower() + ".sha256" and trusted_url(str(other.get("browser_download_url") or "")):
                sha_url = str(other["browser_download_url"])
        return {"name": name, "url": url, "size": int(asset.get("size") or 0), "sha256": digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") else "", "sha256_url": sha_url}
    return None


def summarize(release: dict[str, Any]) -> dict[str, Any] | None:
    """The fields the app needs from GitHub's release JSON; None for drafts, pre-releases and tags that are not versions."""
    if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
        return None
    tag = str(release.get("tag_name") or "")
    try:
        parse_version(tag)
    except UpdateError:
        return None
    url = str(release.get("html_url") or "")
    return {"tag": tag, "version": tag.lstrip("vV"), "name": str(release.get("name") or tag), "notes": str(release.get("body") or "")[:6000], "page": url if trusted_url(url) else "",
            "published": str(release.get("published_at") or ""), "asset": pick_asset(release.get("assets") or [])}


def latest_release(url: str = API_URL, timeout: float = 15.0) -> dict[str, Any] | None:
    """The newest published release, or None if there is none yet. Raises UpdateError with a user-friendly message."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:   # noqa: S310 - fixed https GitHub API URL (tests pass a local one)
            body = response.read(2_000_000)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        if error.code in (403, 429):
            raise UpdateError("GitHub is limiting requests right now; the check will try again later") from error
        raise UpdateError(f"GitHub answered HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise UpdateError("Could not reach GitHub to check for updates") from error
    try:
        return summarize(json.loads(body.decode("utf-8")))
    except (ValueError, UnicodeDecodeError) as error:
        raise UpdateError("GitHub sent an unreadable answer") from error
