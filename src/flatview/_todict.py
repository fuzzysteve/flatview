"""Fast whole-tree conversion to plain Python (dicts / lists / scalars).

For each table and struct in a schema we generate, compile and cache a
dedicated converter function ``(buf, pos) -> dict`` per output mode. A
converter works directly on buffer offsets: it decodes the table's whole
vtable with one ``struct`` call, then reads each field with straight-line
code. No view objects are created and nothing about the schema is
re-examined per row.
"""

from __future__ import annotations

import enum
import mmap
import struct
from typing import Any, Callable

from ._schema import BaseType, Enum, Field, Object, Schema, SCALAR_FORMAT, SCALAR_SIZE

def _flag_namer(enum_: Enum) -> Callable[[int], Any]:
    """flatc-style space-separated names for a bit_flags value."""
    members = [(v.name, v.value) for v in enum_.values if v.value]
    known = 0
    for _, m in members:
        known |= m

    def name(v: int) -> Any:
        if not v or v & ~known:
            return v        # bits no member names: keep the number (as flatc does)
        return " ".join(n for n, m in members if (v & m) == m)
    return name


#: Tables with more fields than this use the generic (non-compiled) converter.
WIDE_TABLE = 256


def _generic_table(schema: Schema, obj: Object, defaults: bool, enums: str,
                   deprecated: bool) -> Callable[[Any, int], dict]:
    """Field-by-field converter through the view classes: same output as the
    compiled converters, constant code size whatever the table width."""
    from ._reader import _plain, view_class
    cls = view_class(schema, obj)
    fields = [(f.name, cls._getters[f.name], f.key) for f in obj.fields
              if deprecated or not f.deprecated]

    def convert(buf, pos):
        # views expect a memoryview; the fast path may hand us bytes/mmap
        view = cls(buf if isinstance(buf, memoryview) else memoryview(buf), pos)
        d = {}
        for name, getter, key in fields:
            if not defaults and not key and not view._has(name):
                continue
            d[name] = _plain(getter(view), defaults, enums, deprecated)
        return d
    return convert


def _int(x: Any) -> int:
    """Guard for every number interpolated into generated source."""
    if type(x) is not int:
        raise TypeError(f"refusing to generate code from non-integer {x!r}")
    return x


class _Unpackers(dict):
    """k -> unpacker for k little-endian u16s, created on first use."""

    def __missing__(self, k: int):
        if k < 0:
            raise struct.error(f"invalid vtable length {2 * k + 4}")
        fn = self[k] = struct.Struct(f"<{k}H").unpack_from
        return fn


class _ZeroPads(dict):
    """m -> (0,) * m; only short pads are cached, so memory stays bounded."""

    def __missing__(self, m: int):
        pad = (0,) * m
        if m <= 256:
            self[m] = pad
        return pad


