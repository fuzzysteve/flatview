"""Benchmark flatview against the official `flatbuffers` package + flatc --python code.

    uv run --group bench python benchmarks/bench_vs_official.py [SDE_DIR]

SDE_DIR holds <table>.fb/.fbs (default: tests/data/sde-full, see
scripts/fetch_sde_examples.py). Official code is generated into a temp dir
with `flatc --python --gen-object-api`.
"""

import importlib
import os
import random
import subprocess
import sys
import tempfile
import time

import flatview

SDE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "tests", "data", "sde-full")


def best(fn, repeat=3):
    times = []
    for _ in range(repeat):
        t = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t)
    return min(times)


def gen_official(tmp, table):
    subprocess.run(["flatc", "--python", "--gen-object-api", "-o", tmp, os.path.join(SDE, table + ".fbs")],
                   check=True, capture_output=True)


def official_module(table, cls):
    return importlib.import_module(f"sde.{table}.{cls}")


def row(name, ours, theirs):
    ratio = theirs / ours if ours else float("inf")
    print(f"  {name:48s} flatview {ours*1000:9.1f} ms   official {theirs*1000:9.1f} ms   {ratio:6.1f}x")


def main():
    tmp = tempfile.mkdtemp(prefix="fbbench")
    for t in ("types", "typeDogma", "mapSolarSystems"):
        gen_official(tmp, t)
    sys.path.insert(0, tmp)

    # ---------------------------------------------------------------- types
    with open(os.path.join(SDE, "types.fb"), "rb") as fh:
        data = fh.read()
    print(f"types.fb ({len(data)/1e6:.0f} MB)")
    schema = flatview.load_schema(os.path.join(SDE, "types.bfbs"))
    TT = official_module("types", "TypesTable").TypesTable

    def ours_open():
        return schema.read(data)

    def theirs_open():
        return TT.GetRootAs(data, 0)

    row("open root", best(ours_open), best(theirs_open))

    rows = schema.read(data).rows
    troot = TT.GetRootAs(data, 0)

    def ours_scan():
        tot = 0
        for r in rows:
            if r.published:
                tot += r.groupID + len(r.name.en) + int(r.mass or 0)
        return tot

    def theirs_scan():
        tot = 0
        for i in range(troot.RowsLength()):
            r = troot.Rows(i)
            if r.Published():
                tot += r.GroupId() + len(r.Name().En().decode()) + int(r.Mass() or 0)
        return tot

    assert ours_scan() == theirs_scan()
    row("scan 53,941 rows, 4 fields each", best(ours_scan), best(theirs_scan))

    ids = random.Random(1).sample(rows.keys(), 1000)

    def ours_lookup():
        return [rows.lookup(i).name.en for i in ids]

    def theirs_lookup_linear():
        # The official Python API has no key lookup: scan for each id.
        wanted = set(ids)
        found = {}
        for i in range(troot.RowsLength()):
            r = troot.Rows(i)
            if r.TypeId() in wanted:
                found[r.TypeId()] = r.Name().En().decode()
        return [found[i] for i in ids]

    assert ours_lookup() == theirs_lookup_linear()
    row("1,000 lookups by typeID (official: one scan)", best(ours_lookup), best(theirs_lookup_linear))

    def ours_one():
        return rows.lookup(587).name.en

    def theirs_one():
        for i in range(troot.RowsLength()):
            r = troot.Rows(i)
            if r.TypeId() == 587:
                return r.Name().En().decode()

    row("single lookup typeID 587", best(ours_one, 20), best(theirs_one, 3))

    def ours_full():
        return schema.read(data)._asdict(defaults=False)

    T_obj = official_module("types", "TypesTable").TypesTableT

    def theirs_unpack():
        return T_obj.InitFromPackedBuf(data, 0)

    row("decode whole file (asdict vs object API UnPack)", best(ours_full, 1), best(theirs_unpack, 1))

    # ------------------------------------------------------------ typeDogma
    with open(os.path.join(SDE, "typeDogma.fb"), "rb") as fh:
        data = fh.read()
    print(f"typeDogma.fb ({len(data)/1e6:.0f} MB)")
    schema = flatview.load_schema(os.path.join(SDE, "typeDogma.bfbs"))
    DT = official_module("typeDogma", "TypeDogmaTable").TypeDogmaTable
    drows = schema.read(data).rows
    droot = DT.GetRootAs(data, 0)

    def ours_attrs():
        tot = 0.0
        for r in drows:
            for a in r.dogmaAttributes:
                if a.attributeID == 9:
                    tot += a.value
        return tot

    def theirs_attrs():
        tot = 0.0
        for i in range(droot.RowsLength()):
            r = droot.Rows(i)
            for j in range(r.DogmaAttributesLength()):
                a = r.DogmaAttributes(j)
                if a.AttributeId() == 9:
                    tot += a.Value()
        return tot

    assert ours_attrs() == theirs_attrs()
    row("sum attribute 9 (hp) over all ~1M attributes", best(ours_attrs, 2), best(theirs_attrs, 2))

    # ------------------------------------------------------- mapSolarSystems
    with open(os.path.join(SDE, "mapSolarSystems.fb"), "rb") as fh:
        data = fh.read()
    print(f"mapSolarSystems.fb ({len(data)/1e6:.1f} MB)")
    schema = flatview.load_schema(os.path.join(SDE, "mapSolarSystems.bfbs"))
    MT = official_module("mapSolarSystems", "MapSolarSystemsTable").MapSolarSystemsTable
    mrows = schema.read(data).rows
    mroot = MT.GetRootAs(data, 0)

    def ours_planets():
        return sum(len(r.planetIDs.tolist()) for r in mrows)

    def theirs_planets():
        n = 0
        for i in range(mroot.RowsLength()):
            r = mroot.Rows(i)
            n += len([r.PlanetIds(j) for j in range(r.PlanetIdsLength())])
        return n

    def theirs_planets_np():
        n = 0
        for i in range(mroot.RowsLength()):
            n += len(mroot.Rows(i).PlanetIdsAsNumpy())
        return n

    assert ours_planets() == theirs_planets()
    row("all planetIDs vectors to lists", best(ours_planets), best(theirs_planets))
    try:
        import numpy  # noqa: F401
        row("  ... vs official AsNumpy", best(ours_planets), best(theirs_planets_np))
    except ImportError:
        pass


if __name__ == "__main__":
    main()
