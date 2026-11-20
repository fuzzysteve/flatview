"""Benchmark encoding: flatview's schema-driven encoder vs the official library.

    uv run --group bench python benchmarks/bench_encode.py [SDE_DIR]

Each side starts from its own in-memory form of the same data and produces
bytes:

* flatview, from dicts: ``schema.encode(d)``, where ``d`` is the file's
  ``_asdict(defaults=False, enums="value")`` (keyed vectors are re-sorted).
* flatview, from a view: ``schema.encode(fb.root)``, copying a buffer.
* official: the object API (``flatc --python --gen-object-api``),
  ``TableT.Pack(builder)`` + ``Finish``, starting from an unpacked object tree.
* stdlib ``json.dumps`` of the same dicts, as a pure-Python baseline.

Every output is decoded again and checked against the original. SDE_DIR holds
<table>.fb/.bfbs/.fbs (default: tests/data/sde-full, see
scripts/fetch_sde_examples.py). Uses the pinned flatc (scripts/get_flatc.py).
"""

import gc
import importlib
import json
import os
import subprocess
import sys
import tempfile
import time

import flatbuffers

import flatview

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import get_flatc  # noqa: E402

SDE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "tests", "data", "sde-full")
TABLES = [("types", "TypesTable"), ("typeDogma", "TypeDogmaTable"), ("mapSolarSystems", "MapSolarSystemsTable")]


def best(fn, repeat):
    times = []
    for _ in range(repeat):
        gc.collect()
        t = time.perf_counter()
        out = fn()
        times.append(time.perf_counter() - t)
    return min(times), out


def main() -> int:
    flatc = get_flatc.resolve()
    if flatc is None:
        print("no flatc: run scripts/get_flatc.py")
        return 1
    tmp = tempfile.mkdtemp(prefix="fbbench")
    for table, _ in TABLES:
        subprocess.run([flatc, "--python", "--gen-object-api", "-o", tmp, os.path.join(SDE, table + ".fbs")],
                       check=True, capture_output=True)
    sys.path.insert(0, tmp)
    print(f"flatc {get_flatc.FLATC_VERSION}, flatbuffers {flatbuffers.__version__}, "
          f"Python {sys.version.split()[0]}")
    print(f"{'':27s}{'flatview (dict)':>17s}{'flatview (view)':>17s}{'official Pack':>17s}{'json.dumps':>14s}")

    for table, cls in TABLES:
        with open(os.path.join(SDE, table + ".fb"), "rb") as fh:
            data = fh.read()
        schema = flatview.load_schema(os.path.join(SDE, table + ".bfbs"))
        root = schema.read(data)
        expected = root._asdict(defaults=False, enums="value")
        repeat = 2 if len(data) > 50e6 else 3

        t_dict, out = best(lambda: schema.encode(expected), repeat)
        assert schema.read(out, verify=True)._asdict(defaults=False, enums="value") == expected
        t_view, out = best(lambda: schema.encode(root), repeat)
        assert schema.read(out, verify=True)._asdict(defaults=False, enums="value") == expected

        T = getattr(importlib.import_module(f"sde.{table}.{cls}"), cls + "T")
        obj = T.InitFromPackedBuf(data, 0)

        def pack():
            b = flatbuffers.Builder(len(data))
            b.Finish(obj.Pack(b))
            return bytes(b.Output())
        t_off, out = best(pack, repeat)
        # the official builder drops a -0.0 equal to its 0.0 default (as flatc
        # does), so compare with defaults filled in, where -0.0 == 0.0
        assert schema.read(out, verify=True)._asdict() == root._asdict()

        t_json, _ = best(lambda: json.dumps(expected), repeat)
        del out
        name = f"{table}.fb ({len(data) / 1e6:.0f} MB)"
        print(f"{name:27s}{t_dict:16.2f}s{t_view:16.2f}s{t_off:16.2f}s{t_json:13.2f}s"
              f"   official/flatview {t_off / t_dict:.1f}x")
    return 0


if __name__ == "__main__":
    sys.exit(main())
