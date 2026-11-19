"""Download EVE SDE FlatBuffer files (.fb + .fbs + .bfbs) for tests/benchmarks.

    uv run python scripts/fetch_sde_examples.py                 # a useful default set
    uv run python scripts/fetch_sde_examples.py --all           # every table (~1 GB unpacked)
    uv run python scripts/fetch_sde_examples.py types groups    # specific tables

Files land in tests/data/sde-full/ (gitignored), decompressed. CCP serves
them as raw gzip bodies under their plain names; flatview reads either form.
"""

import argparse
import concurrent.futures
import gzip
import json
import os
import sys
import urllib.request

INDEX_URL = "https://sde.evetech.net/tranquility/latest/"
DEFAULT = ["_sde", "categories", "groups", "types", "typeDogma", "typeMaterials", "blueprints",
           "compressibleTypes", "dogmaAttributes", "dogmaEffects", "mapSolarSystems", "marketGroups",
           "planetSchematics", "certificates", "masteries", "skins", "npcCorporations"]
HEADERS = {"User-Agent": "flatview-sde-examples/0.1"}
OUT = os.path.join(os.path.dirname(__file__), "..", "tests", "data", "sde-full")


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=120) as r:
        data = r.read()
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tables", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    index = json.loads(fetch(INDEX_URL))
    tables = index["tables"]
    wanted = sorted(tables) if args.all else (args.tables or DEFAULT)
    missing = [t for t in wanted if t not in tables]
    if missing:
        print(f"unknown tables: {missing}", file=sys.stderr)
        return 2
    os.makedirs(args.out, exist_ok=True)
    print(f"release {index['release']} build {index['buildNumber']}: {len(wanted)} tables -> {args.out}")

    jobs = []
    for t in wanted:
        fbi = tables[t]["flatbuffers"]
        jobs += [(fbi["binaryUrl"], f"{t}.fb"), (fbi["schemaUrl"], f"{t}.fbs"),
                 (fbi["binarySchemaUrl"], f"{t}.bfbs")]

    def get(job):
        url, name = job
        data = fetch(url)
        with open(os.path.join(args.out, name), "wb") as fh:
            fh.write(data)
        return name, len(data)

    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        for name, size in pool.map(get, jobs):
            if name.endswith(".fb"):
                print(f"  {name:32s} {size / 1e6:8.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
