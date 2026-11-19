"""Lazy, zero-copy views over FlatBuffer data, driven by a runtime schema.

Every table/struct in a schema gets a generated view class with one property
per field, so ``row.name.en`` costs one vtable probe plus one
``struct.unpack_from`` - nothing is decoded until it is touched. Vectors are
real ``collections.abc.Sequence`` objects (len, negative indices, lazy
slices, iteration) and vectors of keyed tables support O(log n) lookup.
"""

from __future__ import annotations

import copy
import enum
import struct
from collections.abc import Sequence
from typing import Any, Callable, Iterator

from ._schema import BaseType, Enum, Field, Object, Schema, SchemaError, SCALAR_FORMAT, SCALAR_SIZE

_u16 = struct.Struct("<H").unpack_from
_u32 = struct.Struct("<I").unpack_from
_i32 = struct.Struct("<i").unpack_from


class FlatBufferError(ValueError):
    """The buffer could not be interpreted with the given schema."""


def _as_buffer(data: Any) -> memoryview:
    mv = data if isinstance(data, memoryview) else memoryview(data)
    if mv.format != "B" or mv.ndim != 1:
        mv = mv.cast("B")
    return mv


def _decode(buf: memoryview, pos: int) -> str:
    """Decode the string whose length prefix is at ``pos``."""
    n = _u32(buf, pos)[0]
    start = pos + 4
    if start + n > len(buf):
        raise FlatBufferError(f"string at {pos} runs past end of buffer")
    return str(buf[start:start + n], "utf-8")


def _string_bytes(buf: memoryview, pos: int) -> bytes:
    n = _u32(buf, pos)[0]
    return bytes(buf[pos + 4:pos + 4 + n])


# ==========================================================================
# Table and struct views

class TableView:
    """Base class of all generated table views.

    Field values are plain attributes (``row.typeID``) or items
    (``row["typeID"]``). Helper methods are prefixed with ``_`` (namedtuple
    style) so they can never collide with a schema field name.
    """

    __slots__ = ("_buf", "_pos", "_vt", "_vtlen")
    _object: Object
    _schema: Schema
    _fields: tuple[str, ...]
    _getters: dict[str, Callable]
    _slots: dict[str, int]

    def __init__(self, buf: memoryview, pos: int):
        self._buf = buf
        self._pos = pos
        vt = pos - _i32(buf, pos)[0]
        self._vt = vt
        self._vtlen = _u16(buf, vt)[0]

    # -- field access ------------------------------------------------------
    def __getitem__(self, name: str) -> Any:
        try:
            getter = self._getters[name]
        except KeyError:
            raise KeyError(f"{self._object.name} has no field {name!r}") from None
        return getter(self)

    def _get(self, name: str, default: Any = None) -> Any:
        """Field value, or ``default`` if the field is absent from the buffer."""
        return self[name] if self._has(name) else default

    def _has(self, name: str) -> bool:
        """True if the field is physically present in the buffer (not defaulted)."""
        try:
            slot = self._slots[name]
        except KeyError:
            raise KeyError(f"{self._object.name} has no field {name!r}") from None
        return slot < self._vtlen and _u16(self._buf, self._vt + slot)[0] != 0

    def _field_offset(self, name: str) -> int | None:
        """Absolute buffer offset of a field's inline data, or None if absent."""
        slot = self._slots[name]
        if slot < self._vtlen:
            o = _u16(self._buf, self._vt + slot)[0]
            if o:
                return self._pos + o
        return None

    @property
    def _offset(self) -> int:
        """Absolute offset of this table in the buffer."""
        return self._pos

    def _asdict(self, *, defaults: bool = True, enums: str = "member",
                deprecated: bool = False) -> dict[str, Any]:
        """Recursively convert to plain Python data (dicts, lists, scalars).

        defaults:   include absent fields at their default value (scalars) or
                    None (everything else). With ``defaults=False`` only fields
                    present in the buffer are emitted, like ``flatc --json``.
        enums:      "member" (IntEnum members), "name" (str) or "value" (int).
        deprecated: include deprecated fields.
        """
        return _to_python(self, defaults, enums, deprecated)

    # -- protocol niceties -------------------------------------------------
    def __eq__(self, other: object) -> bool:
        if isinstance(other, TableView):
            return (self._object is other._object and self._pos == other._pos
                    and self._buf.obj is other._buf.obj)
        return NotImplemented

    def __hash__(self) -> int:
        return hash((id(self._object), self._pos, id(self._buf.obj)))

    def __dir__(self):
        return sorted(set(super().__dir__()) | set(self._fields))

    def __repr__(self) -> str:
        return _short_repr(self, 2)


