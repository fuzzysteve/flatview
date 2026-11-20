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
        self.fns: dict[Object, Any] = {}      # obj -> compiled table verifier

    # -- compiled fast path (see table_verifier) -----------------------------
    def table(self, obj: Object, pos: int, path: Any, depth: int) -> None:
        """Verify the table at ``pos``. ``path`` is a str or a lazy chain (see _fmt)."""
        fn = self.fns.get(obj)
        if fn is None:
            fn = self.fn_for(obj)
        fn(self, pos, path, depth)

    def fn_for(self, obj: Object):
        fn = self.fns[obj] = table_verifier(self.schema, obj, self.align, self.strict_size,
                                            self.utf8, self.sorted, self.strict_enums)
        return fn

    def slow(self, obj: Object, pos: int, chain: Any, depth: int, refs0: int) -> None:
        """A compiled check failed: verify this table again with the general
        code, from the same offset budget, so the error (message and path) is
        exactly the general verifier's."""
        self.refs = refs0
        self.table_generic(obj, pos, _fmt(chain), depth)

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
                    fmt = struct.Struct("<" + _fmt_char(f.type.base))
                    self.enum_value(f, fmt.unpack_from(self.buf, pos + f.offset)[0], f"{path}.{f.name}")

    def table_generic(self, obj: Object, pos: int, path: str, depth: int) -> None:
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
        for f in obj.fields:
            self.field(obj, f, pos, vt, vtlen, tlen, f"{path}.{f.name}" if path else f.name, depth)

    def field(self, obj: Object, f: Field, pos: int, vt: int, vtlen: int, tlen: int, fpath: str,
              depth: int) -> None:
        """Verify one field of the table at ``pos`` (the general case)."""
        buf = self.buf
        o = _u16(buf, vt + f.offset)[0] if f.offset < vtlen else 0
        if not o:
            if f.required:
                self.fail(fpath, "required field is missing")
            return
        t = f.type
        base = t.base
        fpos = pos + o
        if base.is_scalar:
            size = SCALAR_SIZE[base]
            if self.strict_size and o + size > tlen:
                self.fail(fpath, "field extends past the end of its table")
            self.check(fpos, size, size, fpath, "scalar")
            if t.enum is not None and base != BaseType.UTYPE:
                self.enum_value(f, struct.unpack_from("<" + _fmt_char(base), buf, fpos)[0], fpath)
            elif base == BaseType.UTYPE and t.enum is not None:
                ut = buf[fpos]
                if self.strict_enums and ut not in t.enum.by_value:
                    self.fail(fpath, f"union type {ut} is not a member of {t.enum.name}")
            return
        if base == BaseType.OBJ and t.ref.is_struct:
            if self.strict_size and o + t.ref.bytesize > tlen:
                self.fail(fpath, "struct extends past the end of its table")
            self.struct_(t.ref, fpos, fpath)
            return
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
                for i, v in enumerate(struct.unpack_from(f"<{n}{_fmt_char(el)}", self.buf, start)):
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
                self.sorted_keys(sub, start, n, fpath)
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

    def sorted_keys(self, sub: Object, start: int, n: int, chain: Any) -> None:
        """Keyed vector of verified tables must be sorted (and NaN-free)."""
        kr = _key_reader(sub)
        buf = self.buf
        prev = None
        for i in range(n):
            p = start + 4 * i
            k = kr(buf, p + _u32(buf, p)[0])
            if k != k:     # NaN: unordered, so no binary search over this vector is correct
                self.fail(f"{_fmt(chain)}[{i}]", f"key {sub.key_field.name} is NaN; lookups on this "
                          "vector would be wrong")
            if prev is not None and k < prev:
                self.fail(f"{_fmt(chain)}[{i}]", f"vector is not sorted by key {sub.key_field.name} "
                          f"({_show(k)} after {_show(prev)}); lookups would be wrong")
            prev = k

    def root(self, obj: Object, path: str = "") -> None:
        if self.n < 8:
            self.fail(path, f"buffer too small ({self.n} bytes)")
        self.table(obj, self.deref(0, path, "root table"), path, 0)


def _fmt_char(bt: BaseType) -> str:
    from ._schema import SCALAR_FORMAT
    return SCALAR_FORMAT[bt]


def _show(k: Any) -> str:
    return repr(k.decode("utf-8", "replace")) if isinstance(k, bytes) else repr(k)


def _fmt(chain: Any) -> str:
    """A path from a lazy chain: a str, or (parent chain, field name | index).

    Compiled verifiers pass chains so that no path string is built unless
    something fails."""
    if type(chain) is str:
        return chain
    parts = []
    while type(chain) is tuple:
        chain, part = chain
        parts.append(part)
    path = chain
    for part in reversed(parts):
        if type(part) is int:
            path = f"{path}[{part}]"
        else:
            path = f"{path}.{part}" if path else part
    return path


