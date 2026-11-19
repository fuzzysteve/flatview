"""Regenerate tests/data/old-bfbs/: .bfbs files written by historic flatc releases.

    uv run python scripts/make_old_bfbs.py /path/to/flatc-1.12.0 /path/to/flatc-2.0.0 ...

Each flatc compiles every schema in SCHEMAS it supports, into
tests/data/old-bfbs/<version>/<name>.bfbs. Schemas an old version can't
compile (e.g. optional scalars before 2.0) are skipped. Build old flatc from
the release source tarballs; the test suite only needs the committed outputs.
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "data" / "old-bfbs"
SCHEMAS = [
    "tests/data/kitchen/kitchen.fbs",
    "tests/data/evolution/v1.fbs",
    "tests/data/evolution/v2.fbs",
    "tests/data/unions/members.fbs",
    "src/flatview/reflection.fbs",
    "tests/data/sde/categories.fbs",
    "tests/data/sde/masteries.fbs",
    "tests/data/sde/dogmaEffects.fbs",
]


def version(flatc: str) -> str:
    out = subprocess.run([flatc, "--version"], capture_output=True, text=True).stdout
    return re.search(r"\d+\.\d+\.\d+", out).group(0)


def compile_one(flatc: str, schema: Path, dest: Path) -> str | None:
    for flags in (["--bfbs-comments", "--bfbs-builtins"], []):      # old versions lack the flags
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run([flatc, "--schema", "-b", *flags, "-o", tmp, "-I", str(schema.parent), str(schema)],
                               capture_output=True, text=True)
            out = list(Path(tmp).glob("*.bfbs"))
            if r.returncode == 0 and out:
                dest.write_bytes(out[0].read_bytes())
                return " ".join(flags) or "(no flags)"
            if "unknown commandline argument" not in (r.stderr + r.stdout):
                return None
    return None


def main() -> int:
    for flatc in sys.argv[1:]:
        v = version(flatc)
        d = OUT / v
        d.mkdir(parents=True, exist_ok=True)
        for s in SCHEMAS:
            schema = ROOT / s
            how = compile_one(flatc, schema, d / f"{schema.stem}.bfbs")
            print(f"{v:9s} {schema.name:20s} {how or 'not supported'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
