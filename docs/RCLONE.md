# rclone integration

Code: `rclone_runner.py`. Test double: `tests/fake_rclone.py`.

## Finding rclone

`find_rclone(configured, extra_dirs)` in this order:

1. the path set in **Settings** (if set and the file exists; otherwise "not found", no silent fallback),
2. `tools\rclone\rclone.exe` or `rclone.exe` next to the program (and inside the PyInstaller bundle),
3. `rclone` on `PATH`.

`tools\Fetch-rclone.ps1` downloads the official build from `downloads.rclone.org` and **verifies its SHA-256** against the
`SHA256SUMS` published for that exact version before installing it into `tools\rclone\`.

## The remote is defined by environment variables

No rclone config file holds a login. `build_env()` gives the process (everything from the parent environment except any `RCLONE_*`):

| Variable | Value |
| --- | --- |
| `RCLONE_CONFIG_NC_TYPE` | `webdav` |
| `RCLONE_CONFIG_NC_URL` | `https://<server>/remote.php/dav/files/<user>` |
| `RCLONE_CONFIG_NC_VENDOR` | `nextcloud` |
| `RCLONE_CONFIG_NC_USER` | the Nextcloud user |
| `RCLONE_CONFIG_NC_PASS` | the app password **obscured** by `rclone obscure -` (password on stdin, never on a command line) |

`--config <data>\rclone.empty.conf` points at an empty file so rclone never reads the user's own `rclone.conf`.

## Command line

`rclone <copy|move> NC:<source> <destination>` plus (see `build_args`):

| Flag | From |
| --- | --- |
| `--transfers N`, `--checkers 4` | *Files at once* |
| `--multi-thread-streams N` | *Streams per file* (the old script used 4) |
| `--retries R+1`, `--low-level-retries 6`, `--timeout 60s`, `--contimeout 30s` | *Retries* + bounded network retries (see "Measured behaviour") |
| `--min-age Nm` | *Skip files newer than N minutes* (do not grab files still being written) |
| `--bwlimit 8M` or a timetable | *Speed limit* or *Speed timetable* (`08:00,2M 23:00,off`) |
| `--filter "- pat"` / `"+ pat"` / `"- **"` | ordered filter rules: guard excludes, your excludes, your includes, then `- **` if there are includes (an exclude always wins) |
| `--delete-empty-src-dirs` | move mode + *Remove empty folders* |
| `--checksum` | *Compare checksums* |
| `--dry-run` | dry run |
| `--use-json-log --log-level INFO --log-file <run>.jsonl --stats 0` | structured log per run |
| `--rc --rc-addr 127.0.0.1:<random> --rc-user nextpull --rc-pass <random>` | control channel, localhost only |

Never used on purpose: `--inplace`, anything that deletes local data, `sync` (which deletes in the destination).

## Copy vs move

* **copy**: files are downloaded, the Nextcloud source is untouched.
* **move**: rclone copies each file, **verifies** it (size/modtime, or checksum if enabled) and only then deletes the source file.
  NextPull never deletes remote files any other way. Downloads go to `name.partial` and are renamed when complete, so an interrupted
  run never leaves a half file under the final name.

## Progress and results

* **Live progress** from the rc API `core/stats` (polled every 0.5 s): `bytes`, `totalBytes`, `speed`, `eta`, `transferring[]`.
* **Per-file results** from the JSON log (`classify_event`):

| Log message | Meaning |
| --- | --- |
| `Copied (new)` / `Copied (replaced existing)` + `object` | file transferred (size = what rclone saw, else the local file size) |
| `Deleted` + `object` | the source was deleted (move) |
| `Skipped copy/move as --dry-run is set (size N)` + `object` | dry-run file: nothing transferred, listed with its size. Other `Skipped ...` lines (directory time, remove directory, delete) are ignored. |
| level `error`/`critical` with `object` | that file failed (cleared if a retry later copies it) |
| level `error` without `object` | general error (e.g. `Attempt 1/3 failed`), only the last one is kept as the reason |

* **Live control**: `core/bwlimit` changes the speed during the run; `core/quit` ends it (killed after 10 s if it hangs).

## Measured behaviour (real rclone v1.75.1, `tools/rclone_probe.py`, `tests/test_rclone_matrix.py`)

