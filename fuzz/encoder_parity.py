"""Encoder fuzzing: random schemas and values, cross-checked with flatc.

    uv run python fuzz/encoder_parity.py [--seconds 300] [--seed 1] [--values 30]

Each round generates a random valid schema, builds flatc's C++ oracle for it
(see oracle.py), then for random values:

  A. flatview encode -> flatview verify -> decode == the expected value
  B. flatview's buffer passes flatc's generated verifier, and flatc's JSON
     printer agrees with flatview's decoding
  C. flatc -b encodes the same value from JSON -> flatview verifies it and
     decodes it to the expected value

"Expected" is the input value after the schema's rules: defaults filled in,
float32 rounding, keyed vectors sorted by key. Findings are saved under
fuzz/.cache/findings/encoder/ with the schema and the value.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import flatview  # noqa: E402
from flatview import BaseType  # noqa: E402
from oracle import CACHE, FLATC, Oracle  # noqa: E402
from verifier_parity import normalise, same  # noqa: E402

FINDINGS = CACHE / "findings" / "encoder"

INT_TYPES = {"byte": (-128, 127), "ubyte": (0, 255), "short": (-32768, 32767), "ushort": (0, 65535),
             "int": (-2**31, 2**31 - 1), "uint": (0, 2**32 - 1), "long": (-2**63, 2**63 - 1),
             "ulong": (0, 2**64 - 1)}
UNSIGNED = ("ubyte", "ushort", "uint", "ulong")
FLOAT_TYPES = ("float", "double")
STRINGS = ["", "a", "hello world", "é", "中文", "😀 emoji", "tab\there", "nl\nx", "q\"uote", "back\\slash",
           "ctl\x01\x1f", "ünïcödé", "x" * 40]
FLOAT32_MAX = 3.4028234663852886e38


# ---------------------------------------------------------------------------
# random schemas

class Gen:
    def __init__(self, rng: random.Random):
        self.rng = rng
        self.n = 0

    def name(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}{self.n}"

    def schema(self) -> str:
        r = self.rng
        out = []
        ns = "fz" if r.random() < 0.3 else ""
        if ns:
            out.append(f"namespace {ns};")
        self.enums: list[tuple[str, str, list[int], bool]] = []
        for _ in range(r.randrange(0, 3)):
            ename = self.name("E")
            flags = r.random() < 0.3
            if flags:
                under = r.choice(UNSIGNED[:3])
                nbits = {"ubyte": 8, "ushort": 16, "uint": 32}[under]
                pos = sorted(r.sample(range(nbits), r.randrange(1, min(5, nbits))))
                body = ", ".join(f"F{p} = {p}" for p in pos)
                values = [1 << p for p in pos]
                out.append(f"enum {ename} : {under} (bit_flags) {{ {body} }}")
            else:
                under = r.choice(list(INT_TYPES))
                lo, hi = INT_TYPES[under]
                vals = {0}
                while len(vals) < r.randrange(2, 6):
                    vals.add(r.choice([r.randrange(max(lo, -50), min(hi, 50) + 1), lo, hi]))
                values = sorted(vals)
                out.append(f"enum {ename} : {under} {{ {', '.join(f'V{i} = {v}' for i, v in enumerate(values))} }}")
            self.enums.append((ename, under, values, flags))
        self.structs: list[tuple[str, list[tuple[str, str]]]] = []
        for _ in range(r.randrange(0, 3)):
            sname = self.name("S")
            fields = []
            for i in range(r.randrange(1, 5)):
                fields.append((f"s{i}", self.struct_field_type()))
            self.structs.append((sname, fields))
            out.append(f"struct {sname} {{ " + " ".join(f"{n}: {t};" for n, t in fields) + " }")
        tnames = [self.name("T") for _ in range(r.randrange(1, 4))]
        self.unions: list[tuple[str, list[str]]] = []
        if r.random() < 0.5:
            uname = self.name("U")
            members = r.sample(tnames, r.randrange(1, len(tnames) + 1))
            self.unions.append((uname, members))
            out.append(f"union {uname} {{ {', '.join(members)} }}")
        self.tables: dict[str, list[tuple[str, str, str]]] = {}
        for tname in tnames:
            fields = []
            used_key = False
            for i in range(r.randrange(0, 7)):
                fname, ftype, suffix = f"f{i}", *self.table_field_type(tnames)
                if not used_key and r.random() < 0.25 and (ftype in INT_TYPES or ftype in FLOAT_TYPES
                                                          or ftype == "string") and "null" not in suffix:
                    suffix = (suffix + " (key)") if "(" not in suffix else suffix
                    used_key = True
                fields.append((fname, ftype, suffix))
            self.tables[tname] = fields
            out.append(f"table {tname} {{ " + " ".join(f"{n}: {t}{s};" for n, t, s in fields) + " }")
        self.root = tnames[0]
        out.append(f"root_type {self.root};")
        return "\n".join(out) + "\n"

    def scalar_name(self) -> str:
        return self.rng.choice(list(INT_TYPES) + list(FLOAT_TYPES) + ["bool"])

    def struct_field_type(self) -> str:
        r = self.rng
        k = r.random()
        if k < 0.15 and self.structs:
            return r.choice(self.structs)[0]
        if k < 0.3 and self.enums:
            return r.choice(self.enums)[0]
        if k < 0.45:
            inner = r.choice([self.scalar_name()] + [s[0] for s in self.structs])
            return f"[{inner}:{r.randrange(1, 4)}]"
        return self.scalar_name()

    def table_field_type(self, tnames: list[str]) -> tuple[str, str]:
        r = self.rng
        k = r.random()
        if k < 0.3:
            t = self.scalar_name()
            if r.random() < 0.25:
                return t, " = null"
            if r.random() < 0.3:
                return t, f" = {self.default_literal(t)}"
            if r.random() < 0.08:
                return t, " (deprecated)"
            return t, ""
        if k < 0.4 and self.enums:
            ename, _, values, flags = r.choice(self.enums)
            return ename, ""
        if k < 0.5:
            return "string", ""
        if k < 0.58 and self.structs:
            return r.choice(self.structs)[0], ""
        if k < 0.66:
            return r.choice(tnames), ""
        if k < 0.74 and self.unions:
            return self.unions[0][0], ""
        inner = r.choice([self.scalar_name(), "string"] + [s[0] for s in self.structs] + tnames +
                         [e[0] for e in self.enums])
        return f"[{inner}]", ""

    def default_literal(self, t: str) -> str:
        r = self.rng
        if t == "bool":
            return r.choice(["true", "false"])
        if t in FLOAT_TYPES:
            return r.choice(["1.5", "-2.25", "0.0", "100.0", "inf", "-inf"])
        lo, hi = INT_TYPES[t]
        return str(r.choice([lo, hi, 1, 7]))


# ---------------------------------------------------------------------------
# random values

class Values:
    def __init__(self, schema: flatview.Schema, rng: random.Random):
        self.s = schema
        self.rng = rng

    def scalar(self, bt: BaseType, enum, key: bool = False):
        r = self.rng
        if enum is not None:
            if enum.bit_flags:
                v = 0
                for ev in enum.values:
                    if r.random() < 0.5:
                        v |= ev.value
                return v
            return r.choice([v.value for v in enum.values])
        if bt == BaseType.BOOL:
            return r.random() < 0.5
        if bt.is_float:
            pool = [0.0, -0.0, 1.5, -1e-7, 123456.789, r.uniform(-1e6, 1e6)]
            if bt == BaseType.DOUBLE:
                pool += [1e300, -1e-300, 5e-324]
            else:
                pool += [FLOAT32_MAX, 1e-38]
            if not key:
                pool += [math.inf, -math.inf, math.nan]
            return r.choice(pool)
        lo, hi = flatview._schema.INT_RANGE[bt]
        return r.choice([lo, hi, 0, 1, -1 if lo < 0 else 2, r.randrange(lo, hi + 1)])

    def table(self, obj, depth: int) -> dict:
        r = self.rng
        out = {}
        for f in obj.fields:
            if f.deprecated or f.type.base == BaseType.UTYPE:
                continue
            if f.required or r.random() < 0.6:
                v = self.field(obj, f, depth)
                if v is _SKIP:
                    continue
                if isinstance(v, tuple) and v and v[0] is _UNION:
                    out[f.name + "_type"], out[f.name] = v[1], v[2]
                else:
                    out[f.name] = v
        return out

    def field(self, obj, f, depth):
        r = self.rng
        t = f.type
        base = t.base
        if base.is_scalar:
            return self.scalar(base, t.enum, key=f.key)
        if base == BaseType.STRING:
            return r.choice(STRINGS)
        if base == BaseType.OBJ:
            if t.ref.is_struct:
                return self.struct(t.ref)
            if depth >= 3:
                return _SKIP
            return self.table(t.ref, depth + 1)
        if base == BaseType.UNION:
            members = [v for v in t.ref.values if v.value != 0]
            if not members or depth >= 3:
                return _SKIP
            m = r.choice(members)
            return (_UNION, m.value, self.table(m.union_type.ref, depth + 1))
        if base == BaseType.VECTOR:
            n = r.choice([0, 1, 2, 3, 5])
            el = t.element
            if el == BaseType.UNION:
                return _SKIP
            if el.is_scalar:
                return [self.scalar(el, t.enum) for _ in range(n)]
            if el == BaseType.STRING:
                return [r.choice(STRINGS) for _ in range(n)]
            if t.ref.is_struct:
                return [self.struct(t.ref) for _ in range(n)]
            if depth >= 3:
                return _SKIP
            rows = [self.table(t.ref, depth + 1) for _ in range(n)]
            kf = t.ref.key_field
            if kf is not None and kf.type.base.is_float:
                for row in rows:     # NaN keys are refused by design
                    if isinstance(row.get(kf.name), float) and math.isnan(row[kf.name]):
                        row[kf.name] = 0.0
            return rows
        return _SKIP

    def struct(self, obj) -> dict:
        out = {}
        for f in obj.fields:
            t = f.type
            if t.base == BaseType.ARRAY:
                if t.element == BaseType.OBJ:
                    out[f.name] = [self.struct(t.ref) for _ in range(t.fixed_length)]
                else:
                    out[f.name] = [self.scalar(t.element, t.enum) for _ in range(t.fixed_length)]
            elif t.base == BaseType.OBJ:
                out[f.name] = self.struct(t.ref)
            else:
                out[f.name] = self.scalar(t.base, t.enum)
        return out


_SKIP = object()
_UNION = object()


# ---------------------------------------------------------------------------
# what a correct round trip must produce

def f32(v: float) -> float:
    if math.isnan(v) or math.isinf(v):
        return v
    return struct.unpack("<f", struct.pack("<f", v))[0]


def expect_scalar(bt: BaseType, v):
    if bt == BaseType.FLOAT:
        return f32(v)
    if bt == BaseType.BOOL:
        return bool(v)
    return v


def expect_struct(obj, v: dict) -> dict:
    out = {}
    for f in obj.fields:
        t = f.type
        x = v[f.name]
        if t.base == BaseType.ARRAY:
            out[f.name] = [expect_struct(t.ref, e) if t.element == BaseType.OBJ else expect_scalar(t.element, e)
                           for e in x]
        elif t.base == BaseType.OBJ:
            out[f.name] = expect_struct(t.ref, x)
        else:
            out[f.name] = expect_scalar(t.base, x)
    return out


def key_of(obj, row: dict):
    kf = obj.key_field
    k = row.get(kf.name)
    if kf.type.base == BaseType.STRING:
        return (k or "").encode("utf-8")
    return kf.default if k is None else k


def expect_table(obj, v: dict) -> dict:
    out = {}
    for f in obj.fields:
        if f.deprecated:
            continue
        t = f.type
        base = t.base
        x = v.get(f.name)
        if x is None:
            if base.is_scalar:
                out[f.name] = None if f.optional else expect_scalar(base, f.default)
            else:
                out[f.name] = None
            continue
        if base.is_scalar:
            out[f.name] = expect_scalar(base, x)
        elif base == BaseType.STRING:
            out[f.name] = x
        elif base == BaseType.OBJ:
            out[f.name] = expect_struct(t.ref, x) if t.ref.is_struct else expect_table(t.ref, x)
        elif base == BaseType.UNION:
            member = t.ref.by_value[v[f.name + "_type"]]
            out[f.name] = expect_table(member.union_type.ref, x)
        elif base == BaseType.VECTOR:
            el = t.element
            if el.is_scalar:
                out[f.name] = [expect_scalar(el, e) for e in x]
            elif el == BaseType.STRING:
                out[f.name] = list(x)
            elif t.ref.is_struct:
                out[f.name] = [expect_struct(t.ref, e) for e in x]
            else:
                rows = x
                if t.ref.key_field is not None:
                    rows = sorted(rows, key=lambda row: key_of(t.ref, row))
                out[f.name] = [expect_table(t.ref, e) for e in rows]
    return out


def _unsign_zero(v):
    if isinstance(v, float) and v == 0.0:
        return 0.0
    if isinstance(v, dict):
        return {k: _unsign_zero(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_unsign_zero(x) for x in v]
    return v


def _sort_key(k):
    if isinstance(k, (int, float)) and not isinstance(k, bool):
        return (0, float(k) if not isinstance(k, int) else k, "")
    return (1, 0, repr(k))


def canon(obj, v):
    """Order-insensitive form for keyed vectors: FlatBuffers leaves the order
    of elements with *equal* keys unspecified (flatc's sort doesn't keep input
    order; ours does), so sort each keyed vector by (key, content)."""
    if not isinstance(v, dict):
        return v
    out = dict(v)
    for f in obj.fields:
        x = out.get(f.name)
        t = f.type
        if x is None:
            continue
        if t.base == BaseType.OBJ and not t.ref.is_struct:
            out[f.name] = canon(t.ref, x)
        elif t.base == BaseType.UNION:
            tv = out.get(f.name + "_type")
            member = t.ref.by_value.get(tv)
            if member is not None and member.union_type is not None and member.union_type.ref is not None:
                out[f.name] = canon(member.union_type.ref, x)
        elif t.base == BaseType.VECTOR and t.element == BaseType.OBJ and not t.ref.is_struct:
            rows = [canon(t.ref, e) for e in x]
            if t.ref.key_field is not None:
                kf = t.ref.key_field.name
                # numeric keys compare numerically (so -0.0 groups with 0.0); the
                # tie-break ignores the sign of zero too: flatc's encoder treats
                # -0.0 as equal to a 0.0 default and omits it, losing the sign
                rows.sort(key=lambda e: (_sort_key(e.get(kf)),
                                         json.dumps(_unsign_zero(e), sort_keys=True, default=str)))
            out[f.name] = rows
    return out


# ---------------------------------------------------------------------------
# flatc helpers

_JSON_NONFINITE = re.compile(r'(?<=[:\[,\s])(-?)(NaN|Infinity)(?=\s*[,\]}])')


def to_flatc_json(value) -> str:
    text = json.dumps(value, ensure_ascii=True)
    # flatc spells non-finite floats as nan / inf / -inf
    return _JSON_NONFINITE.sub(lambda m: m.group(1) + ("nan" if m.group(2) == "NaN" else "inf"), text)


def flatc_encode(fbs: Path, value, tmp: Path) -> tuple[bytes | None, str]:
    j = tmp / "value.json"
    j.write_text(to_flatc_json(value))
    out = tmp / "value.bin"
    if out.exists():
        out.unlink()
    r = subprocess.run([FLATC, "-b", "-o", str(tmp), str(fbs), str(j)], capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        msg = (r.stderr or r.stdout).strip().splitlines()
        return None, (msg[-1] if msg else f"exit {r.returncode}")
    return out.read_bytes(), ""


# ---------------------------------------------------------------------------

def bucket(msg: str) -> str:
    msg = re.sub(r"\[\d+\]", "[i]", msg)
    msg = re.sub(r"\b\d+(\.\d+)?(e[-+]?\d+)?\b", "N", msg)
    msg = re.sub(r"\bf\d+\b|\b[TSEU]\d+\b", "X", msg)
    return msg[:100]


def run(seconds: float, seed: int, per_schema: int) -> int:
    rng = random.Random(seed)
    tmp = Path(tempfile.mkdtemp(prefix="encfuzz"))
    counts: Counter = Counter()
    stats: Counter = Counter()
    examples: dict = {}
    deadline = time.time() + seconds
    FINDINGS.mkdir(parents=True, exist_ok=True)

    def record(kind, msg, text, value):
        key = (kind, bucket(msg))
        counts[key] += 1
        value_json = to_flatc_json(value)          # flatc-style JSON (nan/inf spelled out)
        size = len(text) + len(value_json)
        if key not in examples or size < examples[key][0]:   # keep the smallest example
            examples[key] = (size, {"kind": kind, "message": msg, "schema": text, "value": value_json})

    try:
        while time.time() < deadline:
            gen = Gen(rng)
            text = gen.schema()
            d = tmp / f"s{stats['schemas']}"
            d.mkdir()
            fbs = d / "s.fbs"
            fbs.write_text(text)
            chk = subprocess.run([FLATC, "--schema", "-b", "-o", str(d), str(fbs)], capture_output=True, text=True)
            if chk.returncode != 0:
                stats["schemas-flatc-rejected"] += 1      # generator produced something flatc refuses
                continue
            try:
                schema = flatview.load_schema(fbs)
            except flatview.SchemaError as e:
                record("ours-rejects-valid-schema", str(e), text, {})
                continue
            stats["schemas"] += 1
            try:
                oracle = Oracle(fbs)
            except subprocess.CalledProcessError as e:
                stats["oracle-build-failed"] += 1
                continue
            vg = Values(schema, rng)
            root = schema.root
            try:
                for _ in range(per_schema):
                    value = vg.table(root, 0)
                    stats["values"] += 1
                    expected = canon(root, normalise(expect_table(root, value)))
                    # A. flatview round trip
                    try:
                        buf = schema.encode(value)
                    except flatview.EncodeError as e:
                        record("ours-encode-error", str(e), text, value)
                        continue
                    try:
                        schema.verify(buf)
                    except flatview.VerificationError as e:
                        record("ours-output-fails-our-verify", str(e), text, value)
                        continue
                    got = canon(root, normalise(schema.read(buf)._asdict(defaults=True, enums="value")))
                    diff = same(got, expected)
                    if diff:
                        record("ours-roundtrip-differs", diff, text, value)
                    # B. flatc on flatview's buffer
                    st, tj = oracle.check(buf)
                    if st == "FAIL":
                        record("flatc-rejects-our-buffer", "verifier", text, value)
                    elif st == "OK":
                        mine = normalise(schema.read(buf)._asdict(defaults=False, enums="name", deprecated=True))
                        diff = same(mine, normalise(tj))
                        if diff:
                            record("flatc-json-differs", diff, text, value)
                        else:
                            stats["flatc-agrees"] += 1
                    elif st == "CRASH":
                        record("flatc-crashed", tj, text, value)
                    # C. flatc's encoder, read by flatview
                    theirs, err = flatc_encode(fbs, value, d)
                    if theirs is None:
                        record("flatc-cannot-encode", err, text, value)
                        continue
                    try:
                        schema.verify(theirs)
                    except flatview.VerificationError as e:
                        record("ours-rejects-flatc-buffer", str(e), text, value)
                        continue
                    got = canon(root, normalise(schema.read(theirs)._asdict(defaults=True, enums="value")))
                    diff = same(got, expected)
                    if diff:
                        record("flatc-buffer-decodes-differently", diff, text, value)
                    else:
                        stats["all-three-agree"] += 1
            finally:
                oracle.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    (FINDINGS / "findings.json").write_text(json.dumps(
        [{"count": counts[k], **examples[k][1]} for k in sorted(counts, key=lambda k: -counts[k])], indent=1))
    print(f"schemas: {stats['schemas']} (+{stats['schemas-flatc-rejected']} generated but refused by flatc)  "
          f"values: {stats['values']}  flatc agrees: {stats['flatc-agrees']}  all three agree: "
          f"{stats['all-three-agree']}")
    print("\nfindings (kind, bucket): count   [examples in fuzz/.cache/findings/encoder/findings.json]")
    for (kind, b), n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:6d}  {kind:32s} {b}")
    if not counts:
        print("  none")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=300)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--values", type=int, default=30, help="values per random schema")
    args = ap.parse_args()
    return run(args.seconds, args.seed, args.values)


if __name__ == "__main__":
    sys.exit(main())