class _Gen:
    def __init__(self, schema: Schema, defaults: bool, enums: str, deprecated: bool, raw: bool):
        self.schema = schema
        # raw: buf is bytes/mmap, whose slices are bytes (fast .decode());
        # otherwise a memoryview, decoded with str(slice, 'utf-8').
        self.raw = raw
        self.defaults = defaults
        self.enums = enums
        self.deprecated = deprecated
        self.ns: dict[str, Any] = {
            "i32": struct.Struct("<i").unpack_from,
            "u32": struct.Struct("<I").unpack_from,
            "u16": struct.Struct("<H").unpack_from,
            "unpack_from": struct.unpack_from,
            "str": str,
            "NotImplementedError": NotImplementedError,
        }
        self.names: dict[str, str] = {}   # object name -> converter function name
        self.consts = 0
        self.src: list[str] = []
        self.pending: list[tuple[Object, str]] = []

    # -- namespace helpers --------------------------------------------------
    def const(self, value: Any, hint: str = "k") -> str:
        self.consts += 1
        name = f"{hint}{self.consts}"
        self.ns[name] = value
        return name

    def scalar_reader(self, base: BaseType) -> str:
        name = f"s_{base.name.lower()}"
        if name not in self.ns:
            self.ns[name] = struct.Struct("<" + SCALAR_FORMAT[base]).unpack_from
        return name

    def vt_reader(self, n: int) -> tuple[str, str, str]:
        """Names of: unpacker for n slots, lazy dict of shorter unpackers, lazy zero pads.

        Entries are created on demand, for vtable lengths actually seen, so a
        wide table costs nothing up front (eager tables were O(n^2)).
        """
        n = _int(n)
        full = f"vt{n}"
        if full not in self.ns:
            self.ns[full] = struct.Struct(f"<{n}H").unpack_from
        if "VT" not in self.ns:
            self.ns["VT"] = _Unpackers()
            self.ns["Z"] = _ZeroPads()
        return full, "VT", "Z"

    def enum_expr(self, enum_: Enum | None, expr: str) -> str:
        """Expression converting raw int ``expr`` per the enum mode."""
        if enum_ is None or self.enums == "value":
            return expr
        py = enum_.python_or_none
        if self.enums == "member" and py is None:
            return expr          # names unusable as a Python enum: plain ints
        if enum_.bit_flags:
            fn = py if self.enums == "member" else _flag_namer(enum_)
            return f"{self.const(fn, 'F')}({expr})"
        table = {v.value: (py(v.value) if self.enums == "member" else v.name) for v in enum_.values}
        g = self.const(table.get, "E")
        return f"{g}({expr}, {expr})" if expr.isidentifier() else f"(lambda _v: {g}(_v, _v))({expr})"

    def enum_value(self, enum_: Enum | None, raw: Any) -> Any:
        if enum_ is None or raw is None or self.enums == "value":
            return raw
        py = enum_.python_or_none
        if self.enums == "member" and py is None:
            return raw
        if enum_.bit_flags:
            return py(raw) if self.enums == "member" else _flag_namer(enum_)(raw)
        if raw in enum_.by_value:
            return py(raw) if self.enums == "member" else enum_.by_value[raw].name
        return raw

    def decode(self, start: str, end: str) -> str:
        return f"buf[{start}:{end}].decode()" if self.raw else f"str(buf[{start}:{end}], 'utf-8')"

    # -- generation -----------------------------------------------------------
    def nested_fn(self, obj: Object) -> str:
        """Converter for a nested buffer, which is always a memoryview slice."""
        if not self.raw:
            return self.fn(obj)
        holder: dict[str, Callable] = {}

        def call(buf, pos):
            f = holder.get("f")
            if f is None:
                f = holder["f"] = converter(self.schema, obj, self.defaults, self.enums, self.deprecated, False)
            return f(buf, pos)
        return self.const(call, "N")

    def fn(self, obj: Object) -> str:
        name = self.names.get(obj.name)
        if name is None:
            # Index only: schema names (arbitrary strings in a .bfbs) must
            # never be interpolated into generated source.
            name = f"c{len(self.names)}"
            self.names[obj.name] = name
            self.pending.append((obj, name))   # generated by run(), not recursively
        return name

    def run(self, obj: Object) -> str:
        """Generate converters for ``obj`` and everything it reaches (iteratively)."""
        name = self.fn(obj)
        while self.pending:
            o, n = self.pending.pop()
            (self.gen_struct if o.is_struct else self.gen_table)(o, n)
        return name

    def gen_struct(self, obj: Object, name: str) -> None:
        parts = []
        for f in obj.fields:
            parts.append(f"{f.name!r}: {self.struct_field_expr(f, 'pos + %d' % _int(f.offset))}")
        self.src.append(f"def {name}(buf, pos):\n    return {{{', '.join(parts)}}}\n")

    def struct_field_expr(self, f: Field, at: str) -> str:
        t = f.type
        if t.base.is_scalar:
            return self.enum_expr(t.enum, f"{self.scalar_reader(t.base)}(buf, {at})[0]")
        if t.base == BaseType.OBJ:
            return f"{self.fn(t.ref)}(buf, {at})"
        if t.base == BaseType.ARRAY:
            n = _int(t.fixed_length)
            if t.element == BaseType.OBJ:
                size = _int(t.ref.bytesize)
                return f"[{self.fn(t.ref)}(buf, q) for q in range({at}, {at} + {size * n}, {size})]"
            fmt = self.const("<%d%s" % (n, SCALAR_FORMAT[t.element]), "A")
            raw = f"list(unpack_from({fmt}, buf, {at}))"
            if t.enum is None or self.enums == "value":
                return raw
            return f"[{self.enum_expr(t.enum, '_x')} for _x in unpack_from({fmt}, buf, {at})]"
        raise NotImplementedError(t.base)

    def gen_table(self, obj: Object, name: str) -> None:
        if len(obj.fields) > WIDE_TABLE:
            # Compiling one branch per field costs ~13 KB per field, and a
            # hostile schema can declare 32k fields: use the generic converter.
            g = self.const(_generic_table(self.schema, obj, self.defaults, self.enums, self.deprecated), "G")
            self.src.append(f"def {name}(buf, pos):\n    return {g}(buf, pos)\n")
            return
        fields = [f for f in obj.fields if self.deprecated or not f.deprecated]
        nslots = max((_int(f.id) for f in obj.fields), default=-1) + 1
        lines = [f"def {name}(buf, pos):",
                 "    vt = pos - i32(buf, pos)[0]",
                 "    d = {}"]
        if nslots:
            full, vtd, z = self.vt_reader(nslots)
            lines += [
                "    k = (u16(buf, vt)[0] - 4) >> 1",
                # 0/2-byte vtables are valid and mean "no fields" (k <= 0)
                f"    e = {full}(buf, vt + 4) if k >= {nslots} else "
                f"({vtd}[k](buf, vt + 4) + {z}[{nslots} - k] if k > 0 else {z}[{nslots}])",
            ]
        for f in fields:
            lines += self.table_field(obj, f)
        lines.append("    return d")
        self.src.append("\n".join(lines) + "\n")

    def table_field(self, obj: Object, f: Field) -> list[str]:
        t = f.type
        base = t.base
        key = repr(f.name)
        out = [f"    o = e[{_int(f.id)}]", "    if o:"]
        body: list[str] = []
        if base.is_scalar:
            read = f"{self.scalar_reader(base)}(buf, pos + o)[0]"
            body.append(f"d[{key}] = {self.enum_expr(t.enum, '_v')}" if t.enum is not None and self.enums != "value"
                        else f"d[{key}] = {read}")
            if t.enum is not None and self.enums != "value":
                body.insert(0, f"_v = {read}")
            absent = self.enum_value(t.enum, f.default)
        elif base == BaseType.STRING:
            body += ["p = pos + o", "p += u32(buf, p)[0]",
                     f"d[{key}] = {self.decode('p + 4', 'p + 4 + u32(buf, p)[0]')}"]
            absent = None
        elif base == BaseType.OBJ and t.ref.is_struct:
            body.append(f"d[{key}] = {self.fn(t.ref)}(buf, pos + o)")
            absent = None
        elif base == BaseType.OBJ:
            body += ["p = pos + o", f"d[{key}] = {self.fn(t.ref)}(buf, p + u32(buf, p)[0])"]
            absent = None
        elif base == BaseType.UNION:
            tf = obj.fields_by_name[f.name + "_type"]
            members = self.union_members(t.ref)
            body += [f"ut = buf[pos + e[{_int(tf.id)}]] if e[{_int(tf.id)}] else 0",
                     f"m = {members}.get(ut)",
                     "p = pos + o",
                     f"d[{key}] = m(buf, p + u32(buf, p)[0]) if m is not None else None"]
            absent = None
        elif base == BaseType.VECTOR:
            body += ["p = pos + o", "p += u32(buf, p)[0]", "n = u32(buf, p)[0]"]
            body += self.vector_body(obj, f, key)
            absent = None
        else:
            body.append(f"raise NotImplementedError({obj.name + '.' + f.name + ': ' + base.name!r})")
            absent = None
        out += ["        " + b for b in body]
        if self.defaults or f.key:
            out += ["    else:", f"        d[{key}] = {self.const(absent, 'D') if absent is not None else 'None'}"]
        return out

    def union_members(self, union: Enum) -> str:
        members: dict[int, Callable] = {}
        for v in union.values:
            ut = v.union_type
            if ut is None or ut.base == BaseType.NONE:
                continue
            if ut.base == BaseType.STRING:
                members[v.value] = lambda buf, p: str(buf[p + 4:p + 4 + struct.unpack_from("<I", buf, p)[0]], "utf-8")
            else:
                members[v.value] = _Deferred(self, ut.ref)
        return self.const(members, "U")

    def vector_body(self, obj: Object, f: Field, key: str) -> list[str]:
        t = f.type
        el = t.element
        nested = f.attributes.get("nested_flatbuffer")
        if nested is not None:
            from ._reader import _resolve_in
            # nested buffers are converted by the memoryview flavour
            target = self.nested_fn(_resolve_in(self.schema, nested, obj.namespace))
            return ["nb = memoryview(buf)[p + 4:p + 4 + n]", f"d[{key}] = {target}(nb, u32(nb, 0)[0])"]
        if el.is_scalar:
            fmt = SCALAR_FORMAT[el]
            raw = f"unpack_from('<%d{fmt}' % n, buf, p + 4)"
            if t.enum is None or self.enums == "value":
                return [f"d[{key}] = list({raw})"]
            return [f"d[{key}] = [{self.enum_expr(t.enum, '_x')} for _x in {raw}]"]
        if el == BaseType.STRING:
            return [f"d[{key}] = [{self.decode('q + 4', 'q + 4 + u32(buf, q)[0]')} "
                    f"for q in [r + u32(buf, r)[0] for r in range(p + 4, p + 4 + 4 * n, 4)]]"]
        if el == BaseType.OBJ and t.ref.is_struct:
            size = _int(t.ref.bytesize)
            return [f"d[{key}] = [{self.fn(t.ref)}(buf, q) for q in range(p + 4, p + 4 + {size} * n, {size})]"]
        if el == BaseType.OBJ:
            return [f"d[{key}] = [{self.fn(t.ref)}(buf, q + u32(buf, q)[0]) for q in range(p + 4, p + 4 + 4 * n, 4)]"]
        if el == BaseType.UNION:
            tf = obj.fields_by_name[f.name + "_type"]
            members = self.union_members(t.ref)
            return [f"tp = pos + e[{_int(tf.id)}]", "tp += u32(buf, tp)[0]",
                    f"d[{key}] = [({members}.get(buf[tp + 4 + i]) or (lambda b, x: None))(buf, q + u32(buf, q)[0]) "
                    f"for i, q in enumerate(range(p + 4, p + 4 + 4 * n, 4))]"]
        return [f"raise NotImplementedError({'vector of ' + el.name!r})"]


