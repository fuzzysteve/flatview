import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import flatview
from flatview._fbs import BUILTIN_ATTRIBUTES

DATA = Path(__file__).parent / "data"
SDE = DATA / "sde"                  # small SDE samples, gzipped as CCP serves them
SDE_SCHEMAS = DATA / "sde-schemas"  # every SDE table's .fbs and .bfbs
SDE_FULL = DATA / "sde-full"        # optional: scripts/fetch_sde_examples.py
KITCHEN = DATA / "kitchen"

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import get_flatc  # noqa: E402

# The pinned flatc (scripts/get_flatc.py) when it has been fetched, else PATH.
FLATC = get_flatc.resolve()
needs_flatc = pytest.mark.skipif(FLATC is None, reason="no flatc: run scripts/get_flatc.py")


def pytest_report_header(config):
    if FLATC is None:
        return "flatc: none (cross-check tests skipped; run scripts/get_flatc.py)"
    ver = subprocess.run([FLATC, "--version"], capture_output=True, text=True).stdout.strip()
    pinned = get_flatc.FLATC_VERSION
    note = "" if pinned in ver else f"  (NOT the pinned {pinned}: run scripts/get_flatc.py)"
    return f"flatc: {FLATC} - {ver}{note}"


def sde_tables(directory: Path) -> list[str]:
    return sorted(p.name[:-3] for p in directory.glob("*.fb")) if directory.is_dir() else []


# --------------------------------------------------------------------------
# schema normalisation, for comparing schemas from different sources

def _ref(t):
    return t.ref.name if t.ref is not None else None


def normalise(schema: flatview.Schema, builtins: bool = False) -> dict:
    def attrs(d):
        return {k: v for k, v in d.items() if builtins or k not in BUILTIN_ATTRIBUTES}

    def default(f):
        d = f.default
        if isinstance(d, float) and math.isnan(d):
            return "nan"
        return d

    objects = {}
    for o in schema.objects.values():
        objects[o.name] = {
            "is_struct": o.is_struct,
            "minalign": o.minalign if o.is_struct else None,
            "bytesize": o.bytesize if o.is_struct else None,
            "attributes": attrs(o.attributes),
            "documentation": o.documentation,
            "fields": [
                (f.name, f.type.base.name, f.type.element.name, _ref(f.type), f.type.fixed_length,
                 f.id, f.offset, default(f), f.deprecated, f.required, f.key, f.optional,
                 f.padding, attrs(f.attributes), f.documentation)
                for f in o.fields
            ],
        }
    enums = {}
    for e in schema.enums.values():
        enums[e.name] = {
            "is_union": e.is_union,
            "underlying": e.underlying.name,
            "attributes": attrs(e.attributes),
            "documentation": e.documentation,
            "values": [(v.name, v.value, _ref(v.union_type) if v.union_type else None,
                        v.union_type.base.name if v.union_type else None, attrs(v.attributes),
                        v.documentation)
                       for v in e.values],
        }
    return {"objects": objects, "enums": enums,
            "root": schema.root.name if schema.root else None,
            "file_ident": schema.file_ident, "file_ext": schema.file_ext}


# --------------------------------------------------------------------------
# flatc helpers

def _run(args):
    res = subprocess.run(args, capture_output=True, text=True)
    if res.returncode:
        raise AssertionError(f"flatc failed: {res.stderr or res.stdout}")


def flatc_to_json(schema_path: Path, data: bytes, *extra: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        binp = Path(tmp) / "data.bin"
        binp.write_bytes(data)
        _run([FLATC, "--json", "--strict-json", "--raw-binary", "--json-nested-bytes", *extra, "-o", tmp,
              "-I", str(schema_path.parent), str(schema_path), "--", str(binp)])
        return json.loads((Path(tmp) / "data.json").read_text())


def flatc_from_json(schema_path: Path, obj) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        jp = Path(tmp) / "data.json"
        jp.write_text(json.dumps(obj))
        _run([FLATC, "-b", "--json-nested-bytes", "-o", tmp, "-I", str(schema_path.parent), str(schema_path), str(jp)])
        return next(p for p in Path(tmp).iterdir() if p.name != "data.json").read_bytes()


def flatc_bfbs(schema_path: Path, builtins: bool = False) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        args = [FLATC, "--schema", "-b", "--bfbs-comments", "-o", tmp, "-I", str(schema_path.parent)]
        if builtins:
            args.append("--bfbs-builtins")
        _run([*args, str(schema_path)])
        return next(Path(tmp).glob("*.bfbs")).read_bytes()


def close_enough(a, b, path=""):
    """Deep compare; floats compared allowing for flatc's 12-decimal output."""
    if isinstance(a, dict) and isinstance(b, dict):
        assert set(a) == set(b), f"{path}: keys differ: {set(a) ^ set(b)}"
        for k in a:
            close_enough(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list) and isinstance(b, list):
        assert len(a) == len(b), f"{path}: lengths {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            close_enough(x, y, f"{path}[{i}]")
    elif isinstance(a, float) or isinstance(b, float):
        assert isinstance(a, (int, float)) and isinstance(b, (int, float)), f"{path}: {a!r} vs {b!r}"
        if math.isnan(a) or math.isnan(b):
            assert math.isnan(a) and math.isnan(b), f"{path}: {a!r} vs {b!r}"
        else:
            assert a == b or math.isclose(a, b, rel_tol=1e-11, abs_tol=1e-12), f"{path}: {a!r} vs {b!r}"
    else:
        assert a == b and type(a) is type(b), f"{path}: {a!r} vs {b!r}"


@pytest.fixture(scope="session")
def kitchen_schema() -> flatview.Schema:
    return flatview.load_schema(KITCHEN / "kitchen.fbs")
