"""Full structural verifier for FlatBuffers (the official Python library has none).

Checks every offset is in bounds and aligned, vtables are well formed,
strings are terminated valid UTF-8, required fields are present, union
types are known, nested flatbuffers are themselves valid, recursion depth
and table count are bounded, and (optionally) that keyed vectors are sorted
so binary-search lookups are correct. Errors carry a path like
``rows[17].name.en``.
"""

from __future__ import annotations

import struct
from typing import Any

from ._reader import _as_buffer, _key_reader
from ._schema import BaseType, Field, Object, Schema, SCALAR_SIZE

_u16 = struct.Struct("<H").unpack_from
_u32 = struct.Struct("<I").unpack_from
_i32 = struct.Struct("<i").unpack_from


class VerificationError(ValueError):
    def __init__(self, path: str, msg: str):
        self.path = path
        super().__init__(f"{path or '<root>'}: {msg}")


class _Verifier:
    def __init__(self, schema: Schema, buf: memoryview, *, max_depth: int, max_refs: int,
                 check_alignment: bool, check_sorted: bool, check_utf8: bool, strict_enums: bool,
                 base: int = 0, strict_table_size: bool = False):
        self.schema = schema
        # flatc's verifier bounds fields by the buffer, not by the table size
        # recorded in the vtable. The Go builder (used for CCP's SDE files)
        # shares vtables between tables of different sizes, so a vtable can
        # record a size too small for some of its users - see
        # https://github.com/google/flatbuffers/issues/9413. The data is
        # intact, so the stricter check is opt-in.
        self.strict_size = strict_table_size
        # Offset of buf[0] within the enclosing allocation (4 after a size
        # prefix): alignment is relative to the start of the whole buffer.
        # Nested buffers are their own allocation (base 0), as in flatc.
        self.base = base
        self.buf = buf
        self.n = len(buf)
        self.max_depth = max_depth
        # Budget of offsets followed. Shared sub-objects let a small buffer
        # reference the same data millions of times ("reference
        # amplification"); every followed offset costs one unit.
        self.max_refs = max_refs
        self.refs = 0
        self.align = check_alignment
        self.sorted = check_sorted
        self.utf8 = check_utf8
        self.strict_enums = strict_enums

    def fail(self, path: str, msg: str) -> None:
        raise VerificationError(path, msg)

    def check(self, pos: int, size: int, align: int, path: str, what: str) -> None:
        if pos < 0 or pos + size > self.n:
            self.fail(path, f"{what} at {pos} (+{size}) is outside the buffer ({self.n} bytes)")
        if self.align and align > 1 and (pos + self.base) % align:
            self.fail(path, f"{what} at {pos} is not {align}-byte aligned")

    def deref(self, pos: int, path: str, what: str) -> int:
        self.refs += 1
        if self.refs > self.max_refs:
            self.fail(path, f"followed more than {self.max_refs:,} offsets: the buffer shares "
                      "sub-objects heavily (possible reference amplification); raise max_refs "
                      "if this data is trusted")
        self.check(pos, 4, 4, path, f"offset to {what}")
        off = _u32(self.buf, pos)[0]
        if off == 0:
            self.fail(path, f"offset to {what} is zero")
        target = pos + off
        if target >= self.n:
            self.fail(path, f"offset to {what} points outside the buffer")
        return target

    # -- leaves --------------------------------------------------------------
    def string(self, pos: int, path: str) -> None:
        self.check(pos, 4, 4, path, "string length")
        n = _u32(self.buf, pos)[0]
        self.check(pos + 4, n + 1, 1, path, "string data")
        if self.buf[pos + 4 + n] != 0:
            self.fail(path, "string is not zero-terminated")
        if self.utf8:
            try:
                str(self.buf[pos + 4:pos + 4 + n], "utf-8")
            except UnicodeDecodeError as e:
                self.fail(path, f"string is not valid UTF-8 ({e.reason} at byte {e.start})")

    def enum_value(self, f: Field, value: int, path: str) -> None:
        enum = f.type.enum
        if not self.strict_enums or enum is None or enum.is_union:
            return
        if enum.bit_flags:
            allowed = 0
            for v in enum.values:
                allowed |= v.value
            if value & ~allowed:
                self.fail(path, f"{value} has bits not defined in {enum.name}")
        elif value not in enum.by_value:
            self.fail(path, f"{value} is not a value of {enum.name}")

    # -- composites ----------------------------------------------------------
    def struct_(self, obj: Object, pos: int, path: str) -> None:
        self.check(pos, obj.bytesize, obj.minalign, path, f"struct {obj.short_name}")
        if self.strict_enums:
            for f in obj.fields:
                if f.type.base.is_scalar and f.type.enum is not None:
                    fmt = struct.Struct("<" + _fmt(f.type.base))
                    self.enum_value(f, fmt.unpack_from(self.buf, pos + f.offset)[0], f"{path}.{f.name}")

    def table(self, obj: Object, pos: int, path: str, depth: int) -> None:
        if depth > self.max_depth:
            self.fail(path, f"nesting deeper than {self.max_depth}")
        self.check(pos, 4, 4, path, f"table {obj.short_name}")
        vt = pos - _i32(self.buf, pos)[0]
        # As flatc: the vtable is 2-byte aligned, its size even and it fits;
        # a 0- or 2-byte vtable just means "no fields present".
        self.check(vt, 2, 2, path, "vtable")
        vtlen = _u16(self.buf, vt)[0]
        if vtlen % 2:
            self.fail(path, f"vtable size {vtlen} is invalid")
        self.check(vt, vtlen, 1, path, "vtable")
        tlen = 0
        if self.strict_size:
            if vtlen < 4:
                self.fail(path, f"vtable size {vtlen} has no table size")
            tlen = _u16(self.buf, vt + 2)[0]
            if tlen < 4:
                self.fail(path, f"table size {tlen} is invalid")
            self.check(pos, tlen, 1, path, "table data")
        buf = self.buf
        for f in obj.fields:
            fpath = f"{path}.{f.name}" if path else f.name
            o = _u16(buf, vt + f.offset)[0] if f.offset < vtlen else 0
            if not o:
                if f.required:
                    self.fail(fpath, "required field is missing")
                continue
            t = f.type
            base = t.base
            fpos = pos + o
            if base.is_scalar:
                size = SCALAR_SIZE[base]
                if self.strict_size and o + size > tlen:
                    self.fail(fpath, "field extends past the end of its table")
                self.check(fpos, size, size, fpath, "scalar")
                if t.enum is not None and base != BaseType.UTYPE:
                    self.enum_value(f, struct.unpack_from("<" + _fmt(base), buf, fpos)[0], fpath)
                elif base == BaseType.UTYPE and t.enum is not None:
                    ut = buf[fpos]
                    if self.strict_enums and ut not in t.enum.by_value:
                        self.fail(fpath, f"union type {ut} is not a member of {t.enum.name}")
                continue
            if base == BaseType.OBJ and t.ref.is_struct:
                if self.strict_size and o + t.ref.bytesize > tlen:
                    self.fail(fpath, "struct extends past the end of its table")
                self.struct_(t.ref, fpos, fpath)
                continue
            if self.strict_size and o + 4 > tlen:
                self.fail(fpath, "offset field extends past the end of its table")
            if base == BaseType.STRING:
                self.string(self.deref(fpos, fpath, "string"), fpath)
            elif base == BaseType.OBJ:
                self.table(t.ref, self.deref(fpos, fpath, "table"), fpath, depth + 1)
            elif base == BaseType.UNION:
                self.union(obj, f, pos, vt, vtlen, fpos, fpath, depth)
            elif base == BaseType.VECTOR:
                if t.element == BaseType.UTYPE:
                    # union type vector: its value vector must be present too
                    vf = obj.fields_by_name.get(f.name[:-5]) if f.name.endswith("_type") else None
                    if vf is not None and vf.type.element == BaseType.UNION:
                        vo = _u16(buf, vt + vf.offset)[0] if vf.offset < vtlen else 0
                        if not vo:
                            self.fail(fpath, "union type vector without its value vector")
                self.vector(obj, f, pos, vt, vtlen, self.deref(fpos, fpath, "vector"), fpath, depth)
            else:
                self.fail(fpath, f"unsupported field type {base.name}")

    def _union_type_at(self, obj: Object, f: Field, pos: int, vt: int, vtlen: int) -> Field:
        tf = obj.fields_by_name.get(f.name + "_type")
        if tf is None:
            self.fail(f.name, "union field without a _type field")
        return tf

    def union(self, obj, f, pos, vt, vtlen, fpos, fpath, depth) -> None:
        tf = self._union_type_at(obj, f, pos, vt, vtlen)
        o = _u16(self.buf, vt + tf.offset)[0] if tf.offset < vtlen else 0
        ut = self.buf[pos + o] if o else 0
        target = self.deref(fpos, fpath, "union value")   # flatc checks the offset itself
        # With type NONE the value is ignored (readers return None), as flatc does.
        if ut != 0:
            self.union_member(f.type.ref, ut, target, fpath, depth)

    def union_member(self, union, ut: int, target: int, path: str, depth: int) -> None:
        member = union.by_value.get(ut)
        if member is None:
            # A member added by a newer schema: readers return None for it,
            # so (like flatc) it is accepted; strict_enums flags it.
            if self.strict_enums:
                self.fail(path, f"union type {ut} is not a member of {union.name}")
            return
        mt = member.union_type
        if mt is None or mt.base == BaseType.NONE:
            return
        if mt.base == BaseType.STRING:
            self.string(target, path)
        elif mt.ref.is_struct:
            self.struct_(mt.ref, target, path)
        else:
            self.table(mt.ref, target, path, depth + 1)

    def vector(self, obj, f, pos, vt, vtlen, vp, fpath, depth) -> None:
        t = f.type
        el = t.element
        self.check(vp, 4, 4, fpath, "vector length")
        n = _u32(self.buf, vp)[0]
        start = vp + 4
        if el.is_scalar:
            size = SCALAR_SIZE[el]
            # As flatc: only the length prefix must be aligned, not the elements.
            self.check(start, n * size, 1, fpath, "vector data")
            if self.strict_enums and t.enum is not None and el != BaseType.UTYPE:
                for i, v in enumerate(struct.unpack_from(f"<{n}{_fmt(el)}", self.buf, start)):
                    self.enum_value(f, v, f"{fpath}[{i}]")
            nested = f.attributes.get("nested_flatbuffer")
            if nested is not None and not n:
                self.fail(fpath, "nested flatbuffer is empty")
            if nested is not None:
                from ._reader import _resolve_in
                target = _resolve_in(self.schema, nested, obj.namespace)
                # As flatc: a nested buffer is verified on its own, so its
                # alignment is relative to its own start, not the outer buffer's.
                sub = _Verifier(self.schema, self.buf[start:start + n], max_depth=self.max_depth - depth,
                                max_refs=self.max_refs - self.refs, check_alignment=self.align,
                                check_sorted=self.sorted, check_utf8=self.utf8,
                                strict_enums=self.strict_enums, base=0,
                                strict_table_size=self.strict_size)
                sub.root(target, fpath)
                self.refs += sub.refs
            return
        if el == BaseType.OBJ and t.ref.is_struct:
            self.check(start, n * t.ref.bytesize, 1, fpath, "vector data")
            if self.strict_enums:
                for i in range(n):
                    self.struct_(t.ref, start + i * t.ref.bytesize, f"{fpath}[{i}]")
            return
        self.check(start, 4 * n, 4, fpath, "vector data")
        if el == BaseType.STRING:
            for i in range(n):
                p = start + 4 * i
                self.string(self.deref(p, f"{fpath}[{i}]", "string"), f"{fpath}[{i}]")
        elif el == BaseType.OBJ:
            sub = t.ref
            for i in range(n):
                p = start + 4 * i
                self.table(sub, self.deref(p, f"{fpath}[{i}]", "table"), f"{fpath}[{i}]", depth + 1)
            if self.sorted and sub.key_field is not None and n > 0:
                kr = _key_reader(sub)
                prev = None
                for i in range(n):
                    p = start + 4 * i
                    k = kr(self.buf, p + _u32(self.buf, p)[0])
                    if k != k:     # NaN: unordered, so no binary search over this vector is correct
                        self.fail(f"{fpath}[{i}]", f"key {sub.key_field.name} is NaN; lookups on this "
                                  "vector would be wrong")
                    if prev is not None and k < prev:
                        self.fail(f"{fpath}[{i}]", f"vector is not sorted by key {sub.key_field.name} "
                                  f"({_show(k)} after {_show(prev)}); lookups would be wrong")
                    prev = k
        elif el == BaseType.UNION:
            tf = self._union_type_at(obj, f, pos, vt, vtlen)
            o = _u16(self.buf, vt + tf.offset)[0] if tf.offset < vtlen else 0
            if not o:
                self.fail(fpath, "union vector without a type vector")
            tvp = self.deref(pos + o, fpath + "_type", "vector")
            tn = _u32(self.buf, tvp)[0]
            self.check(tvp + 4, tn, 1, fpath + "_type", "vector data")
            if tn != n:
                self.fail(fpath, f"union vector has {n} values but {tn} types")
            for i in range(n):
                ut = self.buf[tvp + 4 + i]
                if ut == 0 or (ut not in t.ref.by_value and not self.strict_enums):
                    continue     # NONE / unknown member: never followed, as flatc
                p = start + 4 * i
                self.union_member(t.ref, ut, self.deref(p, f"{fpath}[{i}]", "union value"),
                                  f"{fpath}[{i}]", depth)
        else:
            self.fail(fpath, f"unsupported vector element type {el.name}")

    def root(self, obj: Object, path: str = "") -> None:
        if self.n < 8:
            self.fail(path, f"buffer too small ({self.n} bytes)")
        self.table(obj, self.deref(0, path, "root table"), path, 0)


