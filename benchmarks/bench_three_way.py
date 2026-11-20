"""flatview vs the official flatbuffers library vs stdlib json, same data.

    uv run --group bench python benchmarks/bench_three_way.py [FB_DIR] [JSONL_DIR]

flatview and official read types.fb; json reads types.jsonl (the SDE's JSONL
export of the same build). The official library uses `flatc --python
--gen-object-api` code generated into a temp dir. Each measurement is a fresh
process and includes opening/reading the file. Reported: best wall time of
two runs, then peak Python heap (tracemalloc, separate run).
"""

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import get_flatc  # noqa: E402

# the pinned flatc (scripts/get_flatc.py), as the tests and fuzzers use, so the
# generated official code matches the flatbuffers package version
FLATC = get_flatc.resolve() or sys.exit("no flatc: run scripts/get_flatc.py")

FB = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "tests", "data", "sde-full")
JL = sys.argv[2] if len(sys.argv) > 2 else "/opt/sde/files"

OFFICIAL_OPEN = "data = open(f'{FB}/types.fb','rb').read(); t = TT.GetRootAs(data, 0)\n"
JSON_LINES = "open(f'{JL}/types.jsonl','rb')"

SCENARIOS = {
    "look up one type (early id 587)": {
        "flatview": "x = flatview.open(f'{FB}/types.fb').rows.lookup(587).name.en",
        "official": OFFICIAL_OPEN + (
            "for i in range(t.RowsLength()):\n    r = t.Rows(i)\n"
            "    if r.TypeId() == 587:\n        x = r.Name().En().decode(); break"),
        "json": (f"for line in {JSON_LINES}:\n    r = json.loads(line)\n"
                 "    if r['typeID'] == 587:\n        x = r['name']['en']; break"),
    },
    "look up one type (last id)": {
        "flatview": "x = flatview.open(f'{FB}/types.fb').rows.lookup(LAST).name.en",
        "official": OFFICIAL_OPEN + (
            "for i in range(t.RowsLength()):\n    r = t.Rows(i)\n"
            "    if r.TypeId() == LAST:\n        x = r.Name().En().decode(); break"),
        "json": (f"for line in {JSON_LINES}:\n    r = json.loads(line)\n"
                 "    if r['typeID'] == LAST:\n        x = r['name']['en']; break"),
    },
    "1,000 lookups (others: build an index first)": {
        "flatview": "rows = flatview.open(f'{FB}/types.fb').rows\nx = [rows.lookup(i).name.en for i in IDS]",
        "official": OFFICIAL_OPEN + (
            "idx = {}\nfor i in range(t.RowsLength()):\n    r = t.Rows(i); idx[r.TypeId()] = r\n"
            "x = [idx[i].Name().En().decode() for i in IDS]"),
        "json": (f"d = {{r['typeID']: r for r in map(json.loads, {JSON_LINES})}}\n"
                 "x = [d[i]['name']['en'] for i in IDS]"),
    },
    "scan all rows, 4 fields": {
        "flatview": ("s = 0\nfor r in flatview.open(f'{FB}/types.fb').rows:\n"
                     "    s += r.groupID + len(r.name.en) + int(r.mass or 0) + bool(r.published)"),
        "official": OFFICIAL_OPEN + (
            "s = 0\nfor i in range(t.RowsLength()):\n    r = t.Rows(i)\n"
            "    s += r.GroupId() + len(r.Name().En().decode()) + int(r.Mass() or 0) + bool(r.Published())"),
        "json": (f"s = 0\nfor line in {JSON_LINES}:\n    r = json.loads(line)\n"
                 "    s += r['groupID'] + len(r['name']['en']) + int(r.get('mass') or 0) + bool(r.get('published'))"),
    },
    "everything as Python objects": {
        "flatview": "x = flatview.open(f'{FB}/types.fb').root._asdict(defaults=False)['rows']",
        # the official library's only whole-tree conversion is the object API
        "official": "x = TTT.InitFromPackedBuf(open(f'{FB}/types.fb','rb').read(), 0).rows",
        "json": f"x = [json.loads(l) for l in {JSON_LINES}]",
    },
}

PRELUDE = """
import sys, time, tracemalloc, random, json, gc
sys.path.insert(0, {gen!r})
import flatview
from sde.types.TypesTable import TypesTable as TT, TypesTableT as TTT
FB, JL = {fb!r}, {jl!r}
_rows = flatview.open(f'{{FB}}/types.fb').rows
_keys = _rows.keys(); LAST = _keys[-1]; IDS = random.Random(1).sample(_keys, 1000)
del _rows, _keys; gc.collect()
TRACE
_t0 = time.perf_counter()
"""

POST = """
dt = time.perf_counter() - _t0
print(dt, tracemalloc.get_traced_memory()[1] if tracemalloc.is_tracing() else 0)
"""


def _exec(src: str) -> tuple[float, int]:
    res = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True)
    if res.returncode:
        raise SystemExit(res.stderr)
    t, heap = res.stdout.split()
    return float(t), int(heap)


def run(gen: str, code: str) -> tuple[float, int]:
    base = PRELUDE.format(gen=gen, fb=FB, jl=JL)
    # tracemalloc slows allocation-heavy code, so time without it
    timed = base.replace("TRACE", "") + code + POST
    best = min(_exec(timed)[0] for _ in range(2))
    traced = base.replace("TRACE", "tracemalloc.start()") + code + POST
    return best, _exec(traced)[1]


def main():
    gen = tempfile.mkdtemp(prefix="fb3way")
    subprocess.run([FLATC, "--python", "--gen-object-api", "-o", gen, f"{FB}/types.fbs"],
                   check=True, capture_output=True)
    print(f"types.fb {os.path.getsize(f'{FB}/types.fb') / 1e6:.0f} MB, "
          f"types.jsonl {os.path.getsize(f'{JL}/types.jsonl') / 1e6:.0f} MB (same SDE build)\n")
    impls = ["flatview", "official", "json"]
    print(f"{'scenario':46s}" + "".join(f"{i:>24s}" for i in impls))
    for name, codes in SCENARIOS.items():
        cells = []
        for impl in impls:
            t, heap = run(gen, codes[impl])
            cells.append(f"{t * 1000:9.1f} ms {heap / 2**20:6.1f} MB")
        print(f"{name:46s}" + "".join(f"{c:>24s}" for c in cells))
    print("\ncells: best wall time (incl. opening the file), peak Python heap")


if __name__ == "__main__":
    main()
