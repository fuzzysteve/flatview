"""Schema-driven encoder: Python data -> FlatBuffer bytes.

Unlike the official Python builder (where you hand-order StartObject/
AddFieldX/EndObject calls from generated code), you give a dict/list tree -
or an existing view, to copy/modify data - and the schema drives everything:

* vectors of keyed tables are sorted by key, so lookups on the result work;
* identical vtables and (optionally) strings are shared;
* scalars are range checked, enums accept names, unknown fields are errors,
  required fields are enforced - each error names the path to the value;
* optional scalars (``= null``) are written iff the value is not None.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Mapping
from typing import Any

from ._schema import (
    BaseType, Enum, Field, Object, Schema, SchemaError, INT_RANGE, SCALAR_FORMAT, SCALAR_SIZE,
)


class EncodeError(ValueError):
    def __init__(self, path: str, msg: str):
        self.path = path
        super().__init__(f"{path or '<root>'}: {msg}")


class TableTooLarge(ValueError):
    """A table's inline fields (or its vtable) exceed the format's 64 KB limit."""


class Builder:
    """Low-level back-to-front FlatBuffer builder (bytes grow towards the front)."""

    def __init__(self, initial_size: int = 1024):
        self.buf = bytearray(max(16, initial_size))
        self.head = len(self.buf)
        self.minalign = 1
        self.vtables: dict[bytes, int] = {}
        self.strings: dict[str, int] = {}
        self.output = b""            # set by _encode() once finished

    # -- primitives ----------------------------------------------------------
    def offset(self) -> int:
        """Current size of the written data (offsets are measured from the end)."""
        return len(self.buf) - self.head

    def _grow(self, need: int) -> None:
        if self.head >= need:
            return
        old = len(self.buf)
        used = old - self.head
        new = old * 2
        while new - used < need:
            new *= 2
        self.buf[0:0] = bytes(new - old)
        self.head += new - old

    def pad(self, n: int) -> None:
        if n:
            self._grow(n)
            self.head -= n  # bytes below head are always zero

    def prep(self, size: int, additional: int) -> None:
        """Align so that after writing ``additional`` bytes we are ``size``-aligned."""
        if size > self.minalign:
            self.minalign = size
        self.pad((-(self.offset() + additional)) % size)

    def place(self, fmt: str, value: Any, size: int) -> None:
        self._grow(size)
        self.head -= size
        struct.pack_into(fmt, self.buf, self.head, value)

    def place_bytes(self, data: bytes) -> None:
        n = len(data)
        self._grow(n)
        self.head -= n
        self.buf[self.head:self.head + n] = data

    def prepend_uoffset(self, off: int) -> None:
        self.prep(4, 0)
        if off > self.offset():
            raise AssertionError("offset must refer to already-written data")
        self.place("<I", self.offset() - off + 4, 4)

    # -- objects ---------------------------------------------------------------
    def create_string(self, s: str | bytes, share: bool = True) -> int:
        key = s if share and isinstance(s, str) else None
        if key is not None:
            off = self.strings.get(key)
            if off is not None:
                return off
        b = s.encode("utf-8") if isinstance(s, str) else bytes(s)
        n = len(b)
        # = prep(4, n + 1); pad(1); place_bytes(b); place("<I", n)
        if self.minalign < 4:
            self.minalign = 4
        pad = (-(len(self.buf) - self.head + n + 1)) % 4
        self._grow(pad + n + 5)
        head = self.head - pad - 1 - n
        self.buf[head:head + n] = b
        head -= 4
        _pack_u32(self.buf, head, n)
        self.head = head
        off = len(self.buf) - head
        if key is not None:
            self.strings[key] = off
        return off

    def create_scalar_vector(self, base: BaseType, values: list, alignment: int | None = None) -> int:
        size = SCALAR_SIZE[base]
        n = len(values)
        self.prep(4, size * n)
        self.prep(max(size, alignment or 1), size * n)
        self._grow(size * n)
        self.head -= size * n
        struct.pack_into(f"<{n}{SCALAR_FORMAT[base]}", self.buf, self.head, *values)
        self.place("<I", n, 4)
        return self.offset()

    def create_offset_vector(self, offsets: list[int]) -> int:
        # = prep(4, 4n), then prepend_uoffset() per element (last first), then the length
        n = len(offsets)
        if self.minalign < 4:
            self.minalign = 4
        pad = (-(len(self.buf) - self.head)) % 4
        self._grow(pad + 4 * n + 4)
        self.head -= pad
        o0 = len(self.buf) - self.head + 4 * n      # offset of element 0, once written
        vals = [o0 - 4 * j - off for j, off in enumerate(offsets)]
        if vals and min(vals) < 4:
            raise AssertionError("offset must refer to already-written data")
        self.head -= 4 * n + 4
        struct.pack_into(f"<{n + 1}I", self.buf, self.head, n, *vals)
        return len(self.buf) - self.head

    def create_struct_vector(self, blobs: list[bytes], bytesize: int, minalign: int) -> int:
        n = len(blobs)
        self.prep(4, bytesize * n)
        self.prep(minalign, bytesize * n)
        self.place_bytes(b"".join(blobs))
        self.place("<I", n, 4)
        return self.offset()

    def table(self, items: list[tuple[int, str, Any, int]], dedup: bool = True) -> int:
        """Write a table. ``items`` = [(field_id, kind, value, size)] where kind
        is a struct format char, "offset" (value = offset) or "struct" (value =
        (blob, minalign)). Returns the table's offset.

        Layout (see _table_layout): aligned first, so padding left by the
        previous object is not counted in this table's size, and identical
        tables get identical vtables; fields largest-alignment first. The
        layout depends only on which fields are present, so it is cached and
        the table is written with one pack_into.
        """
        key = tuple([(it[0], "struct", len(it[2][0]), it[2][1]) if it[1] == "struct"
                     else (it[0], it[1], it[3], it[3]) for it in items])
        lay = _LAYOUTS.get(key)
        if lay is None:
            lay = _table_layout(key)
        return self.write_table(lay, [it[2] for it in items], dedup)

    def write_table(self, lay: tuple, values: list, dedup: bool = True) -> int:
        """table(), given the layout (from _table_layout) and the values in key order."""
        align, packer, order, objr, vt = lay
        if align > self.minalign:
            self.minalign = align
        pad = (-(len(self.buf) - self.head)) % align
        self._grow(pad + objr + len(vt))
        self.head -= pad
        base = len(self.buf) - self.head
        obj_off = base + objr
        existing = self.vtables.get(vt) if dedup else None
        args = [(existing if existing is not None else obj_off + len(vt)) - obj_off]
        for i, rloc in order:
            v = values[i]
            if rloc > 0:                    # uoffset, from its own position
                v = base + rloc - v
                if v < 4:
                    raise AssertionError("offset must refer to already-written data")
            elif rloc < 0:                  # struct: its bytes
                v = v[0]
            args.append(v)
        self.head -= objr
        packer(self.buf, self.head, *args)
        if existing is None:
            self.place_bytes(vt)            # vtables are 2-aligned; we are 4-aligned here
            if dedup:
                self.vtables[vt] = obj_off + len(vt)
        return obj_off

    def finish(self, root: int, file_identifier: str | bytes | None = None,
               size_prefixed: bool = False) -> bytes:
        ident = file_identifier.encode() if isinstance(file_identifier, str) else file_identifier
        if ident is not None and len(ident) != 4:
            raise ValueError("file identifier must be 4 bytes")
        extra = 4 + (4 if ident else 0) + (4 if size_prefixed else 0)
        self.prep(max(self.minalign, 4), extra)
        if ident:
            self.place_bytes(ident)
        self.prepend_uoffset(root)
        if size_prefixed:
            self.place("<I", self.offset(), 4)
        return bytes(self.buf[self.head:])


_pack_u32 = struct.Struct("<I").pack_into

#: non-finite float spellings accepted from text, e.g. ``flatview dump`` output
_NONFINITE = frozenset(f"{sign}{word}" for sign in ("", "+", "-") for word in ("nan", "inf", "infinity"))

#: table layouts by field pattern, see _table_layout (bounded: cleared when full)
_LAYOUTS: dict[tuple, tuple] = {}


def _table_layout(key: tuple) -> tuple:
    """Where each field of a table goes, for one pattern of present fields.

    ``key`` = ((field_id, kind, size, align), ...) in the caller's order.
    Positions are relative: the builder first pads to the largest alignment
    (at least 4), then prepends fields largest-alignment first (stable),
    each aligned, then the 4-byte soffset. Returns (align, pack_into for the
    whole table, [(item index, rloc)] in address order, table size, vtable
    bytes); rloc is the field's offset-from-end relative to the aligned start
    for uoffsets, -1 for structs, 0 for scalars.
    """
    align = max([4, *(k[3] for k in key)])
    r = 0
    locs: dict[int, int] = {}
    placed = []
    for i in sorted(range(len(key)), key=lambda i: -key[i][3]):
        fid, kind, size, a = key[i]
        if kind == "struct":
            r += (-(r + size)) % a + size
        else:
            r += (-r) % a + size
        locs[fid] = r
        placed.append((r, i, size))
    r += (-r) % 4 + 4
    objr = r
    nslots = max(locs) + 1 if locs else 0
    entries = [objr - locs[f] if f in locs else 0 for f in range(nslots)]
    # vtable entries are uint16: a table's inline data and its vtable must fit
    if objr > 0xFFFF:
        raise TableTooLarge(f"{objr:,} bytes of inline data; a table holds at most 65,535")
    if 4 + 2 * nslots > 0xFFFF:
        raise TableTooLarge(f"field id {nslots - 1} needs a vtable over 65,535 bytes")
    vt = struct.pack(f"<{2 + nslots}H", 4 + 2 * nslots, objr, *entries)
    fmt = ["<i"]
    pos = 4
    order = []
    for at, i, size in sorted((objr - r1, i, size) for r1, i, size in placed):
        if at > pos:
            fmt.append(f"{at - pos}x")
        kind = key[i][1]
        fmt.append("I" if kind == "offset" else f"{size}s" if kind == "struct" else kind)
        order.append((i, objr - at if kind == "offset" else -1 if kind == "struct" else 0))
        pos = at + size
    if objr > pos:
        fmt.append(f"{objr - pos}x")
    lay = (align, struct.Struct("".join(fmt)).pack_into, order, objr, vt)
    if len(_LAYOUTS) >= 4096:
        _LAYOUTS.clear()
    _LAYOUTS[key] = lay
    return lay


# ==========================================================================
# Schema-driven encoding

class _Encoder:
    def __init__(self, schema: Schema, builder: Builder, *, sort_keys: bool, share_strings: bool,
                 force_defaults: bool, strict: bool, share_tables: bool = False):
        self.schema = schema
        self.b = builder
        # Private (to_bfbs): the same Python mapping, met twice, is written once
        # and both offsets point at it. Not a public default: shared tables
        # count against the verifier's max_refs budget.
        self.tables: dict[int, tuple[Any, int]] | None = {} if share_tables else None
        self.sort_keys = sort_keys
        self.share = share_strings
        self.force_defaults = force_defaults
        self.strict = strict
        self.fns: dict[Object, Any] = {}     # obj -> compiled encoder (see table_encoder)

    # -- scalars ---------------------------------------------------------------
    def scalar(self, base: BaseType, enum: Enum | None, v: Any, path: str) -> Any:
        if base == BaseType.BOOL:
            if isinstance(v, bool):
                return v
            if isinstance(v, int) and v in (0, 1):
                return bool(v)
            raise EncodeError(path, f"expected bool, got {v!r}")
        if base.is_float:
            if isinstance(v, str) and v.strip().lower() in _NONFINITE:
                return float(v)                  # as flatc's JSON: nan, inf, -inf, infinity
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise EncodeError(path, f"expected a number, got {v!r}")
            try:
                v = float(v)
            except OverflowError:
                raise EncodeError(path, f"{v} does not fit in a {base.name.lower()}") from None
            if base == BaseType.FLOAT and not math.isinf(v) and not math.isnan(v) and abs(v) > 3.4028234663852886e38:
                raise EncodeError(path, f"{v} does not fit in a float")
            return v
        if isinstance(v, str) and enum is not None:
            v = self.enum_from_name(enum, v, path)
        elif isinstance(v, (list, tuple)) and enum is not None and enum.bit_flags:
            v = self.enum_from_name(enum, " ".join(str(x) for x in v), path)
        if isinstance(v, bool) or not isinstance(v, int):
            raise EncodeError(path, f"expected an integer{' or ' + enum.name + ' name' if enum else ''}, got {v!r}")
        v = int(v)
        lo, hi = INT_RANGE[base]
        if not lo <= v <= hi:
            raise EncodeError(path, f"{v} is out of range for {base.name.lower()} ({lo}..{hi})")
        return v

    def enum_from_name(self, enum: Enum, name: str, path: str) -> int:
        parts = name.replace("|", " ").split() if enum.bit_flags else [name]
        value = 0
        for p in parts:
            p = p.rpartition(".")[2]
            ev = enum.by_name.get(p)
            if ev is None:
                raise EncodeError(path, f"{p!r} is not a value of {enum.name}")
            value |= ev.value
        return value

    def string(self, value: str | bytes, path: str) -> int:
        try:
            return self.b.create_string(value, self.share)
        except UnicodeEncodeError as e:
            raise EncodeError(path, f"string is not valid Unicode ({e.reason} at index {e.start})") from None

    # -- structs ---------------------------------------------------------------
    def struct_blob(self, obj: Object, v: Any, path: str) -> bytes:
        blob = bytearray(obj.bytesize)
        self._fill_struct(obj, v, blob, 0, path)
        return bytes(blob)

    def _fill_struct(self, obj: Object, v: Any, blob: bytearray, base_off: int, path: str) -> None:
        from ._reader import StructView
        if isinstance(v, StructView):
            if v._object is obj:
                blob[base_off:base_off + obj.bytesize] = v._bytes()
                return
            v = v._asdict()
        if isinstance(v, Mapping):
            unknown = set(v) - set(obj.fields_by_name)
            if unknown and self.strict:
                raise EncodeError(path, f"unknown field(s) for struct {obj.name}: {sorted(unknown)}")
            values = []
            for f in obj.fields:
                if f.name not in v:
                    raise EncodeError(f"{path}.{f.name}", "struct fields are all required")
                values.append(v[f.name])
        elif isinstance(v, (tuple, list)):
            if len(v) != len(obj.fields):
                raise EncodeError(path, f"struct {obj.name} needs {len(obj.fields)} values, got {len(v)}")
            values = list(v)
        else:
            raise EncodeError(path, f"expected a mapping or tuple for struct {obj.name}, got {type(v).__name__}")
        for f, fv in zip(obj.fields, values):
            fpath = f"{path}.{f.name}"
            off = base_off + f.offset
            t = f.type
            if t.base.is_scalar:
                struct.pack_into("<" + SCALAR_FORMAT[t.base], blob, off, self.scalar(t.base, t.enum, fv, fpath))
            elif t.base == BaseType.OBJ:
                self._fill_struct(t.ref, fv, blob, off, fpath)
            elif t.base == BaseType.ARRAY:
                fv = list(fv)
                if len(fv) != t.fixed_length:
                    raise EncodeError(fpath, f"array needs exactly {t.fixed_length} items, got {len(fv)}")
                if t.element == BaseType.OBJ:
                    for i, item in enumerate(fv):
                        self._fill_struct(t.ref, item, blob, off + i * t.ref.bytesize, f"{fpath}[{i}]")
                else:
                    sz = SCALAR_SIZE[t.element]
                    fmt = "<" + SCALAR_FORMAT[t.element]
                    for i, item in enumerate(fv):
                        struct.pack_into(fmt, blob, off + i * sz,
                                         self.scalar(t.element, t.enum, item, f"{fpath}[{i}]"))

    # -- tables ----------------------------------------------------------------
    def table(self, obj: Object, v: Any, path: str) -> int:
        if self.tables is not None:
            seen = self.tables.get(id(v))
            if seen is not None and seen[0] is v:
                return seen[1]
            off = self._table(obj, v, path)
            self.tables[id(v)] = (v, off)       # keeps v alive, so its id stays unique
            return off
        return self._table(obj, v, path)

    def _table(self, obj: Object, v: Any, path: str) -> int:
        if type(v) is not dict:
            v = self.as_mapping(obj, v, path)
        fn = self.fns.get(obj)
        if fn is None:
            fn = self.fn_for(obj)
        return fn(self, v, path)

    def fn_for(self, obj: Object):
        """The compiled encoder for ``obj`` (cached on the encoder)."""
        fn = self.fns[obj] = table_encoder(self.schema, obj, self.strict, self.force_defaults)
        return fn

    def as_mapping(self, obj: Object, v: Any, path: str) -> Mapping:
        from ._reader import TableView
        if isinstance(v, TableView):
            return v._asdict(defaults=False, enums="value", deprecated=True)
        if isinstance(v, Mapping):
            return v
        if hasattr(v, "_asdict"):
            return v._asdict()
        if hasattr(v, "__dict__"):
            return vars(v)
        raise EncodeError(path, f"expected a mapping for table {obj.name}, got {type(v).__name__}")

    def generic_table(self, obj: Object, v: Mapping, path: str) -> int:
        """Field by field, for tables too wide to compile (see table_encoder)."""
        if self.strict:
            self.check_known(obj, v, path)
        items: list[tuple[int, str, Any, int]] = []
        for f in obj.fields:
            self.field(obj, f, v, items, path)
        try:
            return self.b.table(items)
        except TableTooLarge as e:
            raise EncodeError(path, f"table {obj.name}: {e}") from None

    def check_known(self, obj: Object, v: Mapping, path: str) -> None:
        unknown = set(v) - set(obj.fields_by_name)
        if unknown:
            raise EncodeError(path, f"unknown field(s) for {obj.name}: {sorted(unknown)}")

    def missing(self, path: str, name: str) -> None:
        raise EncodeError(_fpath(path, name), "required field is missing")

    def string_field(self, value: Any, path: str, name: str) -> int:
        if not isinstance(value, (str, bytes)):
            raise EncodeError(_fpath(path, name), f"expected a string, got {type(value).__name__}")
        try:
            return self.b.create_string(value, self.share)
        except UnicodeEncodeError as e:
            raise EncodeError(_fpath(path, name),
                              f"string is not valid Unicode ({e.reason} at index {e.start})") from None

    def field(self, obj: Object, f: Field, v: Mapping, items: list, path: str) -> None:
        """Encode one field of ``v`` into ``items`` (the general case)."""
        fpath = _fpath(path, f.name)
        fv = v.get(f.name)
        t = f.type
        base = t.base
        if fv is None:
            if f.required:
                raise EncodeError(fpath, "required field is missing")
            if base == BaseType.UTYPE and t.ref is not None:
                vf = obj.fields_by_name.get(f.name[:-5]) if f.name.endswith("_type") else None
                if vf is not None and v.get(vf.name) is not None:
                    uv = v.get(vf.name)
                    inferred = self._infer_union_type(t.ref, uv, fpath)
                    if inferred is not None:
                        items.append((f.id, "B", inferred, 1))
            return
        if base.is_scalar:
            val = self.scalar(base, t.enum, fv, fpath)     # also maps enum/union type names
            if not f.optional and not self.force_defaults and val == f.default and not (
                    isinstance(val, float) and math.copysign(1, val) != math.copysign(1, f.default or 0.0)):
                return
            items.append((f.id, SCALAR_FORMAT[base], val, SCALAR_SIZE[base]))
        elif base == BaseType.OBJ and t.ref.is_struct:
            items.append((f.id, "struct", (self.struct_blob(t.ref, fv, fpath), t.ref.minalign), t.ref.bytesize))
        elif base == BaseType.STRING:
            items.append((f.id, "offset", self.string_field(fv, path, f.name), 4))
        elif base == BaseType.OBJ:
            items.append((f.id, "offset", self.table(t.ref, fv, fpath), 4))
        elif base == BaseType.UNION:
            tname = f.name + "_type"
            ut = v.get(tname)
            if ut is None:
                ut = self._infer_union_type(t.ref, fv, fpath)
                if ut is None:
                    raise EncodeError(fpath, f"union value needs {tname} (cannot infer the member type)")
            elif isinstance(ut, str):
                ut = self.enum_from_name(t.ref, ut, fpath + "_type")
            items.append((f.id, "offset", self.union_value(t.ref, int(ut), fv, fpath), 4))
        elif base == BaseType.VECTOR:
            items.append((f.id, "offset", self.vector(obj, f, fv, v, fpath), 4))
        else:
            raise EncodeError(fpath, f"cannot encode field type {base.name}")

    def _infer_union_type(self, union: Enum, value: Any, path: str) -> int | None:
        from ._reader import TableView
        if isinstance(value, TableView):
            for ev in union.values:
                if ev.union_type is not None and ev.union_type.ref is value._object:
                    return ev.value
            raise EncodeError(path, f"{value._object.name} is not a member of union {union.name}")
        return None

    def union_value(self, union: Enum, ut: int, value: Any, path: str) -> int:
        member = union.by_value.get(ut)
        if member is None or member.union_type is None or member.union_type.base == BaseType.NONE:
            raise EncodeError(path, f"{ut} is not a valid member type of union {union.name}")
        mt = member.union_type
        if mt.base == BaseType.STRING:
            if not isinstance(value, (str, bytes)):
                raise EncodeError(path, f"expected a string, got {type(value).__name__}")
            return self.string(value, path)
        if mt.ref.is_struct:
            blob = self.struct_blob(mt.ref, value, path)
            self.b.prep(mt.ref.minalign, len(blob))
            self.b.place_bytes(blob)
            return self.b.offset()
        return self.table(mt.ref, value, path)

    def vector(self, obj: Object, f: Field, value: Any, parent: Mapping, path: str) -> int:
        t = f.type
        el = t.element
        nested = f.attributes.get("nested_flatbuffer")
        nested_align = 8
        if nested is not None and not isinstance(value, (bytes, bytearray, memoryview, list, tuple)):
            from ._reader import nested_target
            target = nested_target(self.schema, obj, f)
            b = _encode(self.schema, value, target, sort_keys=self.sort_keys,
                        share_strings=self.share, force_defaults=self.force_defaults,
                        strict=self.strict, file_identifier=False)
            value = b.output
            # As flatc (ForceVectorAlignment to the nested builder's minalign):
            # the nested buffer's start must be as aligned as its contents.
            nested_align = max(8, b.minalign)
        if isinstance(value, (str, Mapping)) or not hasattr(value, "__iter__"):
            raise EncodeError(path, f"expected a sequence for vector {t.describe()}, got {type(value).__name__}")
        if nested is not None and len(value) == 0:
            raise EncodeError(path, "nested flatbuffer must not be empty (omit the field instead)")
        from ._reader import ScalarVector
        if el.is_scalar:
            if isinstance(value, ScalarVector):
                value = value.tolist()
            if isinstance(value, (bytes, bytearray, memoryview)) and el in (BaseType.UBYTE, BaseType.BYTE):
                items = list(bytes(value)) if el == BaseType.UBYTE else list(struct.unpack(f"<{len(value)}b", value))
            else:
                items = [self.scalar(el, t.enum, x, f"{path}[{i}]") for i, x in enumerate(value)]
            return self.b.create_scalar_vector(el, items, nested_align if nested is not None else None)
        value = list(value)
        if el == BaseType.STRING:
            offs = []
            for i, x in enumerate(value):
                if not isinstance(x, (str, bytes)):
                    raise EncodeError(f"{path}[{i}]", f"expected a string, got {type(x).__name__}")
                offs.append(self.string(x, f"{path}[{i}]"))
            return self.b.create_offset_vector(offs)
        if el == BaseType.OBJ and t.ref.is_struct:
            blobs = [self.struct_blob(t.ref, x, f"{path}[{i}]") for i, x in enumerate(value)]
            return self.b.create_struct_vector(blobs, t.ref.bytesize, t.ref.minalign)
        if el == BaseType.OBJ:
            sub = t.ref
            indexed = list(enumerate(value))
            if self.sort_keys and sub.key_field is not None:
                indexed = self._sorted_by_key(sub, indexed, path)
            # paths use the caller's index, not the post-sort position
            if self.tables is None:
                fn = self.fns.get(sub) or self.fn_for(sub)
                # a loop, not a comprehension: before Python 3.12 a comprehension is
                # one more frame per nesting level
                offs = []
                for i, x in indexed:
                    offs.append(fn(self, x, f"{path}[{i}]") if type(x) is dict
                                else self.table(sub, x, f"{path}[{i}]"))
            else:
                offs = [self.table(sub, x, f"{path}[{i}]") for i, x in indexed]
            return self.b.create_offset_vector(offs)
        if el == BaseType.UNION:
            types = parent.get(f.name + "_type")
            if types is None:
                types = [self._infer_union_type(t.ref, x, f"{path}[{i}]") for i, x in enumerate(value)]
                if any(x is None for x in types):
                    raise EncodeError(path, f"union vector needs {f.name}_type")
            types = [self.enum_from_name(t.ref, x, path) if isinstance(x, str) else int(x) for x in types]
            if len(types) != len(value):
                raise EncodeError(path, f"{len(value)} union values but {len(types)} types")
            offs = [self.union_value(t.ref, ut, x, f"{path}[{i}]") for i, (ut, x) in enumerate(zip(types, value))]
            return self.b.create_offset_vector(offs)
        raise EncodeError(path, f"cannot encode vector of {el.name}")

    def _sorted_by_key(self, sub: Object, value: list[tuple[int, Any]], path: str) -> list:
        from ._reader import TableView
        kf = sub.key_field

        def key(pair):
            item = pair[1]
            if isinstance(item, TableView):
                k = item[kf.name]
            elif isinstance(item, Mapping):
                k = item.get(kf.name)
            else:
                k = getattr(item, kf.name, None)
            if kf.type.base == BaseType.STRING:
                return (k or "").encode("utf-8") if isinstance(k, str) else (k or b"")
            if k is None:
                return kf.default or 0
            if isinstance(k, str) and kf.type.enum is not None:
                return self.enum_from_name(kf.type.enum, k, path)
            if isinstance(k, float) and math.isnan(k):
                # NaN is unordered: a vector containing it can't be sorted, and
                # binary search over it (ours, or flatc's LookupByKey) misbehaves
                raise EncodeError(f"{path}[{pair[0]}].{kf.name}", "NaN cannot be used as a key")
            return k
        try:
            return sorted(value, key=key)
        except TypeError as e:
            raise EncodeError(path, f"cannot sort by key {kf.name}: {e}") from None


def _fpath(path: str, name: str) -> str:
    return f"{path}.{name}" if path else name


#: Tables with more fields than this are encoded field by field, not compiled.
WIDE_TABLE = 256
_MAX_FLOAT = 3.4028234663852886e38


def _int(x: Any) -> int:
    """Guard for every number interpolated into generated source."""
    if type(x) is not int:
        raise TypeError(f"refusing to generate code from non-integer {x!r}")
    return x


def table_encoder(schema: Schema, obj: Object, strict: bool, force_defaults: bool):
    """Cached ``(encoder, mapping, path) -> table offset`` function for ``obj``.

    The same field semantics as ``_Encoder.field`` in straight-line code:
    common values (in-range ints, floats, bools, str, nested dicts) take a
    fast path, anything else goes to the general methods, which do the full
    checks and raise the same errors. Schema-derived text never enters the
    source: names and fields are namespace constants, numbers pass _int().
    """
    cache = schema.__dict__.setdefault("_encoders", {})
    key = (obj, bool(strict), bool(force_defaults))   # by identity, as _todict.converter
    fn = cache.get(key)
    if fn is None:
        fn = cache[key] = _compile_table(obj, bool(strict), bool(force_defaults))
    return fn


def _mask_layout(cache: dict, descs: tuple, mask: int, obj: Object, path: str) -> tuple:
    """Layout for the fields whose bits are set in ``mask`` (bit j = field j)."""
    try:
        lay = _table_layout(tuple(d for j, d in enumerate(descs) if mask >> j & 1))
    except TableTooLarge as e:
        raise EncodeError(path, f"table {obj.name}: {e}") from None
    if len(cache) >= 1024:          # bounded, whatever mix of fields the data has
        cache.clear()
    cache[mask] = lay
    return lay


def _compile_table(obj: Object, strict: bool, force_defaults: bool):
    if len(obj.fields) > WIDE_TABLE:
        # one branch per field: a hostile schema could declare 32k of them
        return lambda enc, v, path: enc.generic_table(obj, v, path)
    ns: dict[str, Any] = {"fp": _fpath, "copysign": math.copysign, "OBJ": obj}
    consts = 0

    def const(value: Any) -> str:
        nonlocal consts
        consts += 1
        ns[f"k{consts}"] = value
        return f"k{consts}"

    lines = ["def encode_table(enc, v, path):"]
    if strict:
        lines += [f"    if not {const(frozenset(obj.fields_by_name))}.issuperset(v):",
                  "        enc.check_known(OBJ, v, path)"]
    # Each field has a fixed (id, kind, size, align): the table's layout
    # depends only on which fields are present, a bitmask m. Values go into
    # vals in field order; layouts are cached per mask in LAYOUTS.
    ns["LAYOUTS"] = {}
    ns["layout"] = _mask_layout
    descs = []
    lines += ["    vals = []", "    ap = vals.append", "    get = v.get", "    m = 0",
              "    fns = enc.fns", "    direct = enc.tables is None"]
    for j, f in enumerate(obj.fields):
        t, base = f.type, f.type.base
        fid = _int(f.id)
        name = const(f.name)
        bit = _int(1 << j)
        if base.is_scalar:
            descs.append((fid, SCALAR_FORMAT[base], SCALAR_SIZE[base], SCALAR_SIZE[base]))
        elif base == BaseType.OBJ and t.ref.is_struct:
            descs.append((fid, "struct", t.ref.bytesize, t.ref.minalign))
        else:
            descs.append((fid, "offset", 4, 4))
        # general method: it appends (id, kind, value, size) when the field is written
        general = [f"    one = []", f"    enc.field(OBJ, {const(f)}, v, one, path)",
                   f"    if one:", f"        m |= {bit}", f"        ap(one[0][2])"]
        if base.is_scalar and base != BaseType.UTYPE:
            if base == BaseType.BOOL:
                fast = "type(x) is bool"
            elif base == BaseType.DOUBLE:
                fast = "type(x) is float"
            elif base == BaseType.FLOAT:
                fast = f"type(x) is float and -{const(_MAX_FLOAT)} <= x <= {const(_MAX_FLOAT)}"
            else:
                lo, hi = INT_RANGE[base]
                fast = f"type(x) is int and {_int(lo)} <= x <= {_int(hi)}"
            body = [f"        if not ({fast}):",
                    f"            x = enc.scalar({const(base)}, {const(t.enum)}, x, fp(path, {name}))"]
            put = f"ap(x); m |= {bit}"
            if f.optional or force_defaults:
                body.append(f"        {put}")
            elif base.is_float:
                d = const(f.default)
                sign = const(math.copysign(1, f.default or 0.0))
                body.append(f"        if not (x == {d} and copysign(1, x) == {sign}):")
                body.append(f"            {put}")
            else:
                body.append(f"        if x != {const(f.default)}:")
                body.append(f"            {put}")
        elif base == BaseType.STRING:
            body = [f"        ap(enc.string_field(x, path, {name})); m |= {bit}"]
        elif base == BaseType.OBJ and not t.ref.is_struct:
            sub = const(t.ref)
            # a plain dict goes straight to the child's compiled encoder: one
            # Python frame per nesting level, so deep data fits the recursion limit
            body = [f"        if type(x) is dict and direct:",
                    f"            ap((fns.get({sub}) or enc.fn_for({sub}))(enc, x, fp(path, {name})))",
                    f"        else:",
                    f"            ap(enc.table({sub}, x, fp(path, {name})))",
                    f"        m |= {bit}"]
        elif base == BaseType.VECTOR:
            body = [f"        ap(enc.vector(OBJ, {const(f)}, x, v, fp(path, {name}))); m |= {bit}"]
        else:
            lines += general           # structs, unions, union types: the general method
            continue
        lines.append(f"    x = get({name})")
        if f.required:
            lines += ["    if x is None:", f"        enc.missing(path, {name})"]
            lines += ["    else:"]
        else:
            lines += ["    if x is not None:"]
        lines += body
    ns["DESCS"] = tuple(descs)
    lines += ["    lay = LAYOUTS.get(m)",
              "    if lay is None:",
              "        lay = layout(LAYOUTS, DESCS, m, OBJ, path)",
              "    return enc.b.write_table(lay, vals)"]
    exec(compile("\n".join(lines) + "\n", "<flatview encoder>", "exec"), ns)
    return ns["encode_table"]


def encode(schema: Schema, value: Any, root: str | Object | None = None, *,
           file_identifier: bool | str | bytes = True, size_prefixed: bool = False,
           sort_keys: bool = True, share_strings: bool = True, force_defaults: bool = False,
           strict: bool = True, initial_size: int = 1024) -> bytes:
    """Encode ``value`` (dict tree, or a view) as a FlatBuffer of ``root`` type.

    file_identifier: True uses the schema's identifier (if any), False omits
                     it, or pass the 4 bytes explicitly.
    sort_keys:       sort vectors of keyed tables (required for lookups).
    share_strings:   write each distinct string once.
    force_defaults:  write scalar fields even when equal to their default.
    strict:          reject unknown field names.
    """
    return _encode(schema, value, root, file_identifier=file_identifier, size_prefixed=size_prefixed,
                   sort_keys=sort_keys, share_strings=share_strings, force_defaults=force_defaults,
                   strict=strict, initial_size=initial_size).output


def _encode(schema: Schema, value: Any, root: str | Object | None, *,
            file_identifier: bool | str | bytes = True, size_prefixed: bool = False,
            sort_keys: bool = True, share_strings: bool = True, force_defaults: bool = False,
            strict: bool = True, initial_size: int = 1024, share_tables: bool = False) -> Builder:
    """encode(), returning the finished Builder (``.output``, ``.minalign``)."""
    obj = schema.object(root)
    if obj.is_struct:
        raise SchemaError("the root of a buffer must be a table")
    b = Builder(initial_size)
    enc = _Encoder(schema, b, sort_keys=sort_keys, share_strings=share_strings,
                   force_defaults=force_defaults, strict=strict, share_tables=share_tables)
    try:
        root_off = enc.table(obj, value, "")
    except RecursionError:
        raise EncodeError("", "data nested too deeply to encode (Python's recursion limit); "
                          "FlatBuffers verifiers reject deep nesting anyway (flatc's default "
                          "limit is 64)") from None
    ident: str | bytes | None
    if file_identifier is True:
        ident = schema.file_ident if root is None or obj is schema.root else None
    elif file_identifier is False:
        ident = None
    else:
        ident = file_identifier
    b.output = b.finish(root_off, ident, size_prefixed)
    return b