class _Deferred:
    """Union member converter resolved after all functions are compiled."""

    __slots__ = ("gen", "obj", "fn")

    def __init__(self, gen: _Gen, obj: Object):
        self.gen = gen
        self.obj = obj
        self.fn = None
        gen.fn(obj)

    def __call__(self, buf, pos):
        fn = self.fn
        if fn is None:
            fn = self.fn = self.gen.ns[self.gen.names[self.obj.name]]
        return fn(buf, pos)


def _compile(schema: Schema, obj: Object, defaults: bool, enums: str, deprecated: bool,
             raw: bool) -> Callable:
    gen = _Gen(schema, defaults, enums, deprecated, raw)
    gen.run(obj)
    exec(compile("\n".join(gen.src), f"<flatview converters for {schema.source or 'schema'}>", "exec"), gen.ns)
    return gen.ns[gen.names[obj.name]]


def converter(schema: Schema, obj: Object, defaults: bool = True, enums: str = "member",
              deprecated: bool = False, raw: bool = False) -> Callable[[Any, int], Any]:
    """Cached ``(buf, pos) -> dict`` converter for a table/struct.

    raw=True: ``buf`` must be ``bytes`` or ``mmap`` (slices are bytes);
    otherwise any buffer, typically a memoryview.
    """
    if enums not in ("member", "name", "value"):
        raise ValueError("enums must be 'member', 'name' or 'value'")
    cache = schema.__dict__.setdefault("_converters", {})
    key = (obj.name, bool(defaults), enums, bool(deprecated), bool(raw))
    fn = cache.get(key)
    if fn is None:
        fn = cache[key] = _compile(schema, obj, bool(defaults), enums, bool(deprecated), bool(raw))
    return fn


def fast_buffer(buf: memoryview) -> tuple[Any, bool]:
    """(buffer, raw) to convert from: the underlying bytes/mmap when the view
    covers all of it (positions are then identical), else the memoryview."""
    base = buf.obj
    # A byte view as large as its exporter must start at offset 0.
    if isinstance(base, (bytes, mmap.mmap)) and not getattr(base, "closed", False) \
            and buf.nbytes == len(base):
        return base, True
    return buf, False