class StructView:
    """Base class of generated struct views.

    Structs are values: they compare equal by content, iterate over their
    fields (so ``x, y, z = row.position`` works) and compare equal to tuples.
    """

    __slots__ = ("_buf", "_pos")
    _object: Object
    _schema: Schema
    _fields: tuple[str, ...]
    _getters: dict[str, Callable]

    def __init__(self, buf: memoryview, pos: int):
        self._buf = buf
        self._pos = pos

    def __getitem__(self, name: str | int) -> Any:
        if isinstance(name, int):
            name = self._fields[name]
        try:
            return self._getters[name](self)
        except KeyError:
            raise KeyError(f"{self._object.name} has no field {name!r}") from None

    def __iter__(self) -> Iterator[Any]:
        for name in self._fields:
            yield self._getters[name](self)

    def __len__(self) -> int:
        return len(self._fields)

    def _astuple(self) -> tuple:
        return tuple(_plain(v, True, "member", False) for v in self)

    def _asdict(self, *, defaults: bool = True, enums: str = "member",
                deprecated: bool = False) -> dict[str, Any]:
        return _to_python(self, defaults, enums, deprecated)

    def _bytes(self) -> bytes:
        return bytes(self._buf[self._pos:self._pos + self._object.bytesize])

    @property
    def _offset(self) -> int:
        return self._pos

    def __eq__(self, other: object) -> bool:
        if isinstance(other, StructView):
            return self._object is other._object and self._bytes() == other._bytes()
        if isinstance(other, tuple):
            return self._astuple() == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self._object.name, self._bytes()))

    def __dir__(self):
        return sorted(set(super().__dir__()) | set(self._fields))

    def __repr__(self) -> str:
        inner = ", ".join(f"{n}={_short_repr(v, 0)}" for n, v in zip(self._fields, self))
        return f"{self._object.short_name}({inner})"


# ==========================================================================
# Vectors

class Vector(Sequence):
    """A lazy, read-only view of a FlatBuffer vector (or struct array)."""

    __slots__ = ("_buf", "_start", "_len")
    _stride = 4

    def __init__(self, buf: memoryview, start: int, length: int):
        self._buf = buf
        self._start = start
        self._len = length
        end = start + length * self._stride
        if length < 0 or end > len(buf):
            raise FlatBufferError(f"vector at {start} with {length} elements runs past end of buffer")

    def __len__(self) -> int:
        return self._len

    def _at(self, i: int) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError

    def __getitem__(self, i):
        if isinstance(i, slice):
            start, stop, step = i.indices(self._len)
            if step == 1:
                new = copy.copy(self)
                new._start = self._start + start * self._stride
                new._len = max(0, stop - start)
                return new
            return [self._at(k) for k in range(start, stop, step)]
        if i < 0:
            i += self._len
        if not 0 <= i < self._len:
            raise IndexError("vector index out of range")
        return self._at(i)

    def __iter__(self) -> Iterator[Any]:
        at = self._at
        for i in range(self._len):
            yield at(i)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (Sequence, Vector)) and not isinstance(other, (str, bytes)):
            return len(self) == len(other) and all(a == b for a, b in zip(self, other))
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]

    def tolist(self) -> list:
        return list(self)

    def _aslist(self, *, defaults: bool = True, enums: str = "member", deprecated: bool = False) -> list:
        return [_plain(v, defaults, enums, deprecated) for v in self]

    def __repr__(self) -> str:
        if self._len <= 6:
            items = ", ".join(_short_repr(v, 1) for v in self)
        else:
            items = ", ".join(_short_repr(self._at(k), 1) for k in range(3)) + f", ... ({self._len} items)"
        return f"[{items}]"