Everything below was observed with the real binary against a fake Nextcloud that can inject faults; each point is pinned by a test.

| Situation | What rclone does / what NextPull does |
| --- | --- |
| **Big files** (above 256 MiB, fetched with several ranged streams) | logged as **`Multi-thread Copied (new)`**, not `Copied (new)`. The parser missed it at first (0 files recorded for a download that had worked); pinned by a 280 MB test for Copy and Move |
| Rerun, files identical | `There was nothing to transfer` -> run `ok`, "Nothing new to download" |
| Local file differs | `Copied (replaced existing)` |
| **Move after an earlier copy** (crash between copy and delete) | only `Deleted` is logged: the file is recorded as **"Already had"** (`present`), counted as removed, not as a transfer |
| Names Windows cannot store (`a:b?.txt`, trailing dot/space) | transferred under look-alike names (`a：b？.txt`, `．`, `␠`); History keeps the remote name and the true size (`local_relpath`, rc sizes) |
| Paths over 260 characters | work (extended-length paths) |
| **Names differing only by case** on Windows (`Movie.mkv`/`movie.mkv`) | rclone would silently let one overwrite the other and a *move* would then delete both on Nextcloud. The **name-clash guard** (`rclone lsf -R` before the run) leaves all members of such groups untouched, lists them as *Skipped* and flags the run |
| Include + exclude | plain `--include` flags are evaluated **before** `--exclude`, whatever the order, so `*.mkv` + exclude `Skip/**` still copied `Skip/d.mkv`. NextPull uses ordered `--filter` rules instead |
| `403`/`404` on a file | per-file error, retried per attempt; one History row, error counted once |
| `429`/`503` a few times | retried transparently (honours Retry-After): run `ok` |
| Connection cut mid-file, on every request | rclone resumes with `Range` requests: the file arrives intact |
| Corrupt bytes, same length | **accepted** unless *Compare checksums* is on (size-only otherwise); with it: `corrupted on transfer: sha1 hashes differ`, file removed, run flagged. Needs the server to provide SHA-1 |
| Persistent `500` | stalls an attempt: 189 s at `--low-level-retries 10`, 41 s at 5, 7 s at 3. A connection cut every 500 KB needs one retry per cut, so 3 fails a 2 MB file. NextPull uses **6** |
| `DELETE` -> 403/404/500 in a move | `Couldn't delete: ...`; the file *was* downloaded: one row with a note and `deleted=0`; the parent folders are kept (`not deleting directories as there were IO errors`) |
| Invalid `--bwlimit` | rclone exits 2 before writing a log; NextPull validates the timetable when the job is saved and, if it ever happens, writes rclone's stderr into the run log |
| rclone killed mid-transfer | exit code 15, a `*.partial` file is left; the rerun removes it and completes; nothing was deleted on Nextcloud |
| Cancel | works in every phase, including while the server hangs (the pre-check runs on a helper thread); a cancelled move never deletes the source |

### Pre-check (`NextcloudSession.check_folder`)

Before rclone starts, a `PROPFIND Depth: 0` on the source folder tells *why* a run would fail: wrong login (401/403), folder missing
(404), maintenance (5xx, retried 3 times), or **a 200 HTML page instead of WebDAV** (a captive portal or proxy page, which rclone would
treat as an empty folder and report "nothing to transfer"). Each gets a plain-language message.

### File statuses in History

`ok` (transferred; a note if the delete on Nextcloud failed) | `present` (already downloaded earlier, removed from Nextcloud now) |
`dry` (dry run: would transfer, with its size) | `skipped` (name clash, left untouched) | `error`.

## Exit codes -> states

Exit code 0 means the run is `ok`, even if an attempt failed on the way (rclone logs those as errors). Otherwise: some files copied ->
`partial`, none -> `failed`. Messages for known codes (`exit_message`): 3/4 folder not found, 5 too many temporary errors, 7 fatal.
`cancelled` and `stopped` are decided by NextPull (it sent `core/quit`), regardless of the exit code.

## Notes for the Nextcloud/Cloudflare setup

rclone talks to whatever URL the user connected with. If that hostname is behind Cloudflare's **proxy**, large transfers pass through
it, which Cloudflare's free-plan terms do not allow for bulk video. Prefer a hostname that is not proxied (see [NEXTCLOUD.md](NEXTCLOUD.md)).
