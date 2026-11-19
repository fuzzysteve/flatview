"""Download EVE SDE FlatBuffer files (.fb + .fbs + .bfbs) for tests/benchmarks.

    uv run python scripts/fetch_sde_examples.py                 # a useful default set
    uv run python scripts/fetch_sde_examples.py --all           # every table (~1 GB unpacked)
    uv run python scripts/fetch_sde_examples.py types groups    # specific tables

Files land in tests/data/sde-full/ (gitignored), decompressed. CCP serves
them as raw gzip bodies under their plain names; flatview reads either form.
"""

import argparse
import concurrent.futures
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from flatview._io import _decompress   # gzip with a size cap

INDEX_URL = "https://sde.evetech.net/tranquility/latest/"
DEFAULT = ["_sde", "categories", "groups", "types", "typeDogma", "typeMaterials", "blueprints",
           "compressibleTypes", "dogmaAttributes", "dogmaEffects", "mapSolarSystems", "marketGroups",
           "planetSchematics", "certificates", "masteries", "skins", "npcCorporations"]
HEADERS = {"User-Agent": "flatview-sde-examples/0.1"}
SAFE_NAME = re.compile(r"[A-Za-z0-9_]+")   # table names become file names
ALLOWED_HOST = "sde.evetech.net"
MAX_DOWNLOAD = 1 << 30                       # largest compressed file is ~30 MB


class _SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to https://sde.evetech.net/."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urllib.parse.urlsplit(newurl)
        if u.scheme != "https" or u.hostname != ALLOWED_HOST:
            raise urllib.error.HTTPError(newurl, code, f"refusing redirect to {newurl}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SameHostRedirects)
OUT = os.path.join(os.path.dirname(__file__), "..", "tests", "data", "sde-full")


def fetch(url: str) -> bytes:
    with _OPENER.open(urllib.request.Request(url, headers=HEADERS), timeout=120) as r:
        data = r.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise OSError(f"{url}: response larger than {MAX_DOWNLOAD:,} bytes")
    return _decompress(data, url)


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
    for t in wanted:
        if not SAFE_NAME.fullmatch(t):
            print(f"refusing unsafe table name from index: {t!r}", file=sys.stderr)
            return 2
    os.makedirs(args.out, exist_ok=True)
    print(f"release {index['release']} build {index['buildNumber']}: {len(wanted)} tables -> {args.out}")

    jobs = []
    for t in wanted:
        fbi = tables[t]["flatbuffers"]
        for url, name in ((fbi["binaryUrl"], f"{t}.fb"), (fbi["schemaUrl"], f"{t}.fbs"),
                          (fbi["binarySchemaUrl"], f"{t}.bfbs")):
            u = urllib.parse.urlsplit(url)
            if u.scheme != "https" or u.hostname != ALLOWED_HOST:
                print(f"refusing URL outside https://{ALLOWED_HOST}/: {url!r}", file=sys.stderr)
                return 2
            jobs.append((url, name))

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