class ScalarVector(Vector):
    """Vector of scalars / enums. ``tolist()`` decodes in one C call."""

    __slots__ = ("_fmt", "_unpack", "_convert", "_stride")

    def __init__(self, buf, start, length, base: BaseType, convert: Callable | None = None):
        self._fmt = SCALAR_FORMAT[base]
        self._stride = SCALAR_SIZE[base]
        self._unpack = struct.Struct("<" + self._fmt).unpack_from
        self._convert = convert
        super().__init__(buf, start, length)

    def _at(self, i: int) -> Any:
        v = self._unpack(self._buf, self._start + i * self._stride)[0]
        return self._convert(v) if self._convert else v

    def tolist(self) -> list:
        vals = list(struct.unpack_from(f"<{self._len}{self._fmt}", self._buf, self._start))
        return [self._convert(v) for v in vals] if self._convert else vals

    def __iter__(self):
        return iter(self.tolist())

    def tobytes(self) -> bytes:
        """The raw little-endian element bytes."""
        return bytes(self._buf[self._start:self._start + self._len * self._stride])

    def _aslist(self, *, defaults=True, enums="member", deprecated=False) -> list:
        if self._convert is None:
            return self.tolist()
        return [_plain(v, defaults, enums, deprecated) for v in self.tolist()]


class StringVector(Vector):
    __slots__ = ()

    def _at(self, i: int) -> str:
        p = self._start + 4 * i
        return _decode(self._buf, p + _u32(self._buf, p)[0])

    def _aslist(self, **kw) -> list:
        return list(self)


class StructVector(Vector):
    __slots__ = ("_cls", "_stride")

    def __init__(self, buf, start, length, cls: type):
        self._cls = cls
        self._stride = cls._object.bytesize
        super().__init__(buf, start, length)

    def _at(self, i: int):
        return self._cls(self._buf, self._start + i * self._stride)

    def _aslist(self, *, defaults=True, enums="member", deprecated=False) -> list:
        from ._todict import converter
        conv = converter(self._cls._schema, self._cls._object, defaults, enums, deprecated)
        s = self._stride
        return [conv(self._buf, p) for p in range(self._start, self._start + s * self._len, s)]


class TableVector(Vector):
    """Vector of tables. If the table has a ``key`` field, supports lookup."""

    __slots__ = ("_cls",)

    def __init__(self, buf, start, length, cls: type):
        self._cls = cls
        super().__init__(buf, start, length)

    def _pos_at(self, i: int) -> int:
        p = self._start + 4 * i
        return p + _u32(self._buf, p)[0]

    def _at(self, i: int):
        return self._cls(self._buf, self._pos_at(i))

    def _aslist(self, *, defaults=True, enums="member", deprecated=False) -> list:
        from ._todict import converter, fast_buffer
        buf, raw = fast_buffer(self._buf)
        conv = converter(self._cls._schema, self._cls._object, defaults, enums, deprecated, raw)
        return [conv(buf, p + _u32(buf, p)[0]) for p in range(self._start, self._start + 4 * self._len, 4)]

    # -- keyed access ------------------------------------------------------
    @property
    def key_field(self) -> Field | None:
        return self._cls._object.key_field

    def _key_reader(self) -> Callable[[memoryview, int], Any]:
        kr = self._cls._key_reader
        if kr is None:
            raise TypeError(f"{self._cls._object.name} has no key field; use index_by(field) instead")
        return kr

    def _normalise_key(self, key: Any) -> Any:
        if self.key_field.type.base == BaseType.STRING:
            if isinstance(key, str):
                return key.encode("utf-8")
            return bytes(key)
        return key

    def bisect(self, key: Any) -> int:
        """Index of the first element whose key is >= ``key`` (vector must be sorted)."""
        kr = self._key_reader()
        key = self._normalise_key(key)
        buf, lo, hi = self._buf, 0, self._len
        while lo < hi:
            mid = (lo + hi) // 2
            if kr(buf, self._pos_at(mid)) < key:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def find(self, key: Any, default: Any = None) -> Any:
        """Binary-search for the element with this key; ``default`` if missing."""
        i = self.bisect(key)
        if i < self._len and self._key_reader()(self._buf, self._pos_at(i)) == self._normalise_key(key):
            return self._at(i)
        return default

    def lookup(self, key: Any):
        """Binary-search for the element with this key; KeyError if missing.

        FlatBuffers requires vectors of keyed tables to be sorted by key for
        lookups (writers use CreateVectorOfSortedTables). Use
        :meth:`is_sorted` or ``verify(check_sorted=True)`` on untrusted data.
        """
        sentinel = _MISSING
        found = self.find(key, sentinel)
        if found is sentinel:
            raise KeyError(key)
        return found

    def has_key(self, key: Any) -> bool:
        return self.find(key, _MISSING) is not _MISSING

    def keys(self) -> list:
        """All key values, in vector order."""
        kr = self._key_reader()
        is_str = self.key_field.type.base == BaseType.STRING
        out = [kr(self._buf, self._pos_at(i)) for i in range(self._len)]
        return [k.decode("utf-8") for k in out] if is_str else out

    def is_sorted(self) -> bool:
        kr = self._key_reader()
        prev = _MISSING
        for i in range(self._len):
            k = kr(self._buf, self._pos_at(i))
            if prev is not _MISSING and k < prev:
                return False
            prev = k
        return True

    def index_by(self, field: str | Callable[[Any], Any]) -> dict:
        """Build a dict {value: element} on any field (or key function)."""
        if callable(field):
            return {field(row): row for row in self}
        getter = self._cls._getters[field]
        return {getter(row): row for row in self}

    def group_by(self, field: str | Callable[[Any], Any]) -> dict[Any, list]:
        """Build a dict {value: [elements...]} on any field (or key function)."""
        getter = field if callable(field) else self._cls._getters[field]
        out: dict[Any, list] = {}
        for row in self:
            out.setdefault(getter(row), []).append(row)
        return out


