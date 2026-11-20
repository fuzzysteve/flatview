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
        if key is not None and key in self.strings:
            return self.strings[key]
        b = s.encode("utf-8") if isinstance(s, str) else bytes(s)
        self.prep(4, len(b) + 1)
        self.pad(1)
        self.place_bytes(b)
        self.place("<I", len(b), 4)
        off = self.offset()
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
        self.prep(4, 4 * len(offsets))
        for off in reversed(offsets):
            self.prepend_uoffset(off)
        self.place("<I", len(offsets), 4)
        return self.offset()

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
        (blob, minalign)). Returns the table's offset."""
        # Align first, so padding left by the previous object is not counted in
        # this table's size: identical tables then get identical vtables.
        self.prep(max([4, *(_align_of(it) for it in items)]), 0)
        start = self.offset()
        locs: dict[int, int] = {}
        # Largest first: they are prepended, so this minimises padding.
        for fid, kind, value, size in sorted(items, key=lambda it: -_align_of(it)):
            if kind == "offset":
                self.prepend_uoffset(value)
            elif kind == "struct":
                blob, minalign = value
                self.prep(minalign, len(blob))
                self.place_bytes(blob)
            else:
                self.prep(size, 0)
                self.place("<" + kind, value, size)
            locs[fid] = self.offset()
        self.prep(4, 0)
        self.place("<i", 0, 4)
        obj_off = self.offset()
        nslots = max(locs) + 1 if locs else 0
        entries = [obj_off - locs[i] if i in locs else 0 for i in range(nslots)]
        tsize = obj_off - start
        vt = struct.pack(f"<{2 + nslots}H", 4 + 2 * nslots, tsize, *entries)
        existing = self.vtables.get(vt) if dedup else None
        if existing is None:
            self.place_bytes(vt)  # vtables are 2-aligned; we are 4-aligned here
            existing = self.offset()
            if dedup:
                self.vtables[vt] = existing
        struct.pack_into("<i", self.buf, len(self.buf) - obj_off, existing - obj_off)
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


def _align_of(item) -> int:
    _, kind, value, size = item
    if kind == "struct":
        return value[1]
    return size


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

    # -- scalars ---------------------------------------------------------------
    def scalar(self, base: BaseType, enum: Enum | None, v: Any, path: str) -> Any:
        if base == BaseType.BOOL:
            if isinstance(v, bool):
                return v
            if isinstance(v, int) and v in (0, 1):
                return bool(v)
            raise EncodeError(path, f"expected bool, got {v!r}")
        if base.is_float:
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
        from ._reader import TableView
        if isinstance(v, TableView):
            v = v._asdict(defaults=False, enums="value", deprecated=True)
        elif not isinstance(v, Mapping):
            if hasattr(v, "_asdict"):
                v = v._asdict()
            elif hasattr(v, "__dict__"):
                v = vars(v)
            else:
                raise EncodeError(path, f"expected a mapping for table {obj.name}, got {type(v).__name__}")
        if self.strict:
            unknown = set(v) - set(obj.fields_by_name)
            if unknown:
                raise EncodeError(path, f"unknown field(s) for {obj.name}: {sorted(unknown)}")
        items: list[tuple[int, str, Any, int]] = []
        for f in obj.fields:
            fpath = f"{path}.{f.name}" if path else f.name
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
                continue
            if base.is_scalar:
                val = self.scalar(base, t.enum, fv, fpath)     # also maps enum/union type names
                if not f.optional and not self.force_defaults and val == f.default and not (
                        isinstance(val, float) and math.copysign(1, val) != math.copysign(1, f.default or 0.0)):
                    continue
                items.append((f.id, SCALAR_FORMAT[base], val, SCALAR_SIZE[base]))
            elif base == BaseType.OBJ and t.ref.is_struct:
                items.append((f.id, "struct", (self.struct_blob(t.ref, fv, fpath), t.ref.minalign), t.ref.bytesize))
            elif base == BaseType.STRING:
                if not isinstance(fv, (str, bytes)):
                    raise EncodeError(fpath, f"expected a string, got {type(fv).__name__}")
                items.append((f.id, "offset", self.string(fv, fpath), 4))
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
        return self.b.table(items)

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
            from ._reader import _resolve_in
            target = _resolve_in(self.schema, nested, obj.namespace)
            b = _encode(self.schema, value, target, sort_keys=self.sort_keys,
                        share_strings=self.share, force_defaults=self.force_defaults,
                        strict=self.strict, file_identifier=False)
            value = b.output
            # As flatc (ForceVectorAlignment to the nested builder's minalign):
            # the nested buffer's start must be as aligned as its contents.
            nested_align = max(8, b.minalign)
        if isinstance(value, (str, Mapping)):
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
    root_off = enc.table(obj, value, "")
    ident: str | bytes | None
    if file_identifier is True:
        ident = schema.file_ident if root is None or obj is schema.root else None
    elif file_identifier is False:
        ident = None
    else:
        ident = file_identifier
    b.output = b.finish(root_off, ident, size_prefixed)
    return b
