"""flatc as an oracle: its generated C++ verifier and its JSON printer.

The first use downloads the flatbuffers source for the installed flatc's
version and compiles the parser/text sources (about a minute). Harnesses are
built per schema and cached in fuzz/.cache/ (gitignored).

    from oracle import Oracle
    with Oracle("schema.fbs") as o:
        status, json_obj = o.check(buffer_bytes)   # "OK" | "FAIL" | "NOJSON"
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path

import flatview

HERE = Path(__file__).resolve().parent
CACHE = HERE / ".cache"
LIB_SOURCES = ("idl_parser", "idl_gen_text", "reflection", "util")
_NONFINITE = re.compile(r"([:\[,]\s*)(-?)(nan|inf|infinity)(?=\s*[,\]}])")


def flatc_version() -> str:
    out = subprocess.run(["flatc", "--version"], capture_output=True, text=True, check=True).stdout
    m = re.search(r"(\d+\.\d+\.\d+)", out)
    if not m:
        raise RuntimeError(f"cannot parse flatc version from {out!r}")
    return m.group(1)


def flatbuffers_source() -> Path:
    """Source tree matching the installed flatc, with libfb.a built."""
    ver = flatc_version()
    root = CACHE / f"flatbuffers-{ver}"
    lib = root / "libfb.a"
    if lib.exists():
        return root
    CACHE.mkdir(exist_ok=True)
    url = f"https://github.com/google/flatbuffers/archive/refs/tags/v{ver}.tar.gz"
    req = urllib.request.Request(url, headers={"User-Agent": "flatview-fuzz-oracle"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    with tempfile.TemporaryDirectory(dir=CACHE) as tmp:
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            tf.extractall(tmp, filter="data")
        src = Path(tmp) / f"flatbuffers-{ver}"
        objs = []
        for name in LIB_SOURCES:
            obj = Path(tmp) / f"{name}.o"
            subprocess.run(["g++", "-std=c++17", "-O1", "-c", "-I", str(src / "include"),
                            str(src / "src" / f"{name}.cpp"), "-o", str(obj)], check=True)
            objs.append(str(obj))
        subprocess.run(["ar", "rcs", str(src / "libfb.a"), *objs], check=True)
        if root.exists():
            shutil.rmtree(root)
        shutil.move(str(src), root)
    return root


def build(fbs: Path) -> Path:
    """Compile (or reuse) the oracle binary for one schema file."""
    fbs = Path(fbs).resolve()
    schema = flatview.load_schema(fbs)
    if schema.root is None:
        raise ValueError(f"{fbs} has no root_type")
    src = flatbuffers_source()
    texts = b"".join(p.read_bytes() for p in sorted(fbs.parent.glob("*.fbs")))
    key = hashlib.sha256(texts + str(fbs).encode() + (HERE / "oracle.cpp").read_bytes()
                         + flatc_version().encode()).hexdigest()[:16]
    out = CACHE / "oracles" / f"{fbs.stem}-{key}"
    binary = out / "oracle"
    if binary.exists():
        return binary
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run(["flatc", "--cpp", "--gen-all", "-o", str(out), "-I", str(fbs.parent), str(fbs)],
                   check=True, capture_output=True)
    header = f"{fbs.stem}_generated.h"
    ns = schema.root.namespace.replace(".", "::")
    verify = f"{ns + '::' if ns else ''}Verify{schema.root.short_name}Buffer"
    subprocess.run(["g++", "-std=c++17", "-O1", "-I", str(src / "include"), "-I", str(out),
                    f'-DSCHEMA_HEADER="{header}"', f"-DVERIFY_FN={verify}",
                    str(HERE / "oracle.cpp"), str(src / "libfb.a"), "-o", str(binary)], check=True)
    return binary


class Oracle:
    def __init__(self, fbs: str | os.PathLike):
        self.fbs = Path(fbs).resolve()
        self.binary = build(self.fbs)
        self.tmp = tempfile.TemporaryDirectory(prefix="oracle")
        self.path = Path(self.tmp.name) / "buf.bin"
        self._start()

    def _start(self) -> None:
        self.proc = subprocess.Popen([str(self.binary), str(self.fbs), str(self.fbs.parent)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, bufsize=0)

    def _crashed(self):
        self.proc.wait()
        err = self.proc.stderr.read().decode(errors="replace").strip().splitlines()
        self._start()
        return "CRASH", (err[-1] if err else f"exit {self.proc.returncode}")

    def check(self, data: bytes):
        """("OK", parsed JSON) | ("FAIL", None) | ("NOJSON", message) | ("CRASH", message).

        CRASH means flatc's verifier passed the buffer but its JSON printer
        aborted (a flatc bug); the oracle restarts itself.
        """
        self.path.write_bytes(data)
        try:
            self.proc.stdin.write(str(self.path).encode() + b"\n")
        except BrokenPipeError:
            return self._crashed()
        line = self.proc.stdout.readline()
        if not line:
            return self._crashed()
        line = line.decode().rstrip("\n")
        if line == "FAIL":
            return "FAIL", None
        if line.startswith("NOJSON"):
            return "NOJSON", line[7:]
        n = int(line[3:])
        body = b""
        while len(body) < n + 1:
            chunk = self.proc.stdout.read(n + 1 - len(body))
            if not chunk:
                return self._crashed()
            body += chunk
        text = body[:n].decode("utf-8", "surrogateescape")
        # flatc prints non-finite floats as bare words, which JSON lacks
        text = _NONFINITE.sub(
            lambda m: m.group(1) + ('"nan"' if m.group(3) == "nan" else f'"{m.group(2)}inf"'), text)
        try:
            return "OK", json.loads(text)
        except json.JSONDecodeError as e:
            return "BADJSON", f"{e}: ...{text[max(0, e.pos - 40):e.pos + 40]!r}..."

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        self.tmp.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