#: Tables with more fields than this are verified field by field, not compiled.
WIDE_TABLE = 256


def _int(x: Any) -> int:
    """Guard for every number interpolated into generated source."""
    if type(x) is not int:
        raise TypeError(f"refusing to generate code from non-integer {x!r}")
    return x


def table_verifier(schema: Schema, obj: Object, align: bool, strict_size: bool, utf8: bool,
                   check_sorted: bool, strict_enums: bool):
    """Cached ``(verifier, pos, path, depth) -> None`` function for ``obj``.

    Straight-line checks for the common fields (scalars, structs, strings,
    tables, and vectors of those); the rest (unions, nested flatbuffers,
    union type vectors, enum checks under strict_enums) go to the general
    ``_Verifier.field``. A compiled function accepts exactly what the general
    code accepts: when any of its own checks fails it hands the whole table
    to ``_Verifier.slow``, which re-verifies it with the general code from
    the same offset budget, so errors are identical. Schema text never
    enters the source: names and objects are constants, numbers pass _int().
    """
    opts = (bool(align), bool(strict_size), bool(utf8), bool(check_sorted), bool(strict_enums))
    cache = schema.__dict__.setdefault("_verifiers", {})
    key = (obj.name, *opts)
    fn = cache.get(key)
    if fn is None:
        fn = cache[key] = _compile_table(obj, *opts)
    return fn


def _has_enum_fields(obj: Object) -> bool:
    return any(f.type.base.is_scalar and f.type.enum is not None for f in obj.fields)


