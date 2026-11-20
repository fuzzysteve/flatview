"""Peak memory: flatview vs the official flatbuffers package (+ flatc --python code).

    uv run --group bench python benchmarks/bench_memory.py [SDE_DIR]

Each scenario runs in a fresh subprocess. Reports
  * heap: peak Python allocations (tracemalloc) - what your process owns;
  * rss:  peak resident set (VmHWM) minus the post-import baseline. For
          flatview this includes mmapped file pages, which are clean, shared
          page cache the OS can drop at any time.
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

SDE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "tests", "data", "sde-full")

SCENARIOS = {
    "open types.fb": {
        "flatview": "fb = flatview.open(f'{SDE}/types.fb'); rows = fb.rows; n = len(rows)",
        "official": "data = open(f'{SDE}/types.fb','rb').read(); t = TT.GetRootAs(data, 0); n = t.RowsLength()",
    },
    "scan all rows, 4 fields": {
        "flatview": ("fb = flatview.open(f'{SDE}/types.fb'); s = 0\n"
                     "for r in fb.rows:\n    s += r.groupID + len(r.name.en) + int(r.mass or 0) + bool(r.published)"),
        "official": ("data = open(f'{SDE}/types.fb','rb').read(); t = TT.GetRootAs(data, 0); s = 0\n"
                     "for i in range(t.RowsLength()):\n    r = t.Rows(i)\n"
                     "    s += r.GroupId() + len(r.Name().En().decode()) + int(r.Mass() or 0) + bool(r.Published())"),
    },
    "keep 53,941 row views in a list": {
        "flatview": "fb = flatview.open(f'{SDE}/types.fb'); keep = list(fb.rows)",
        "official": ("data = open(f'{SDE}/types.fb','rb').read(); t = TT.GetRootAs(data, 0)\n"
                     "keep = [t.Rows(i) for i in range(t.RowsLength())]"),
    },
    "1,000 lookups by typeID": {
        "flatview": ("fb = flatview.open(f'{SDE}/types.fb'); rows = fb.rows\n"
                     "names = [rows.lookup(i).name.en for i in IDS]"),
        "official": ("data = open(f'{SDE}/types.fb','rb').read(); t = TT.GetRootAs(data, 0); w = set(IDS); f = {}\n"
                     "for i in range(t.RowsLength()):\n    r = t.Rows(i)\n"
                     "    if r.TypeId() in w: f[r.TypeId()] = r.Name().En().decode()\n"
                     "names = [f[i] for i in IDS]"),
    },
    "decode whole file to Python objects": {
        "flatview": "fb = flatview.open(f'{SDE}/types.fb'); d = fb.root._asdict(defaults=False)",
        "official": "data = open(f'{SDE}/types.fb','rb').read(); d = TTT.InitFromPackedBuf(data, 0)",
    },
}

PRELUDE = """
import sys, tracemalloc, random
sys.path.insert(0, {gen!r})
import flatview
from sde.types.TypesTable import TypesTable as TT, TypesTableT as TTT
SDE = {sde!r}
_s = flatview.load_schema(f'{{SDE}}/types.bfbs'); _r = _s.read(open(f'{{SDE}}/types.fb','rb').read()).rows
IDS = random.Random(1).sample(_r.keys(), 1000); del _s, _r
import gc; gc.collect()
def hwm():
    for line in open('/proc/self/status'):
        if line.startswith('VmHWM'): return int(line.split()[1]) * 1024
def rss():
    for line in open('/proc/self/status'):
        if line.startswith('VmRSS'): return int(line.split()[1]) * 1024
"""

RUN = """
base = rss()
tracemalloc.start()
{code}
heap = tracemalloc.get_traced_memory()[1]
tracemalloc.stop()
print(heap, hwm() - base)
"""


def main():
    gen = tempfile.mkdtemp(prefix="fbmem")
    subprocess.run([FLATC, "--python", "--gen-object-api", "-o", gen, f"{SDE}/types.fbs"],
                   check=True, capture_output=True)
    print(f"{'scenario':38s} {'flatview heap':>14s} {'official heap':>14s} {'flatview rss':>13s} {'official rss':>13s}")
    for name, impls in SCENARIOS.items():
        out = {}
        for impl, code in impls.items():
            # The prelude loads the file once to pick IDs; reset the HWM so only the scenario counts.
            src = PRELUDE.format(gen=gen, sde=SDE) + "open('/proc/self/clear_refs','w').write('5')\n" + RUN.format(code=code)
            res = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True)
            if res.returncode:
                raise SystemExit(res.stderr)
            heap, rss = map(int, res.stdout.split())
            out[impl] = (heap, rss)
        mb = lambda b: f"{b / 2**20:9.1f} MB"
        print(f"{name:38s} {mb(out['flatview'][0]):>14s} {mb(out['official'][0]):>14s} "
              f"{mb(out['flatview'][1]):>13s} {mb(out['official'][1]):>13s}")


if __name__ == "__main__":
    main()
