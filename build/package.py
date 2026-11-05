r"""Pack dist/NextPull into the release asset the updater expects: NextPull-<version>.zip plus NextPull-<version>.zip.sha256.

    py -3 build\package.py            (build.ps1 runs it after PyInstaller)

The zip holds the *contents* of dist/NextPull at its root (NextPull.exe, _internal/, tools/, updater/ ...), with forward-slash paths.
It is refused if the updater is missing, and it is test-unpacked with the same code the updater uses.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def version() -> str:
    match = re.search(r'^__version__ = "([^"]+)"', (ROOT / "nextpull.py").read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        raise SystemExit("Cannot read __version__ from nextpull.py")
    return match.group(1)


def package(dist: Path, out_dir: Path) -> tuple[Path, Path]:
    for needed in ("NextPull.exe", "updater/NextPullUpdater.exe", "_internal/web/index.html"):
        if not (dist / needed).is_file():
            raise SystemExit(f"{dist / needed} is missing: build first (build/build.ps1) - a release without the updater could never update itself")
    name = f"NextPull-{version()}.zip"
    target = out_dir / name
    out_dir.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(dist.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                archive.write(path, path.relative_to(dist).as_posix())
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    sha_file = out_dir / (name + ".sha256")
    sha_file.write_text(f"{digest}  {name}\n", encoding="ascii")
    import installer   # prove the updater will accept what we are about to publish
    scratch = Path(tempfile.mkdtemp(prefix="nextpull-package-check-"))
    try:
        installer.unpack(target, scratch / "unpacked")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return target, sha_file


if __name__ == "__main__":
    zip_path, sha_path = package(ROOT / "dist" / "NextPull", ROOT / "dist")
    print(f"{zip_path.name}: {zip_path.stat().st_size / 1048576:.1f} MB, sha256 {sha_path.read_text().split()[0]}")
