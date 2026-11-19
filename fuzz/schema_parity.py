"""Differential fuzzing of the .fbs parser against flatc.

    uv run python fuzz/schema_parity.py [--seconds 180] [--seed 1]

Schemas (real ones plus a hand-written edge-case corpus) are mutated at the
token level. Each variant is compiled by `flatc --schema -b` and parsed by
flatview. Findings, bucketed and saved under fuzz/.cache/findings/schema/:

  ours-accepts-flatc-rejects   flatview parses something flatc refuses
  ours-rejects-flatc-accepts   flatview refuses something flatc compiles
  both-accept-differ           both parse it, but to different schemas
  bfbs-load-fails              flatview cannot load flatc's .bfbs output
  ours-crashed                 flatview raised something other than SchemaError
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tests"))

import flatview  # noqa: E402
from conftest import normalise  # noqa: E402
from oracle import CACHE, FLATC  # noqa: E402

FINDINGS = CACHE / "findings" / "schema"
KITCHEN_DIR = ROOT / "tests" / "data" / "kitchen"
SDE_SCHEMAS = ROOT / "tests" / "data" / "sde-schemas"

# Hand-written edge cases: grammar corners in both directions.
CORPUS = [
    "table T { a: int; } root_type T;",
    "namespace a.b; table T { a: int; } root_type T;",
    "namespace a; table A { v: int; } namespace b; table B { a: a.A; } root_type B;",
    "enum E : byte { A, B = 5, C } table T { e: E = C; } root_type T;",
    "enum E : byte { A = 1, B } table T { e: E; } root_type T;",
    "enum E : byte { A = 1, B } table T { e: E = A; } root_type T;",
    "enum E : ubyte (bit_flags) { A, B, C } table T { e: E = \"A B\"; } root_type T;",
    "enum E : ubyte (bit_flags) { A, B = 7 } table T { e: E; } root_type T;",
    "enum E : ubyte (bit_flags) { A, B = 8 } table T { e: E; } root_type T;",
    "enum E : int { A = -5, B, C = 10, D = 3 } table T { e: E; } root_type T;",
    "enum E : uint { A = 4294967295 } table T { e: E; } root_type T;",
    "union U { A, B } table A { a: int; } table B { b: int; } table T { u: U; } root_type T;",
    "table A { a: int; } union U { A, A2: A } table T { u: U; } root_type T;",
    "table A { a: int; } union U { A = 3, B: A = 7 } table T { u: U; } root_type T;",
    "table A { a: int; } union U { A } table T { u: U (id: 1); x: int (id: 0); } root_type T;",
    "table A { a: int; } union U { A } table T { us: [U]; } root_type T;",
    "struct S { a: byte; b: long; c: short; } table T { s: S; } root_type T;",
    "struct S (force_align: 16) { a: int; } table T { s: S; } root_type T;",
    "struct S { a: [int:3]; b: [byte:5]; } table T { s: S; } root_type T;",
    "struct P { x: float; } struct S { p: [P:2]; z: double; } table T { s: [S]; } root_type T;",
    "table T { a: int = 0x10; b: double = -1.5e3; c: float = inf; d: float = -inf; e: double = nan; } root_type T;",
    "table T { a: bool = true; b: bool = false; c: bool = 1; } root_type T;",
    "table T { a: int = null; b: bool = null; c: double = null; } root_type T;",
    "table T { a: ulong = 18446744073709551615; b: long = -9223372036854775808; } root_type T;",
    "table T { a: byte = -128; b: ubyte = 255; c: short = -32768; d: ushort = 65535; } root_type T;",
    "table T { a: int (id: 1); b: int (id: 0); } root_type T;",
    "table T { a: int (deprecated); b: string (required); c: int (key); } root_type T;",
    "table T { k: string (key); } table R { ts: [T]; } root_type R;",
    "table T { k: double (key); } table R { ts: [T]; } root_type R;",
    "table T { a: [ubyte] (nested_flatbuffer: \"T\"); } root_type T;",
    "table T { a: [ubyte] (flexbuffer); } root_type T;",
    "attribute \"custom\"; table T { a: int (custom); } root_type T;",
    "attribute custom; table T { a: int (custom: 5); } root_type T;",
    "attribute \"c\"; table T (c: \"x y\") { a: int; } root_type T;",
    "table T { a: int; } root_type T; file_identifier \"ABCD\"; file_extension \"abc\";",
    "/// doc\ntable T {\n  /// field doc\n  a: int;\n} root_type T;",
    "/* block */ table T { a: int; /* inline */ } // trailing\nroot_type T;",
    "table T { a: string = \"default\"; } root_type T;",
    "table T { a: [int] = []; } root_type T;",
    "table T { a: string = 'single'; b: [string] = []; c: [int] = [1]; } root_type T;",
    "table T { a: string = 5; } root_type T;",
    "attribute 'quoted'; table T { a: int (quoted: 'v'); } root_type T;",
    "union U { A, T } table A { a: int; } table B { T: int; } table T { u: U; } root_type T;",
    "table A { a: int; } table B { T: int; } union U { A, T } table T { u: U; } root_type T;",
    "table T { a: int; } table U { t: T; } rpc_service S { M(T): U; } root_type T;",
    "table T { a: int8; b: uint8; c: int16; d: uint16; e: int32; f: uint32; g: int64; h: uint64; i: float32; j: float64; } root_type T;",
    "table T { a: int; } namespace; table U { t: T; } root_type U;",
    "native_include \"x.h\"; table T { a: int; } root_type T;",
    "table T { a: int (hash: \"fnv1_32\"); } root_type T;",
    "table T {} root_type T;",
    "struct S { a: int; } root_type S;",
    "table T { a: int; a: int; } root_type T;",
    "table T { a: int (id: 0); b: int (id: 2); } root_type T;",
    "table T { a: int (key); b: int (key); } root_type T;",
    "enum E : byte { A, A } table T { e: E; } root_type T;",
    "enum E : float { A } table T { e: E; } root_type T;",
    "table T { a: [[int]]; } root_type T;",
    "struct S { a: string; } table T { s: S; } root_type T;",
    "struct S {} table T { s: S; } root_type T;",
    "table T { a: int = 1.5; } root_type T;",
    "table T { a: ubyte = 256; } root_type T;",
    "table T { a: string (required) = null; } root_type T;",
    "table T { a: int; } root_type T; root_type T;",
    "table T { a: int; } file_identifier \"ABC\";",
    "table T { s: S; } struct S { x: int; } root_type T;",
    "table T { a: T; } root_type T;",
    "union U { T } table T { u: U; } root_type T;",
    "enum E : byte { A } union U { E } table T { u: U; } root_type T;",
]

VOCAB = [
    "table", "struct", "enum", "union", "namespace", "root_type", "file_identifier", "file_extension",
    "attribute", "rpc_service", "bool", "byte", "ubyte", "short", "ushort", "int", "uint", "long",
    "ulong", "float", "double", "string", "int8", "uint64", "float32", "{", "}", "(", ")", "[", "]",
    ":", ";", ",", "=", ".", "key", "required", "deprecated", "id", "force_align", "bit_flags",
    "nested_flatbuffer", "original_order", "hash", "flexbuffer", "null", "true", "false", '"ABCD"',
    '"x"', '""', "0", "1", "-1", "2", "3", "7", "8", "16", "255", "256", "65535", "65536", "2147483648",
    "4294967296", "9223372036854775808", "18446744073709551616", "0x10", "1.5", "1e40", "nan", "inf",
    "-inf", "-0.0", "T", "A", "B", "S", "E", "U", "x", "NONE",
]
NUMBERS = ["0", "1", "-1", "2", "7", "8", "16", "127", "128", "255", "256", "32767", "65535", "65536",
           "2147483647", "2147483648", "4294967295", "4294967296", "9223372036854775807",
           "9223372036854775808", "18446744073709551615", "18446744073709551616", "-129", "-32769",
           "0x7f", "0xff", "0x10000", "1.5", "-2.5", "1e40", "1e-40", "nan", "inf", "-inf", "-0.0"]

_TOK = re.compile(r'"(?:[^"\\\n]|\\.)*"|[A-Za-z_][A-Za-z0-9_]*|-?(?:0[xX][0-9a-fA-F]+|\d+\.?\d*(?:[eE][-+]?\d+)?)|\S')
_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)


def tokens(text: str) -> list[str]:
    return _TOK.findall(_COMMENT.sub(" ", text))


def mutate(text: str, rng: random.Random) -> str:
    toks = tokens(text)
    for _ in range(rng.choice((1, 1, 2, 3))):
        if not toks:
            toks = [rng.choice(VOCAB)]
        i = rng.randrange(len(toks))
        op = rng.randrange(7)
        if op == 0:
            del toks[i]
        elif op == 1:
            toks.insert(i, toks[i])
        elif op == 2 and i + 1 < len(toks):
            toks[i], toks[i + 1] = toks[i + 1], toks[i]
        elif op == 3:
            toks[i] = rng.choice(VOCAB)
        elif op == 4:
            toks.insert(i, rng.choice(VOCAB))
        elif op == 5:
            nums = [k for k, t in enumerate(toks) if re.fullmatch(r"-?[0-9].*", t)]
            if nums:
                toks[rng.choice(nums)] = rng.choice(NUMBERS)
            else:
                toks.insert(i, rng.choice(NUMBERS))
        else:
            ids = [k for k, t in enumerate(toks) if re.fullmatch(r"[A-Za-z_]\w*", t)]
            if ids:
                toks[rng.choice(ids)] = rng.choice(["int", "double", "string", "bool", "ubyte", "T", "S",
                                                    "E", "U", "A", "key", "table", "struct"])
    return " ".join(toks)


def seeds() -> list[tuple[str, dict[str, str]]]:
    """(main text, extra files) pairs."""
    out = [(c, {}) for c in CORPUS]
    out.append(((KITCHEN_DIR / "kitchen.fbs").read_text(), {"common.fbs": (KITCHEN_DIR / "common.fbs").read_text()}))
    for p in sorted(SDE_SCHEMAS.glob("*.fbs"))[::4]:
        out.append((p.read_text(), {}))
    out.append(((ROOT / "src" / "flatview" / "reflection.fbs").read_text(), {}))
    return out


def run_flatc(path: Path, outdir: Path) -> tuple[bool, str, bytes | None]:
    for f in outdir.glob("*.bfbs"):
        f.unlink()
    try:
        res = subprocess.run([FLATC, "--schema", "-b", "--bfbs-builtins", "--bfbs-comments", "-o", str(outdir),
                              "-I", str(path.parent), str(path)], capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return False, "flatc timed out", None
    if res.returncode != 0:
        msg = (res.stderr or res.stdout).strip()
        errs = [l.strip() for l in msg.splitlines() if re.search(r"error: \S", l)]
        return False, (errs[0] if errs else " ".join(msg.split())[:200]), None
    out = list(outdir.glob("*.bfbs"))
    return True, "", out[0].read_bytes() if out else None


def ours(path: Path) -> tuple[bool | None, str, flatview.Schema | None]:
    try:
        return True, "", flatview.load_schema(path)
    except flatview.SchemaError as e:
        return False, str(e), None
    except RecursionError as e:
        return None, f"RecursionError: {e}", None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", None


def bucket(msg: str) -> str:
    msg = re.sub(r"^.*?:\d+:(\s*\d+:)?\s*", "", msg)          # strip path:line:col
    msg = re.sub(r"^error:\s*", "", msg)
    msg = re.sub(r"'[^']*'|\"[^\"]*\"", "X", msg)
    msg = re.sub(r"\b\d+\b", "N", msg)
    return msg.strip()[:90]


def diff(a, b, path="") -> str:
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a or k not in b:
                return f"{path}.{k} only in {'ours' if k in a else 'flatc'}"
            r = diff(a[k], b[k], f"{path}.{k}")
            if r:
                return r
        return ""
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return f"{path}: {len(a)} vs {len(b)} items"
        for i, (x, y) in enumerate(zip(a, b)):
            r = diff(x, y, f"{path}[{i}]")
            if r:
                return r
        return ""
    if a != b and not (a != a and b != b):    # NaN == NaN here
        return f"{path}: ours {a!r} vs flatc {b!r}"
    return ""


def run(seconds: float, seed: int, only_corpus: bool) -> int:
    rng = random.Random(seed)
    base = seeds()
    counts: Counter = Counter()
    stats: Counter = Counter()
    examples: dict[tuple[str, str], str] = {}
    tmp = Path(tempfile.mkdtemp(prefix="schemafuzz"))
    out = tmp / "out"
    out.mkdir()
    deadline = time.time() + seconds
    i = 0
    try:
        while time.time() < deadline:
            if only_corpus or i < len(base):
                if i >= len(base):
                    break
                text, extra = base[i]
            else:
                text0, extra = rng.choice(base)
                text = mutate(text0, rng)
            i += 1
            for f in tmp.glob("*.fbs"):
                f.unlink()
            for name, body in extra.items():
                (tmp / name).write_text(body)
            path = tmp / "main.fbs"
            path.write_text(text)
            fok, fmsg, bfbs = run_flatc(path, out)
            ook, omsg, schema = ours(path)
            stats["total"] += 1
            kind = key = None
            if ook is None:
                kind, key = "ours-crashed", bucket(omsg)
            elif ook and not fok:
                kind, key = "ours-accepts-flatc-rejects", bucket(fmsg)
            elif fok and not ook:
                kind, key = "ours-rejects-flatc-accepts", bucket(omsg)
            elif not fok:
                stats["both-reject"] += 1
            else:
                stats["both-accept"] += 1
                if bfbs is None:
                    continue
                try:
                    theirs = flatview.load_bfbs(bfbs)
                except Exception as e:
                    kind, key = "bfbs-load-fails", bucket(str(e))
                else:
                    d = diff(normalise(schema, True), normalise(theirs, True))
                    if d and re.search(r"ours (1844674407370955161\d|9223372036854775\d{3}|\d{19,}) vs flatc 0$", d):
                        d = ""    # known flatc limitation: ulong defaults >= 2**63 are stored as 0 in .bfbs
                    if d:
                        kind, key = "both-accept-differ", re.sub(r"\d+", "N", d)[:90]
                    else:
                        stats["same"] += 1
            if kind:
                counts[(kind, key)] += 1
                if (kind, key) not in examples or len(text) < len(examples[(kind, key)]):
                    examples[(kind, key)] = text
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    FINDINGS.mkdir(parents=True, exist_ok=True)
    (FINDINGS / "findings.json").write_text(json.dumps(
        [{"kind": k, "bucket": b, "count": counts[(k, b)], "example": examples[(k, b)]}
         for (k, b) in sorted(counts, key=lambda kb: -counts[kb])], indent=1))
    print(f"schemas: {stats['total']}  both-reject: {stats['both-reject']}  both-accept: {stats['both-accept']}"
          f"  identical: {stats['same']}")
    print("\nfindings (kind, bucket): count   [examples in fuzz/.cache/findings/schema/findings.json]")
    for (kind, b), n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:6d}  {kind:27s} {b}")
    if not counts:
        print("  none")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=180)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--corpus-only", action="store_true", help="just the hand-written corpus and seeds")
    args = ap.parse_args()
    return run(args.seconds, args.seed, args.corpus_only)


if __name__ == "__main__":
    sys.exit(main())
