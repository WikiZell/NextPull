"""A test double for rclone. Run as ``python fake_rclone.py <command> ...`` (tests use ``[sys.executable, this_file]`` as the
rclone command). It implements just what NextPull uses, with the same shapes as the real thing:

* ``version`` and ``obscure -`` (password on stdin);
* ``copy`` / ``move`` from a *local directory* standing in for the Nextcloud remote (``NC:path`` -> ``$FAKE_REMOTE_ROOT/path``)
  to a local destination: ``--min-age``, ``--include/--exclude``, ``--bwlimit`` (flat ``NM``), ``--dry-run``,
  ``--delete-empty-src-dirs``, ``--use-json-log --log-file``, and the **rc API** (``core/stats``, ``core/transferred``,
  ``core/bwlimit``, ``core/quit``) with basic auth;
* the JSON log lines NextPull parses (``Copied (new)``, ``Deleted``, ``Skipped copy as --dry-run``, errors).

Behaviour switches (environment): FAKE_SPEED (bytes/s, default 300000000), FAKE_FAIL_FILES (comma list: those files fail),
FAKE_FATAL=1 (exit 7 at once), FAKE_RETRY_NOISE=1 (log an 'Attempt 1/3 failed' error but succeed), FAKE_START_DELAY (s).
"""
from __future__ import annotations

import base64
import fnmatch
import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VALUE_FLAGS = {"--config", "--transfers", "--checkers", "--multi-thread-streams", "--retries", "--low-level-retries", "--timeout", "--contimeout",
               "--log-level", "--log-file", "--stats", "--rc-addr", "--rc-user", "--rc-pass", "--min-age", "--bwlimit", "--include", "--exclude", "--filter"}


def parse_rate(text: str) -> float:
    text = (text or "").strip().lower()
    if not text or text == "off":
        return 0.0
    units = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}
    try:
        return float(text[:-1]) * units[text[-1]] if text[-1] in units else float(text)
    except ValueError:
        return 0.0


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.bytes = 0
        self.total = 0
        self.speed = 0.0
        self.transferring: list[dict] = []
        self.transferred: list[dict] = []
        self.errors = 0
        self.rate = 0.0
        self.quit = False
        self.started = time.time()


def serve_rc(addr: str, user: str, password: str, state: State) -> ThreadingHTTPServer:
    expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:   # quiet
            pass

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            if self.headers.get("Authorization") != expected:
                self.send_response(401); self.end_headers(); return
            command = self.path.strip("/")
            with state.lock:
                if command == "core/stats":
                    reply = {"bytes": state.bytes, "totalBytes": state.total, "speed": state.speed, "eta": None, "errors": state.errors, "transfers": len(state.transferred),
                             "elapsedTime": time.time() - state.started, "transferring": [dict(item) for item in state.transferring]}
                elif command == "core/transferred":
                    reply = {"transferred": list(state.transferred)}
                elif command == "core/bwlimit":
                    if "rate" in body:
                        state.rate = parse_rate(str(body["rate"]))
                    reply = {"rate": str(body.get("rate", "off"))}
                elif command == "core/quit":
                    state.quit = True
                    reply = {}
                else:
                    self.send_response(404); self.end_headers(); return
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    host, port = addr.rsplit(":", 1)
    server = ThreadingHTTPServer((host, int(port)), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Log:
    def __init__(self, path: str | None) -> None:
        self.path = path

    def write(self, level: str, msg: str, obj: str | None = None) -> None:
        line = {"level": level, "msg": msg, "source": "fake/rclone.go:1", "time": datetime.now().astimezone().isoformat()}
        if obj is not None:
            line["object"] = obj
            line["objectType"] = "*local.Object"
        text = json.dumps(line)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(text + "\n")
        else:
            print(text, file=sys.stderr)


def matches(patterns: list[str], relative: str) -> bool:
    name = relative.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(relative, pattern[1:]) if pattern.startswith("/") else (fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(name, pattern)) for pattern in patterns)


def allowed(filters: list[str], relative: str) -> bool:
    """rclone ``--filter`` rules: the FIRST matching rule decides ('+ pat' include, '- pat' exclude); no match means included."""
    for rule in filters:
        if matches([rule[2:].strip()], relative):
            return rule.startswith("+")
    return True


