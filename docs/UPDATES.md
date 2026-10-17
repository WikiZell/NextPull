# Updates (developer notes)

`updates.py` is pure apart from one HTTP call:

- `latest_release(url)` -> `summarize(json)`: tag, notes, release page and the program zip (`NextPull*.zip`) with its SHA-256 (GitHub's `digest` field or a `<zip>.sha256` asset). Drafts, pre-releases and non-version tags give `None`.
- `is_newer(latest, current)`: numeric compare, a pre-release sorts before its release, garbage is never newer.
- `trusted_url(url)`: HTTPS on `github.com` / `*.githubusercontent.com` only (also used for the release page that `open_release_page` opens).

`NextPullApi`: a daemon thread (`_update_loop`) checks `UPDATE_FIRST_DELAY` seconds after `start_engine`, then every `UPDATE_INTERVAL` while the `check_updates` setting is on.
The result is kept in memory (`_update`); `status()` carries `update: {version, installable}` for the banner when the version is newer, not skipped and the setting is on.
Bridge methods: `check_update` (always checks), `update_info`, `skip_update(version)` (state `update_skipped`), `open_release_page`. A notification is shown once per version (state `update_notified`).
Tests set `api._update_url` to a local fake server (`tests/test_updates.py`); never let a test reach the real GitHub.

Release convention for the future installer: tag `vX.Y.Z`, asset `NextPull-X.Y.Z.zip` containing the contents of `dist\NextPull`, plus `NextPull-X.Y.Z.zip.sha256`.
