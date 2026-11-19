"""Differential fuzzing: flatview's verifier/decoder vs flatc's generated C++.

    uv run python fuzz/verifier_parity.py [--seconds 120] [--seed 1]

For each target schema, real buffers are mutated (structure-aware: offsets
nudged, vtable entries changed, truncation, splices) and every result is
checked by both implementations. Disagreements are bucketed and saved under
fuzz/.cache/findings/verifier/ with a JSON note.

flatview's verifier is deliberately stricter in three switchable ways (key
order, UTF-8, the max_refs amplification budget). The core comparison turns
them off; --strict-pass measures how often the extra checks fire on inputs
flatc accepts.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import struct
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import flatview  # noqa: E402
from oracle import CACHE, Oracle  # noqa: E402

KITCHEN = ROOT / "tests" / "data" / "kitchen" / "kitchen.fbs"
SDE_SCHEMAS = ROOT / "tests" / "data" / "sde-schemas"
SDE_DATA = ROOT / "tests" / "data" / "sde"
FINDINGS = CACHE / "findings" / "verifier"


# ---------------------------------------------------------------------------
# targets and seeds

def kitchen_seeds(schema: flatview.Schema) -> list[bytes]:
    bp = schema.encode({"typeID": 999, "runs": 7}, "Blueprint")
    ship = {
        "typeID": 587, "name": "Rifter", "race": "Minmatar", "mass": 1067000.0, "hp": 1200,
        "position": {"x": 1.0, "y": -2.5, "z": 3e10},
        "bounds": {"min": [0.0, 0.0, 0.0], "tag": 7, "counts": [1, 2, 3],
                   "pairs": [[-1, 2**40], [32767, -(2**63)]], "slot": "Rig"},
        "modules": [{"name": "Booster", "slot": "Mid", "cpu": 15.0, "flags": "Published Repackable"},
                    {"name": "Cannon", "slot": "High", "flags": "Published Dynamic"}],
        "tags": ["frigate", "t1", "ünïcödé"], "slots": ["High", "Mid", "Low"], "raw": [0, 1, 255],
        "points": [[1.0, 2.0, 3.0]], "primary_type": "Module", "primary": {"name": "AB"},
        "cargo_type": ["Charge", "Ammo", "Module"],
        "cargo": [{"typeID": 178, "volume": 0.0025}, {"typeID": 179}, {"name": "Spare"}],
        "blueprint": bp, "big": 2**64 - 1, "tiny": -128, "ratio": 0.5, "enabled": False, "maybe": True,
    }
    return [
        schema.encode({"name": "Fleet", "ships": [ship, {"typeID": 2, "name": "Drake"}],
                       "by_name": [{"name": "b"}, {"name": "a"}]}),
        schema.encode({"ships": [{"typeID": 1, "name": "x", "cargo_type": ["Module"],
                                  "cargo": [{"name": "m"}]}]}),
        schema.encode({"name": "empty"}),
        schema.encode({}),
    ]


def sde_seeds(table: str, schema: flatview.Schema) -> list[bytes]:
    fb = flatview.open(SDE_DATA / f"{table}.fb", schema)
    rows = fb.root.rows
    seeds = []
    raw = bytes(fb.data)
    if len(raw) <= 64_000:
        seeds.append(raw)                                   # CCP's Go-built bytes as-is
    for n in (1, 3, 8):
        seeds.append(schema.encode({"rows": [r._asdict(defaults=False) for r in rows[:n]]}))
    return seeds


def targets() -> list[tuple[str, Path, list[bytes]]]:
    out = []
    k = flatview.load_schema(KITCHEN)
    out.append(("kitchen", KITCHEN, kitchen_seeds(k)))
    for table in ("categories", "compressibleTypes", "masteries", "planetSchematics", "dogmaEffects",
                  "certificates"):
        fbs = SDE_SCHEMAS / f"{table}.fbs"
        out.append((table, fbs, sde_seeds(table, flatview.load_schema(fbs))))
    return out


# ---------------------------------------------------------------------------
# mutations

def mutate(data: bytes, rng: random.Random, corpus: list[bytes]) -> bytes:
    b = bytearray(data)
    for _ in range(rng.choice((1, 1, 1, 2, 3))):
        if not b:
            b = bytearray(rng.randbytes(rng.randrange(1, 16)))
        op = rng.randrange(9)
        n = len(b)
        if op == 0:                                         # bit flip
            i = rng.randrange(n)
            b[i] ^= 1 << rng.randrange(8)
        elif op == 1:                                       # random byte
            b[rng.randrange(n)] = rng.randrange(256)
        elif op in (2, 3) and n >= 4:                       # u32 at an aligned spot
            i = rng.randrange(n // 4) * 4
            cur = struct.unpack_from("<I", b, i)[0]
            new = rng.choice([0, 1, 2, 4, 8, n - i, n - i - 4, 0xFFFFFFFF, 0x7FFFFFFF, 0x80000000,
                              (cur + rng.randrange(-64, 65)) & 0xFFFFFFFF, rng.getrandbits(32),
                              (-cur) & 0xFFFFFFFF])
            struct.pack_into("<I", b, i, new & 0xFFFFFFFF)
        elif op == 4 and n >= 2:                            # u16 (vtable entries, lengths)
            i = rng.randrange(n // 2) * 2
            cur = struct.unpack_from("<H", b, i)[0]
            new = rng.choice([0, 1, 2, 3, 4, 6, 8, 0xFFFF, cur + 2, cur - 2, cur + 4, cur + 1,
                              rng.getrandbits(16)])
            struct.pack_into("<H", b, i, new & 0xFFFF)
        elif op == 5:                                       # truncate
            del b[rng.randrange(n):]
        elif op == 6:                                       # extend
            b += rng.randbytes(rng.randrange(1, 32))
        elif op == 7 and n >= 8:                            # copy a chunk within the buffer
            ln = rng.randrange(1, min(64, n))
            src, dst = rng.randrange(n - ln + 1), rng.randrange(n - ln + 1)
            b[dst:dst + ln] = b[src:src + ln]
        elif op == 8 and corpus:                            # splice from another seed
            other = rng.choice(corpus)
            if other:
                ln = rng.randrange(1, min(64, len(other)) + 1)
                src = rng.randrange(len(other) - ln + 1)
                dst = rng.randrange(max(1, n - ln + 1))
                b[dst:dst + ln] = other[src:src + ln]
    return bytes(b)


# ---------------------------------------------------------------------------
# comparison

_FLOAT_WORD = re.compile(r"(?<=[:\[,])(-?)(nan|inf|infinity)(?=[,\]}])")


def normalise(v):
    if isinstance(v, float):
        if math.isnan(v):
            return "nan"
        if math.isinf(v):
            return "inf" if v > 0 else "-inf"
        return v
    if isinstance(v, dict):
        return {k: normalise(x) for k, x in v.items()}
    if isinstance(v, list):
        return [normalise(x) for x in v]
    return v


def same(a, b) -> str | None:
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            return f"keys differ: {sorted(set(a) ^ set(b))}"
        for k in a:
            r = same(a[k], b[k])
            if r:
                return f".{k}{r if r.startswith(('.', '[')) else ': ' + r}"
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"length {len(a)} vs {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            r = same(x, y)
            if r:
                return f"[{i}]{r if r.startswith(('.', '[')) else ': ' + r}"
        return None
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) \
            and not isinstance(b, bool) and (isinstance(a, float) or isinstance(b, float)):
        if a == b or math.isclose(a, b, rel_tol=1e-11, abs_tol=1e-12):
            return None
        # flatc prints 12 decimals: tiny values collapse; huge floats print as e.g. 1e+38
        if abs(a - b) <= 5e-13 + 1e-11 * max(abs(a), abs(b)):
            return None
        # float32 fields are printed with 6 significant digits
        if isinstance(a, float) and (float(f"{a:.6f}") == b or float(f"{a:.6g}") == b):
            return None
        return f"{a!r} vs {b!r}"
    if a == b and type(a) is type(b):
        return None
    return f"{a!r} vs {b!r}"


def ours(schema, data: bytes, strict: bool):
    try:
        if strict:
            schema.verify(data)
        else:
            schema.verify(data, max_refs=10**9, check_sorted=False, check_utf8=False)
    except flatview.VerificationError as e:
        return False, str(e)
    except Exception as e:   # anything else from the verifier is itself a finding
        return None, f"{type(e).__name__}: {e}"
    return True, None


def bucket_of(message: str) -> str:
    msg = re.sub(r"\[\d+\]", "[i]", message)
    msg = re.sub(r"\d+", "N", msg)
    return msg.split(": ", 1)[-1][:90]


def save(kind: str, bucket: str, target: str, data: bytes, note: dict, counts: Counter) -> None:
    key = (kind, bucket)
    counts[key] += 1
    if counts[key] > 3:
        return
    d = FINDINGS / kind
    d.mkdir(parents=True, exist_ok=True)
    stem = f"{target}-{abs(hash(bucket)) % 10**8}-{counts[key]}"
    (d / f"{stem}.bin").write_bytes(data)
    (d / f"{stem}.json").write_text(json.dumps({"target": target, "bucket": bucket, **note}, indent=1,
                                               default=str))


def run(seconds: float, seed: int, strict_pass: bool) -> int:
    rng = random.Random(seed)
    tg = targets()
    counts: Counter = Counter()
    stats = defaultdict(Counter)
    deadline = time.time() + seconds
    oracles = {name: Oracle(fbs) for name, fbs, _ in tg}
    schemas = {name: flatview.load_schema(fbs) for name, fbs, _ in tg}
    corpus = [s for _, _, seeds in tg for s in seeds]
    try:
        for name, fbs, seeds in tg:            # seeds must agree outright
            for s in seeds:
                st, _ = oracles[name].check(s)
                ok, msg = ours(schemas[name], s, strict=True)
                if st != "OK" or not ok:
                    print(f"SEED DISAGREEMENT {name}: flatc={st} ours={ok} {msg}")
        i = 0
        while time.time() < deadline:
            name, fbs, seeds = tg[i % len(tg)]
            i += 1
            schema, oracle = schemas[name], oracles[name]
            data = mutate(rng.choice(seeds), rng, corpus)
            theirs, tjson = oracle.check(data)
            ok, msg = ours(schema, data, strict=False)
            st = stats[name]
            st["total"] += 1
            if theirs == "CRASH":                    # flatc verified it, then its printer aborted
                st["flatc-printer-crash"] += 1
                save("flatc-printer-crash", bucket_of(tjson), name, data, {"flatc": tjson, "ours": msg}, counts)
                continue
            if ok is None:
                st["ours-crashed"] += 1
                save("ours-crashed", bucket_of(msg), name, data, {"error": msg, "flatc": theirs}, counts)
                continue
            if ok and theirs == "FAIL":
                st["ours-accepts-flatc-rejects"] += 1
                save("ours-accepts-flatc-rejects", "accepted", name, data, {}, counts)
                continue
            if not ok and theirs != "FAIL":
                st["ours-rejects-flatc-accepts"] += 1
                save("ours-rejects-flatc-accepts", bucket_of(msg), name, data, {"error": msg}, counts)
                continue
            if not ok:
                st["both-reject"] += 1
                continue
            st["both-accept"] += 1
            if strict_pass:
                sok, smsg = ours(schema, data, strict=True)
                if not sok:
                    st["strict-extra-reject"] += 1
                    counts[("strict", bucket_of(smsg))] += 1
            try:
                # flatc's JSON includes deprecated fields
                mine = normalise(schema.read(data)._asdict(defaults=False, enums="name", deprecated=True))
            except Exception as e:
                if theirs == "OK":
                    st["decode: ours fails, flatc prints"] += 1
                    save("decode-ours-fails", bucket_of(f"{type(e).__name__}: {e}"), name, data,
                         {"error": f"{type(e).__name__}: {e}"}, counts)
                else:
                    st["decode: both fail"] += 1
                continue
            if theirs == "NOJSON":
                st["decode: flatc cannot print"] += 1
                counts[("flatc-nojson", tjson[:60])] += 1
                continue
            diff = same(mine, normalise(tjson))
            if diff:
                st["decode-differs"] += 1
                save("decode-differs", bucket_of(diff), name, data, {"diff": diff}, counts)
            else:
                st["decode-same"] += 1
    finally:
        for o in oracles.values():
            o.close()
    print(f"\n{'target':18s} " + " ".join(f"{k:>12s}" for k in ("total", "both-reject", "both-accept",
                                                                 "decode-same")))
    for name, st in stats.items():
        print(f"{name:18s} " + " ".join(f"{st[k]:12d}" for k in ("total", "both-reject", "both-accept",
                                                                 "decode-same")))
    problems = {k: v for k, v in counts.items()}
    print("\nfindings (kind, bucket): count")
    for (kind, bucket), n in sorted(problems.items(), key=lambda kv: -kv[1]):
        print(f"  {n:6d}  {kind:28s} {bucket}")
    if not problems:
        print("  none")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=120)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--strict-pass", action="store_true")
    args = ap.parse_args()
    return run(args.seconds, args.seed, args.strict_pass)


if __name__ == "__main__":
    sys.exit(main())