def main(argv: list[str]) -> int:
    if not argv:
        return 2
    command, rest = argv[0], argv[1:]
    if command == "version":
        print("rclone v1.63.1-fake\n- os/version: test")
        return 0
    if command == "obscure":
        password = sys.stdin.read().strip() if rest[:1] == ["-"] else (rest[0] if rest else "")
        print("obscured:" + base64.urlsafe_b64encode(password.encode()).decode().rstrip("="))
        return 0
    if command == "lsf" and rest:   # what NextPull's name-clash guard uses: relative file paths, one per line
        opts, sw, i = {}, set(), 1
        while i < len(rest):
            if rest[i] in VALUE_FLAGS:
                opts.setdefault(rest[i], []).append(rest[i + 1]); i += 2
            else:
                sw.add(rest[i]); i += 1
        root = Path(os.environ["FAKE_REMOTE_ROOT"]) / rest[0].partition(":")[2]
        if not root.is_dir():
            print("directory not found", file=sys.stderr)
            return 3
        for file in sorted(root.rglob("*")):
            if file.is_file():
                rel = file.relative_to(root).as_posix()
                if allowed(opts.get("--filter", []), rel):
                    print(rel)
        return 0
    if command not in ("copy", "move") or len(rest) < 2:
        print("fake rclone: unsupported command", file=sys.stderr)
        return 2

    source, dest, flags = rest[0], rest[1], rest[2:]
    options: dict[str, list[str]] = {}
    switches: set[str] = set()
    index = 0
    while index < len(flags):
        flag = flags[index]
        if flag in VALUE_FLAGS:
            options.setdefault(flag, []).append(flags[index + 1]); index += 2
        else:
            switches.add(flag); index += 1

    log = Log(options.get("--log-file", [None])[0])
    state = State()
    state.rate = parse_rate(options.get("--bwlimit", [""])[0])
    server = serve_rc(options["--rc-addr"][0], options["--rc-user"][0], options["--rc-pass"][0], state) if "--rc-addr" in options else None
    time.sleep(float(os.environ.get("FAKE_START_DELAY", "0")))

    if os.environ.get("FAKE_FATAL") == "1":
        log.write("critical", "Failed to create file system for \"NC:\": 401 Unauthorized")
        return 7
    if os.environ.get("RCLONE_CONFIG_NC_TYPE") != "webdav" or not os.environ.get("RCLONE_CONFIG_NC_PASS", "").startswith("obscured:"):
        log.write("critical", "remote NC is not configured through the environment")
        return 1
    remote, _, path = source.partition(":")
    root = Path(os.environ["FAKE_REMOTE_ROOT"]) / path
    if not root.is_dir():
        log.write("error", "error listing: directory not found")
        return 3

    filters = options.get("--filter", [])
    min_age = 0.0
    if "--min-age" in options:
        text = options["--min-age"][0]
        min_age = float(text[:-1]) * 60 if text.endswith("m") else float(text.rstrip("s"))
    dry, move = "--dry-run" in switches, command == "move"
    speed_env = float(os.environ.get("FAKE_SPEED", "300000000"))
    fail_names = {name for name in os.environ.get("FAKE_FAIL_FILES", "").split(",") if name}
    transfers_failed = 0

    candidates = []
    for file in sorted(root.rglob("*")):
        if not file.is_file():
            continue
        relative = file.relative_to(root).as_posix()
        if not allowed(filters, relative):
            continue
        if min_age and time.time() - file.stat().st_mtime < min_age:
            continue
        candidates.append((relative, file))
    state.total = sum(file.stat().st_size for _, file in candidates)
    if os.environ.get("FAKE_RETRY_NOISE") == "1":
        log.write("error", "Attempt 1/3 failed with 1 errors and: temporary glitch")

    for relative, file in candidates:
        if state.quit:
            break
        size = file.stat().st_size
        if dry:
            log.write("notice", f"Skipped {'move' if move else 'copy'} as --dry-run is set (size {size})", relative)   # real rclone wording
            continue
        if file.name in fail_names:
            log.write("error", "Failed to copy: simulated failure", relative)
            transfers_failed += 1
            with state.lock:
                state.errors += 1
            continue
        target = Path(dest) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".partial")
        entry = {"name": relative, "size": size, "bytes": 0, "percentage": 0, "speed": 0.0, "eta": None}
        with state.lock:
            state.transferring = [entry]
        began = time.time()
        aborted = False
        with open(file, "rb") as reader, open(partial, "wb") as writer:
            while True:
                chunk = reader.read(64 * 1024)
                if not chunk:
                    break
                writer.write(chunk)
                with state.lock:
                    state.bytes += len(chunk)
                    entry["bytes"] += len(chunk)
                    entry["percentage"] = int(100 * entry["bytes"] / size) if size else 100
                    rate = state.rate or speed_env
                    rate = min(rate, speed_env)
                    elapsed = max(time.time() - began, 1e-6)
                    state.speed = entry["speed"] = entry["bytes"] / elapsed
                    quit_now = state.quit
                if quit_now:
                    aborted = True
                    break
                time.sleep(len(chunk) / rate)
        if aborted:
            partial.unlink(missing_ok=True)
            break
        os.replace(partial, target)
        os.utime(target, (file.stat().st_atime, file.stat().st_mtime))
        log.write("info", "Copied (new)", relative)
        with state.lock:
            state.transferred.append({"name": relative, "size": size, "bytes": size, "checked": False, "error": ""})
            state.transferring = []
        if move:
            file.unlink()
            log.write("info", "Deleted", relative)

    if move and "--delete-empty-src-dirs" in switches and not state.quit and not dry:
        for directory in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            try:
                directory.rmdir()
                log.write("info", "Removed empty directory", directory.relative_to(root).as_posix())
            except OSError:
                pass
    time.sleep(0.05)
    if server:
        server.shutdown()
    return 1 if transfers_failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