def _fmt(bt: BaseType) -> str:
    from ._schema import SCALAR_FORMAT
    return SCALAR_FORMAT[bt]


def _show(k: Any) -> str:
    return repr(k.decode("utf-8", "replace")) if isinstance(k, bytes) else repr(k)


def verify(schema: Schema, data: Any, root: str | Object | None = None, *,
           size_prefixed: bool = False, check_identifier: bool = True,
           max_depth: int = 64, max_refs: int | None = None, check_alignment: bool = True,
           check_sorted: bool = True, check_utf8: bool = True, strict_enums: bool = False,
           strict_table_size: bool = False) -> None:
    """Verify a whole buffer against the schema. Raises VerificationError.

    max_refs bounds the number of offsets followed. The default, buffer size
    / 4, is the most any buffer without shared sub-objects can contain, so
    reference-amplification buffers (a few KB that reference one sub-table
    millions of times) are rejected quickly. Raise it for trusted data that
    deliberately shares tables.
    """
    buf = _as_buffer(data)
    obj = schema.object(root)
    base = 0
    if size_prefixed:
        base = 4
        if len(buf) < 4:
            raise VerificationError("", "buffer too small for a size prefix")
        n = _u32(buf, 0)[0]
        if n + 4 > len(buf):
            raise VerificationError("", f"size prefix {n} exceeds buffer length {len(buf)}")
        buf = buf[4:4 + n]
    # the identifier belongs to the schema's root type, however root was given
    if check_identifier and schema.file_ident and obj is schema.root:
        if bytes(buf[4:8]) != schema.file_ident.encode():
            raise VerificationError("", f"file identifier {bytes(buf[4:8])!r} != {schema.file_ident!r}")
    if max_refs is None:
        # Each offset occupies 4 bytes, so a buffer without shared
        # sub-objects can never need more than this.
        max_refs = len(buf) // 4 + 1
    _Verifier(schema, buf, max_depth=max_depth, max_refs=max_refs, check_alignment=check_alignment,
              check_sorted=check_sorted, check_utf8=check_utf8, strict_enums=strict_enums,
              base=base, strict_table_size=strict_table_size).root(obj)