class UnionVector(Vector):
    __slots__ = ("_types", "_members")

    def __init__(self, buf, start, length, types: ScalarVector, members: dict[int, Callable]):
        self._types = types
        self._members = members
        super().__init__(buf, start, length)

    def _at(self, i: int):
        t = int(self._types[i])
        make = self._members.get(t)
        if make is None:
            return None
        p = self._start + 4 * i
        return make(self._buf, p + _u32(self._buf, p)[0])


_MISSING = object()


# ==========================================================================
# Code generation of view classes

def view_class(schema: Schema, obj: Object) -> type:
    """The (cached) generated view class for a table/struct."""
    cls = obj.view_class
    if cls is None:
        cls = _build_struct_class(schema, obj) if obj.is_struct else _build_table_class(schema, obj)
    return cls


def _enum_converter(enum: Enum | None) -> Callable | None:
    if enum is None:
        return None
    members = {}
    py = enum.python_or_none
    if py is None:
        return None          # names unusable as a Python enum: plain ints
    for v in enum.values:
        members[v.value] = py(v.value)
    get = members.get

    if enum.bit_flags:
        def conv(v):
            m = get(v)
            return m if m is not None else py(v)
    else:
        def conv(v):
            m = get(v)
            return v if m is None else m
    return conv


def _build_table_class(schema: Schema, obj: Object) -> type:
    ns: dict[str, Any] = {"__slots__": (), "_object": obj, "_schema": schema}
    cls = type(obj.short_name, (TableView,), ns)
    cls.__qualname__ = obj.name
    obj.view_class = cls  # set early: recursive schemas reference themselves
    getters: dict[str, Callable] = {}
    slots: dict[str, int] = {}
    for f in obj.fields:
        g = _table_getter(schema, obj, f)
        getters[f.name] = g
        slots[f.name] = f.offset
        if _attribute_ok(f.name, TableView):
            setattr(cls, f.name, property(g, doc=_field_doc(f)))
    cls._getters = getters
    cls._slots = slots
    cls._fields = tuple(f.name for f in obj.fields)
    cls._key_reader = _key_reader(obj) if obj.key_field else None
    return cls


def _attribute_ok(name: str, base: type) -> bool:
    """Whether a field may become a view attribute. Never shadow helpers, and
    never take double-underscore names: as class attributes they would replace
    Python protocol methods (``__len__``, ``__iter__``...). Such fields stay
    reachable as ``view["name"]``."""
    return not name.startswith("__") and not hasattr(base, name)


def _field_doc(f: Field) -> str:
    doc = f"{f.name}: {f.type.describe()}"
    if f.documentation:
        doc += "\n\n" + "\n".join(d.strip() for d in f.documentation)
    return doc


