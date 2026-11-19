"""Explain why flatc's verifier rejects a buffer.

    uv run python fuzz/explain.py <schema.fbs> <buffer.bin> [...more buffers]

Builds a debug oracle (-O0 -g, FLATBUFFERS_DEBUG_VERIFICATION_FAILURE, which
asserts at the first failing check) and prints the backtrace frames from the
generated verifier, i.e. which table/field flatc was checking.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import flatview  # noqa: E402
from oracle import CACHE, FLATC, HERE as _H, flatbuffers_source  # noqa: E402


def debug_build(fbs: Path) -> Path:
    fbs = fbs.resolve()
    schema = flatview.load_schema(fbs)
    src = flatbuffers_source()
    out = CACHE / "oracles" / f"{fbs.stem}-debug"
    binary = out / "oracle"
    if binary.exists():
        return binary
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run([FLATC, "--cpp", "--gen-all", "-o", str(out), "-I", str(fbs.parent), str(fbs)],
                   check=True, capture_output=True)
    ns = schema.root.namespace.replace(".", "::")
    verify = f"{ns + '::' if ns else ''}Verify{schema.root.short_name}Buffer"
    subprocess.run(["g++", "-std=c++17", "-O0", "-g", "-DFLATBUFFERS_DEBUG_VERIFICATION_FAILURE",
                    "-I", str(src / "include"), "-I", str(out),
                    f'-DSCHEMA_HEADER="{fbs.stem}_generated.h"', f"-DVERIFY_FN={verify}",
                    str(_H / "oracle.cpp"), str(src / "libfb.a"), "-o", str(binary)], check=True)
    return binary


def explain(fbs: Path, buf: Path) -> list[str]:
    binary = debug_build(fbs)
    res = subprocess.run(["gdb", "-batch", "-ex", "run", "-ex", "bt",
                          "--args", str(binary), str(fbs.resolve()), str(fbs.resolve().parent)],
                         input=f"{buf.resolve()}\n", capture_output=True, text=True, timeout=120)
    frames = []
    for line in res.stdout.splitlines():
        m = re.match(r"#\d+\s+(?:0x[0-9a-f]+ in )?(.+?) \(.*\) at (.+?):(\d+)", line)
        if m and ("_generated.h" in m.group(2) or "verifier.h" in m.group(2)):
            frames.append(f"{m.group(1)}  ({Path(m.group(2)).name}:{m.group(3)})")
    return frames or ["(flatc accepted it, or no generated frames in the backtrace)"]


def main() -> int:
    fbs = Path(sys.argv[1])
    for b in sys.argv[2:]:
        print(f"== {b}")
        for f in explain(fbs, Path(b))[:6]:
            print("   ", f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
