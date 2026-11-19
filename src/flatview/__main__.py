"""Command line: inspect, dump, verify and convert FlatBuffer files.

    flatview info   types.fb
    flatview get    types.fb 587                 # key lookup in the root vector
    flatview dump   types.fb --jsonl --limit 5   # one JSON object per row
    flatview verify types.fb
    flatview schema types.bfbs                   # print as .fbs
    flatview encode schema.fbs data.json -o out.bin
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import re
import sys
import time
from typing import Any

from . import Schema, TableVector, Vector, VerificationError, load_schema, open as fv_open
from ._reader import TableView


#: Characters that can act on a terminal or disguise text when printed raw:
#: C0 controls (except tab/newline), DEL, the C1 controls (U+009B is a
#: one-character CSI), bidi overrides/isolates ("Trojan Source") and the
#: Unicode line/paragraph separators.
_TERMINAL_UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f"
                              "\u2028\u2029\u202a-\u202e\u2066-\u2069]")


def _escape_unsafe(text: str) -> str:
    """Replace terminal-unsafe characters with ``\\uXXXX``. Inside JSON strings
    that is the standard escape, so JSON output stays exact."""
    return _TERMINAL_UNSAFE.sub(lambda m: "\\u%04x" % ord(m.group()), text)


def _show(text: str) -> str:
    """Human-readable output: always neutralised."""
    return _escape_unsafe(str(text))


def _json_default(o: Any):
    if isinstance(o, bytes):
        return base64.b64encode(o).decode()
    raise TypeError(f"cannot serialise {type(o).__name__}")


def _clean(v: Any) -> Any:
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return str(v)
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_clean(x) for x in v]
    return v


def _dumps(v: Any, indent: int | None) -> str:
    # json.dumps already escapes C0 controls; this escapes the rest (C1, DEL,
    # bidi, U+2028/9) the same lossless way, so data can't drive the terminal.
    return _escape_unsafe(json.dumps(_clean(v), ensure_ascii=False, indent=indent, default=_json_default))


def _pick(root: TableView, path: str | None) -> Any:
    """Follow a dotted path like 'rows' or 'rows.3.name' from the root."""
    cur: Any = root
    if not path:
        return cur
    for part in path.split("."):
        if isinstance(cur, Vector):
            cur = cur[int(part)]
        else:
            cur = cur[part]
    return cur


def _main_vector(root: TableView) -> TableVector | None:
    vecs = [f for f in root._object.fields if f.type.base.name == "VECTOR" and f.type.element.name == "OBJ"]
    if len(vecs) == 1:
        v = root[vecs[0].name]
        return v if isinstance(v, TableVector) else None
    return None


def cmd_info(args) -> int:
    fb = fv_open(args.file, args.schema, size_prefixed=args.size_prefixed)
    s = fb.schema
    print(_show(f"file:    {args.file} ({len(fb.data):,} bytes uncompressed)"))
    print(_show(f"schema:  {s.source}"))
    print(f"root:    {s.root.name if s.root else '?'}")
    if s.file_ident:
        print(_show(f"ident:   {s.file_ident}"))
    for f in fb.root._object.fields:
        v = fb.root[f.name]
        extra = f"  ({len(v):,} items)" if isinstance(v, Vector) else ""
        print(f"  {f.name}: {f.type.describe()}{extra}")
    vec = _main_vector(fb.root)
    if vec is not None:
        obj = vec._cls._object
        key = obj.key_field
        print(f"\n{obj.name}" + (f"  (key: {key.name}, sorted: {vec.is_sorted()})" if key else ""))
        for f in obj.fields:
            flags = [x for x in ("required", "key", "optional", "deprecated") if getattr(f, x)]
            user = {k: v for k, v in f.attributes.items() if k not in ("id", "key", "required")}
            print(f"  {f.name}: {f.type.describe()}"
                  + (f"  [{', '.join(flags)}]" if flags else "")
                  + (f"  {user}" if user else ""))
    return 0


def cmd_get(args) -> int:
    fb = fv_open(args.file, args.schema, size_prefixed=args.size_prefixed)
    vec = _pick(fb.root, args.path) if args.path else _main_vector(fb.root)
    if not isinstance(vec, TableVector) or vec.key_field is None:
        print("error: no keyed vector to look up in (use --path)", file=sys.stderr)
        return 2
    key: Any = args.key
    if vec.key_field.type.base.is_integer:
        key = int(key)
    elif vec.key_field.type.base.is_float:
        key = float(key)
    row = vec.find(key)
    if row is None:
        print(f"error: {key!r} not found", file=sys.stderr)
        return 1
    print(_dumps(row._asdict(defaults=args.defaults, enums="name"), 2))
    return 0


def cmd_dump(args) -> int:
    fb = fv_open(args.file, args.schema, size_prefixed=args.size_prefixed)
    target = _pick(fb.root, args.path)
    if args.jsonl:
        if not isinstance(target, Vector):
            target = _main_vector(fb.root) if args.path is None else None
        if target is None:
            print("error: --jsonl needs a vector (use --path)", file=sys.stderr)
            return 2
        out = sys.stdout
        for i, row in enumerate(target):
            if args.limit is not None and i >= args.limit:
                break
            v = row._asdict(defaults=args.defaults, enums="name") if hasattr(row, "_asdict") else row
            out.write(_dumps(v, None))
            out.write("\n")
        return 0
    if isinstance(target, (TableView,)):
        v = target._asdict(defaults=args.defaults, enums="name")
    elif isinstance(target, Vector):
        items = target if args.limit is None else target[:args.limit]
        v = items._aslist(defaults=args.defaults, enums="name")
    else:
        v = target
    print(_dumps(v, 2))
    return 0


def cmd_verify(args) -> int:
    fb = fv_open(args.file, args.schema, size_prefixed=args.size_prefixed)
    t = time.perf_counter()
    try:
        fb.verify(strict_enums=args.strict_enums, check_sorted=not args.no_sorted)
    except VerificationError as e:
        print(_show(f"INVALID: {e}"))
        return 1
    print(f"OK ({len(fb.data):,} bytes, {time.perf_counter() - t:.2f}s)")
    return 0


def cmd_schema(args) -> int:
    text = load_schema(args.file).to_fbs()
    # Strings in the rendered schema are already escaped losslessly; doc
    # comments can't hold escapes, so neutralise them only for a terminal and
    # keep redirected output exact.
    if sys.stdout.isatty():
        text = _escape_unsafe(text)
    print(text, end="")
    return 0


def cmd_encode(args) -> int:
    schema: Schema = load_schema(args.schema_file)
    with open(args.json_file, encoding="utf-8") as fh:
        value = json.load(fh)
    data = schema.encode(value, args.root, size_prefixed=args.size_prefixed)
    with open(args.output, "wb") as fh:
        fh.write(data)
    print(f"wrote {len(data):,} bytes to {args.output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="flatview", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("file")
        sp.add_argument("-s", "--schema", help=".fbs/.bfbs (default: next to the file)")
        sp.add_argument("--size-prefixed", action="store_true", help="buffer starts with a 4-byte length")

    sp = sub.add_parser("info", help="summarise a file and its schema")
    common(sp)
    sp.set_defaults(fn=cmd_info)

    sp = sub.add_parser("get", help="look up one row by key")
    common(sp)
    sp.add_argument("key")
    sp.add_argument("--path", help="dotted path to the vector (default: the root's only table vector)")
    sp.add_argument("--defaults", action="store_true", help="include absent fields at their defaults")
    sp.set_defaults(fn=cmd_get)

    sp = sub.add_parser("dump", help="convert to JSON / JSON Lines")
    common(sp)
    sp.add_argument("--path", help="dotted path to dump, e.g. rows or rows.0.name")
    sp.add_argument("--jsonl", action="store_true", help="one JSON object per vector element")
    sp.add_argument("--limit", type=int)
    sp.add_argument("--defaults", action="store_true", help="include absent fields at their defaults")
    sp.set_defaults(fn=cmd_dump)

    sp = sub.add_parser("verify", help="fully verify a buffer")
    common(sp)
    sp.add_argument("--strict-enums", action="store_true")
    sp.add_argument("--no-sorted", action="store_true", help="skip the key-order check")
    sp.set_defaults(fn=cmd_verify)

    sp = sub.add_parser("schema", help="print a .bfbs/.fbs schema as .fbs text")
    sp.add_argument("file")
    sp.set_defaults(fn=cmd_schema)

    sp = sub.add_parser("encode", help="encode JSON with a schema")
    sp.add_argument("schema_file")
    sp.add_argument("json_file")
    sp.add_argument("-o", "--output", required=True)
    sp.add_argument("--root", help="root table (default: the schema's root_type)")
    sp.add_argument("--size-prefixed", action="store_true")
    sp.set_defaults(fn=cmd_encode)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