def _key_reader(obj: Object) -> Callable[[memoryview, int], Any]:
    """Fast raw reader for the key field: (buf, table_pos) -> comparable value."""
    f = obj.key_field
    slot = f.offset
    if f.type.base == BaseType.STRING:
        def read(buf, pos):
            vt = pos - _i32(buf, pos)[0]
            if slot < _u16(buf, vt)[0]:
                o = _u16(buf, vt + slot)[0]
                if o:
                    p = pos + o
                    return _string_bytes(buf, p + _u32(buf, p)[0])
            return b""
        return read
    if not f.type.base.is_scalar:
        raise SchemaError(f"key field {obj.name}.{f.name} must be a scalar or string")
    unpack = struct.Struct("<" + SCALAR_FORMAT[f.type.base]).unpack_from
    default = f.default if f.default is not None else 0

    def read(buf, pos):
        vt = pos - _i32(buf, pos)[0]
        if slot < _u16(buf, vt)[0]:
            o = _u16(buf, vt + slot)[0]
            if o:
                return unpack(buf, pos + o)[0]
        return default
    return read


def _table_getter(schema: Schema, obj: Object, f: Field) -> Callable:
    slot = f.offset
    t = f.type
    base = t.base

    def locate(self) -> int:
        """Absolute position of the field's inline data, or 0 if absent."""
        if slot < self._vtlen:
            o = _u16(self._buf, self._vt + slot)[0]
            if o:
                return self._pos + o
        return 0

    if base.is_scalar:
        unpack = struct.Struct("<" + SCALAR_FORMAT[base]).unpack_from
        conv = _enum_converter(t.enum)
        default = f.default
        if conv is not None and default is not None:
            default = conv(default)

        if conv is None:
            def get(self):
                if slot < self._vtlen:
                    o = _u16(self._buf, self._vt + slot)[0]
                    if o:
                        return unpack(self._buf, self._pos + o)[0]
                return default
        else:
            def get(self):
                if slot < self._vtlen:
                    o = _u16(self._buf, self._vt + slot)[0]
                    if o:
                        return conv(unpack(self._buf, self._pos + o)[0])
                return default
        return get

    if base == BaseType.STRING:
        def get(self):
            if slot < self._vtlen:
                o = _u16(self._buf, self._vt + slot)[0]
                if o:
                    p = self._pos + o
                    return _decode(self._buf, p + _u32(self._buf, p)[0])
            return None
        return get

    if base == BaseType.OBJ:
        sub = t.ref
        if sub.is_struct:
            def get(self):
                p = locate(self)
                return view_class(schema, sub)(self._buf, p) if p else None
        else:
            def get(self):
                p = locate(self)
                if not p:
                    return None
                return view_class(schema, sub)(self._buf, p + _u32(self._buf, p)[0])
        return get

    if base == BaseType.UNION:
        type_field = obj.fields_by_name.get(f.name + "_type")
        if type_field is None:
            raise SchemaError(f"union field {obj.name}.{f.name} has no {f.name}_type field")
        type_slot = type_field.offset
        members = _union_makers(schema, t.ref)

        def get(self):
            p = locate(self)
            if not p:
                return None
            ut = 0
            if type_slot < self._vtlen:
                o = _u16(self._buf, self._vt + type_slot)[0]
                if o:
                    ut = self._buf[self._pos + o]
            make = members.get(ut)
            if make is None:
                return None
            return make(self._buf, p + _u32(self._buf, p)[0])
        return get

    if base == BaseType.VECTOR:
        el = t.element
        nested = f.attributes.get("nested_flatbuffer")
        if nested is not None:
            target = _resolve_in(schema, nested, obj.namespace)

            def get(self):
                p = locate(self)
                if not p:
                    return None
                vp = p + _u32(self._buf, p)[0]
                n = _u32(self._buf, vp)[0]
                inner = self._buf[vp + 4:vp + 4 + n]
                return read(schema, inner, target, size_prefixed=False, check_identifier=False)
            return get

        if el.is_scalar:
            conv = _enum_converter(t.enum)

            def make_vec(buf, vp):
                return ScalarVector(buf, vp + 4, _u32(buf, vp)[0], el, conv)
        elif el == BaseType.STRING:
            def make_vec(buf, vp):
                return StringVector(buf, vp + 4, _u32(buf, vp)[0])
        elif el == BaseType.OBJ:
            sub = t.ref
            if sub.is_struct:
                def make_vec(buf, vp):
                    return StructVector(buf, vp + 4, _u32(buf, vp)[0], view_class(schema, sub))
            else:
                def make_vec(buf, vp):
                    return TableVector(buf, vp + 4, _u32(buf, vp)[0], view_class(schema, sub))
        elif el == BaseType.UNION:
            type_field = obj.fields_by_name.get(f.name + "_type")
            if type_field is None:
                raise SchemaError(f"union vector {obj.name}.{f.name} has no {f.name}_type field")
            members = _union_makers(schema, t.ref)
            type_getter = None

            def get(self):
                nonlocal type_getter
                p = locate(self)
                if not p:
                    return None
                if type_getter is None:
                    type_getter = type(self)._getters[type_field.name]
                types = type_getter(self)
                vp = p + _u32(self._buf, p)[0]
                n = _u32(self._buf, vp)[0]
                if types is None or len(types) != n:
                    raise FlatBufferError(f"{obj.name}.{f.name}: union type vector length mismatch")
                return UnionVector(self._buf, vp + 4, n, types, members)
            return get
        else:
            raise SchemaError(f"unsupported vector element type {el.name} in {obj.name}.{f.name}")

        def get(self):
            p = locate(self)
            if not p:
                return None
            return make_vec(self._buf, p + _u32(self._buf, p)[0])
        return get

    if base == BaseType.VECTOR64:
        def get(self):
            raise NotImplementedError(f"{obj.name}.{f.name}: 64-bit vectors are not supported yet")
        return get

    raise SchemaError(f"unsupported field type {base.name} in {obj.name}.{f.name}")


