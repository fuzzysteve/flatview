"""The flatc the tests and fuzzers compare against: pinned to the latest release.

    uv run python scripts/get_flatc.py        # download it (if needed), print its path

The official release binary for this platform goes into .tools/ (gitignored).
conftest.py and fuzz/oracle.py use it when present, so local runs and CI use
the same flatc regardless of what's on PATH. FLATVIEW_FLATC overrides it.
To move to a newer flatc, update FLATC_VERSION and FLATC_TAG.
"""

from __future__ import annotations

import io
import os
import platform
import shutil
import stat
import sys
import urllib.request
import zipfile
from pathlib import Path

FLATC_VERSION = "25.12.19"
FLATC_TAG = "v25.12.19-2026-02-06-03fffb2"     # the release's git tag (also names its source tarball)

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / ".tools" / f"flatc-{FLATC_VERSION}"


def asset() -> str:
    system, machine = platform.system(), platform.machine().lower()
    if system == "Linux":
        return "Linux.flatc.binary.g++-13.zip"
    if system == "Darwin":
        return "Mac.flatc.binary.zip" if machine in ("arm64", "aarch64") else "MacIntel.flatc.binary.zip"
    if system == "Windows":
        return "Windows.flatc.binary.zip"
    raise SystemExit(f"no flatc release binary for {system}/{machine}")


def binary_path() -> Path:
    return TOOLS / ("flatc.exe" if platform.system() == "Windows" else "flatc")


def resolve() -> str | None:
    """flatc to use: $FLATVIEW_FLATC, else the pinned one in .tools/, else PATH."""
    env = os.environ.get("FLATVIEW_FLATC")
    if env:
        return env
    if binary_path().exists():
        return str(binary_path())
    return shutil.which("flatc")


def ensure() -> Path:
    path = binary_path()
    if path.exists():
        return path
    url = f"https://github.com/google/flatbuffers/releases/download/{FLATC_TAG}/{asset()}"
    req = urllib.request.Request(url, headers={"User-Agent": "flatview-get-flatc"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    TOOLS.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [n for n in zf.namelist() if Path(n).name in ("flatc", "flatc.exe")]
        if not names:
            raise SystemExit(f"{url}: no flatc binary in the archive ({zf.namelist()})")
        path.write_bytes(zf.read(names[0]))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def main() -> int:
    print(ensure())
    return 0


if __name__ == "__main__":
    sys.exit(main())