def _compile_table(obj: Object, align: bool, strict_size: bool, utf8: bool, check_sorted: bool,
                   strict_enums: bool):
    if len(obj.fields) > WIDE_TABLE:
        return lambda V, pos, chain, depth: V.table_generic(obj, pos, _fmt(chain), depth)
    ns: dict[str, Any] = {"i32": _i32, "u16": _u16, "u32": _u32, "OBJ": obj, "str": str,
                          "UnicodeDecodeError": UnicodeDecodeError}
    consts = 0

    def const(value: Any) -> str:
        nonlocal consts
        consts += 1
        ns[f"k{consts}"] = value
        return f"k{consts}"

    slow = "return V.slow(OBJ, pos, chain, depth, refs0)"

    def aligned(expr: str, a: int) -> str:
        """`` or <misaligned>`` when alignment is checked, else nothing."""
        return f" or ({expr} + base) & {_int(a - 1)}" if align and a > 1 else ""

    def deref(at: str, into: str, ind: str) -> list[str]:
        # = _Verifier.deref: one unit of the offset budget, aligned in-bounds
        # non-zero offset to a target inside the buffer
        return [f"{ind}r = V.refs + 1",
                f"{ind}V.refs = r",
                f"{ind}if r > maxr or {at} + 4 > n{aligned(at, 4)}:",
                f"{ind}    {slow}",
                f"{ind}{into} = u32(buf, {at})[0]",
                f"{ind}if not {into}:",
                f"{ind}    {slow}",
                f"{ind}{into} += {at}",
                f"{ind}if {into} >= n:",
                f"{ind}    {slow}"]

    def string(at: str, ind: str) -> list[str]:
        # = _Verifier.string
        out = [f"{ind}if {at} + 4 > n{aligned(at, 4)}:",
               f"{ind}    {slow}",
               f"{ind}se = {at} + 4 + u32(buf, {at})[0]",
               f"{ind}if se + 1 > n or buf[se]:",
               f"{ind}    {slow}"]
        if utf8:
            out += [f"{ind}try:",
                    f"{ind}    str(buf[{at} + 4:se], 'utf-8')",
                    f"{ind}except UnicodeDecodeError:",
                    f"{ind}    {slow}"]
        return out

    lines = ["def verify_table(V, pos, chain, depth):",
             "    buf = V.buf",
             "    n = V.n",
             "    base = V.base",
             "    maxr = V.max_refs",
             "    fns = V.fns",
             "    refs0 = V.refs",
             f"    if depth > V.max_depth or pos < 0 or pos + 4 > n{aligned('pos', 4)}:",
             f"        {slow}",
             "    vt = pos - i32(buf, pos)[0]",
             f"    if vt < 0 or vt + 2 > n{aligned('vt', 2)}:",
             f"        {slow}",
             "    vtlen = u16(buf, vt)[0]",
             "    if vtlen & 1 or vt + vtlen > n:",
             f"        {slow}"]
    # the whole vtable in one unpack (as the converters do): e[id] is the
    # field's offset, 0 when absent or beyond the vtable
    nslots = max((_int(f.id) for f in obj.fields), default=-1) + 1
    if nslots:
        from ._todict import _Unpackers, _ZeroPads
        ns["full"] = struct.Struct(f"<{nslots}H").unpack_from
        ns["VT"], ns["Z"] = _Unpackers(), _ZeroPads()
        lines += ["    k = (vtlen - 4) >> 1",
                  f"    e = full(buf, vt + 4) if k >= {nslots} else "
                  f"(VT[k](buf, vt + 4) + Z[{nslots} - k] if k > 0 else Z[{nslots}])"]
    if strict_size:
        lines += ["    if vtlen < 4:", f"        {slow}",
                  "    tlen = u16(buf, vt + 2)[0]",
                  "    if tlen < 4 or pos + tlen > n:", f"        {slow}"]
    else:
        lines.append("    tlen = 0")

    for f in obj.fields:
        t, base = f.type, f.type.base
        name = const(f.name)
        here = f"(chain, {name})"
        lines.append(f"    o = e[{_int(f.id)}]")
        general = [f"    if o or {f.required!r}:",
                   f"        V.field(OBJ, {const(f)}, pos, vt, vtlen, tlen, _fmt({here}), depth)"]
        missing = [f"    else:", f"        {slow}"] if f.required else []
        ns["_fmt"] = _fmt

        def too_long(size: int) -> str:
            return f" or o + {_int(size)} > tlen" if strict_size else ""

        if base.is_scalar:
            if strict_enums and t.enum is not None:
                lines += general
                continue
            size = SCALAR_SIZE[base]
            lines += ["    if o:",
                      "        p = pos + o",
                      f"        if p + {_int(size)} > n{aligned('p', size)}{too_long(size)}:",
                      f"            {slow}"] + missing
        elif base == BaseType.OBJ and t.ref.is_struct:
            if strict_enums and _has_enum_fields(t.ref):
                lines += general
                continue
            bs = t.ref.bytesize
            lines += ["    if o:",
                      "        p = pos + o",
                      f"        if p + {_int(bs)} > n{aligned('p', t.ref.minalign)}{too_long(bs)}:",
                      f"            {slow}"] + missing
        elif base in (BaseType.STRING, BaseType.OBJ, BaseType.VECTOR):
            el = t.element
            kind = None
            if base == BaseType.STRING:
                kind = "string"
            elif base == BaseType.OBJ:
                kind = "table"
            elif "nested_flatbuffer" in f.attributes or el in (BaseType.UNION, BaseType.UTYPE):
                kind = None
            elif el.is_scalar:
                kind = None if strict_enums and t.enum is not None else "scalars"
            elif el == BaseType.STRING:
                kind = "strings"
            elif el == BaseType.OBJ and t.ref.is_struct:
                kind = None if strict_enums else "structs"
            elif el == BaseType.OBJ:
                kind = "tables"
            if kind is None:
                lines += general
                continue
            body = ["        p = pos + o"]
            if strict_size:
                body += ["        if o + 4 > tlen:", f"            {slow}"]
            body += deref("p", "q", "        ")
            if kind == "string":
                body += string("q", "        ")
            elif kind == "table":
                sub = const(t.ref)
                body += [f"        (fns.get({sub}) or V.fn_for({sub}))(V, q, {here}, depth + 1)"]
            else:
                body += [f"        if q + 4 > n{aligned('q', 4)}:", f"            {slow}",
                         "        ln = u32(buf, q)[0]",
                         "        st = q + 4"]
                if kind == "scalars":
                    body += [f"        if st + ln * {_int(SCALAR_SIZE[el])} > n:", f"            {slow}"]
                elif kind == "structs":
                    body += [f"        if st + ln * {_int(t.ref.bytesize)} > n:", f"            {slow}"]
                else:
                    body += [f"        if st + 4 * ln > n{aligned('st', 4)}:", f"            {slow}"]
                    if kind == "strings":
                        body += ["        for a in range(st, st + 4 * ln, 4):"]
                        body += deref("a", "q", "            ")
                        body += string("q", "            ")
                    else:
                        sub = const(t.ref)
                        body += [f"        fn = fns.get({sub}) or V.fn_for({sub})",
                                 f"        vchain = {here}",
                                 "        for i in range(ln):",
                                 "            a = st + 4 * i"]
                        body += deref("a", "q", "            ")
                        body += ["            fn(V, q, (vchain, i), depth + 1)"]
                        if check_sorted and t.ref.key_field is not None:
                            body += ["        if ln:", f"            V.sorted_keys({sub}, st, ln, vchain)"]
            lines += ["    if o:"] + body + missing
        else:
            lines += general
    exec(compile("\n".join(lines) + "\n", "<flatview verifier>", "exec"), ns)
    return ns["verify_table"]


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