def _union_makers(schema: Schema, union: Enum) -> dict[int, Callable]:
    makers: dict[int, Callable] = {}
    for v in union.values:
        ut = v.union_type
        if ut is None or ut.base == BaseType.NONE:
            continue
        if ut.base == BaseType.STRING:
            makers[v.value] = _decode
        elif ut.base == BaseType.OBJ:
            sub = ut.ref

            def make(buf, pos, _sub=sub):
                return view_class(schema, _sub)(buf, pos)
            makers[v.value] = make
    return makers


def _resolve_in(schema: Schema, name: str, ns: str) -> Object:
    parts = ns.split(".") if ns else []
    for i in range(len(parts), -1, -1):
        cand = ".".join(parts[:i] + [name])
        if cand in schema.objects:
            return schema.objects[cand]
    return schema.object(name)


def _build_struct_class(schema: Schema, obj: Object) -> type:
    ns: dict[str, Any] = {"__slots__": (), "_object": obj, "_schema": schema}
    cls = type(obj.short_name, (StructView,), ns)
    cls.__qualname__ = obj.name
    obj.view_class = cls
    getters: dict[str, Callable] = {}
    for f in obj.fields:
        g = _struct_getter(schema, f)
        getters[f.name] = g
        if _attribute_ok(f.name, StructView):
            setattr(cls, f.name, property(g, doc=_field_doc(f)))
    cls._getters = getters
    cls._fields = tuple(f.name for f in obj.fields)
    return cls


def _struct_getter(schema: Schema, f: Field) -> Callable:
    off = f.offset
    t = f.type
    if t.base.is_scalar:
        unpack = struct.Struct("<" + SCALAR_FORMAT[t.base]).unpack_from
        conv = _enum_converter(t.enum)
        if conv is None:
            return lambda self: unpack(self._buf, self._pos + off)[0]
        return lambda self: conv(unpack(self._buf, self._pos + off)[0])
    if t.base == BaseType.OBJ:
        sub = t.ref
        return lambda self: view_class(schema, sub)(self._buf, self._pos + off)
    if t.base == BaseType.ARRAY:
        n = t.fixed_length
        if t.element == BaseType.OBJ:
            sub = t.ref
            return lambda self: StructVector(self._buf, self._pos + off, n, view_class(schema, sub))
        conv = _enum_converter(t.enum)
        el = t.element
        return lambda self: ScalarVector(self._buf, self._pos + off, n, el, conv)
    raise SchemaError(f"unsupported struct field type {t.base.name}")


# ==========================================================================
# Conversion to plain Python

def _plain(v: Any, defaults: bool, enums: str, deprecated: bool) -> Any:
    if isinstance(v, (TableView, StructView)):
        return _to_python(v, defaults, enums, deprecated)
    if isinstance(v, Vector):
        return v._aslist(defaults=defaults, enums=enums, deprecated=deprecated)
    if isinstance(v, int) and type(v) not in (int, bool):
        # enum member
        if enums == "name":
            if isinstance(v, enum.Flag):
                # flatc's JSON form for bit flags: space-separated names, or
                # the number when it has bits no member defines
                known = 0
                for m in type(v):
                    known |= m.value
                if not v or int(v) & ~known:
                    return int(v)
                return " ".join(m.name for m in type(v) if m.value and (v & m) == m)
            return v.name if getattr(v, "name", None) else int(v)
        if enums == "value":
            return int(v)
    return v


def _to_python(view: TableView | StructView, defaults: bool, enums: str, deprecated: bool) -> dict:
    # Compiled per-table converters (see _todict). Key fields are always
    # emitted, as flatc --json does: writers omit a key equal to its default,
    # but it is still the row's identity.
    from ._todict import converter, fast_buffer
    buf, raw = fast_buffer(view._buf)
    return converter(view._schema, view._object, defaults, enums, deprecated, raw)(buf, view._pos)


def to_python(value: Any, *, defaults: bool = True, enums: str = "member",
              deprecated: bool = False) -> Any:
    """Convert any view (table, struct, vector) to plain Python data."""
    return _plain(value, defaults, enums, deprecated)


def _short_repr(v: Any, depth: int) -> str:
    if isinstance(v, TableView):
        if depth <= 0:
            return f"<{v._object.short_name}>"
        parts = []
        for f in v._object.fields:
            if f.deprecated or not v._has(f.name):
                continue
            parts.append(f"{f.name}={_short_repr(v._getters[f.name](v), depth - 1)}")
            if len(parts) >= 12:
                parts.append("...")
                break
        return f"<{v._object.short_name} {' '.join(parts)}>"
    if isinstance(v, Vector):
        if depth <= 0:
            return f"[... {len(v)} items]"
        return repr(v)
    if isinstance(v, str) and len(v) > 60:
        return repr(v[:57] + "...")
    return repr(v)


# ==========================================================================
# Entry point

def has_identifier(data: Any, ident: str | bytes, size_prefixed: bool = False) -> bool:
    """True if the buffer carries the given 4-byte file identifier."""
    buf = _as_buffer(data)
    ident = ident.encode() if isinstance(ident, str) else ident
    base = 4 if size_prefixed else 0
    return len(buf) >= base + 8 and bytes(buf[base + 4:base + 8]) == ident


def read(schema: Schema, data: Any, root: str | Object | None = None, *, verify: bool = False,
         size_prefixed: bool = False, check_identifier: bool = True) -> TableView:
    """View the root table of ``data`` (bytes, bytearray, mmap, memoryview).

    size_prefixed: the buffer starts with a 4-byte length (FinishSizePrefixed).
                   Never guessed: a valid buffer's root offset can equal its
                   length - 4, so auto-detection would misread real data.
    verify:        run the full verifier first (recommended for untrusted data).
    """
    buf = _as_buffer(data)
    obj = schema.object(root)
    if obj.is_struct:
        raise SchemaError(f"root type {obj.name} must be a table")
    if size_prefixed:
        n = _u32(buf, 0)[0]
        if n + 4 > len(buf):
            raise FlatBufferError(f"size prefix {n} exceeds buffer length {len(buf)}")
        buf = buf[4:4 + n]
    if len(buf) < 8:
        raise FlatBufferError(f"buffer too small ({len(buf)} bytes) to be a FlatBuffer")
    if check_identifier and schema.file_ident and root is None:
        if bytes(buf[4:8]) != schema.file_ident.encode():
            raise FlatBufferError(f"file identifier {bytes(buf[4:8])!r} does not match schema's "
                                  f"{schema.file_ident!r}")
    if verify:
        from ._verify import verify as _verify
        _verify(schema, buf, obj, size_prefixed=False, check_identifier=False)
    pos = _u32(buf, 0)[0]
    if pos + 4 > len(buf):
        raise FlatBufferError(f"root offset {pos} is outside the buffer")
    return view_class(schema, obj)(buf, pos)
