"""In-memory schema model shared by the .fbs parser, the .bfbs loader, the
reader, the verifier and the builder.

The model deliberately mirrors ``reflection.fbs`` (what flatc writes into a
.bfbs), so both schema sources produce identical objects.
"""

from __future__ import annotations

import enum
import struct
from dataclasses import dataclass, field as dc_field
from typing import Any, Iterator


class BaseType(enum.IntEnum):
    NONE = 0
    UTYPE = 1
    BOOL = 2
    BYTE = 3
    UBYTE = 4
    SHORT = 5
    USHORT = 6
    INT = 7
    UINT = 8
    LONG = 9
    ULONG = 10
    FLOAT = 11
    DOUBLE = 12
    STRING = 13
    VECTOR = 14
    OBJ = 15
    UNION = 16
    ARRAY = 17
    VECTOR64 = 18

    @property
    def is_scalar(self) -> bool:
        return BaseType.UTYPE <= self <= BaseType.DOUBLE

    @property
    def is_integer(self) -> bool:
        return BaseType.UTYPE <= self <= BaseType.ULONG

    @property
    def is_float(self) -> bool:
        return self in (BaseType.FLOAT, BaseType.DOUBLE)


#: struct-module format character for each scalar base type (little endian).
SCALAR_FORMAT = {
    BaseType.UTYPE: "B",
    BaseType.BOOL: "?",
    BaseType.BYTE: "b",
    BaseType.UBYTE: "B",
    BaseType.SHORT: "h",
    BaseType.USHORT: "H",
    BaseType.INT: "i",
    BaseType.UINT: "I",
    BaseType.LONG: "q",
    BaseType.ULONG: "Q",
    BaseType.FLOAT: "f",
    BaseType.DOUBLE: "d",
}

SCALAR_SIZE = {bt: struct.calcsize("<" + f) for bt, f in SCALAR_FORMAT.items()}

#: Inclusive (min, max) for each integer base type.
INT_RANGE = {
    bt: ((-(1 << (8 * SCALAR_SIZE[bt] - 1)), (1 << (8 * SCALAR_SIZE[bt] - 1)) - 1)
         if f.islower() else (0, (1 << (8 * SCALAR_SIZE[bt])) - 1))
    for bt, f in SCALAR_FORMAT.items() if bt.is_integer
}
INT_RANGE[BaseType.BOOL] = (0, 1)

#: Names accepted in .fbs for each scalar/base type.
TYPE_NAMES = {
    "bool": BaseType.BOOL,
    "byte": BaseType.BYTE, "int8": BaseType.BYTE,
    "ubyte": BaseType.UBYTE, "uint8": BaseType.UBYTE,
    "short": BaseType.SHORT, "int16": BaseType.SHORT,
    "ushort": BaseType.USHORT, "uint16": BaseType.USHORT,
    "int": BaseType.INT, "int32": BaseType.INT,
    "uint": BaseType.UINT, "uint32": BaseType.UINT,
    "long": BaseType.LONG, "int64": BaseType.LONG,
    "ulong": BaseType.ULONG, "uint64": BaseType.ULONG,
    "float": BaseType.FLOAT, "float32": BaseType.FLOAT,
    "double": BaseType.DOUBLE, "float64": BaseType.DOUBLE,
    "string": BaseType.STRING,
}


class SchemaError(Exception):
    """The schema is malformed, or a name could not be resolved."""


@dataclass(eq=False)
class Type:
    """A field's type.

    ``base`` is the outer type. For VECTOR/ARRAY, ``element`` is the item type.
    ``ref`` is the Object (table/struct) or Enum (enum/union) the type points
    at, for OBJ/UNION/UTYPE or enum-typed scalars (or their vector elements).
    """

    base: BaseType
    element: BaseType = BaseType.NONE
    ref: "Object | Enum | None" = None
    fixed_length: int = 0

    @property
    def item(self) -> BaseType:
        """The type of a single value: ``element`` for vectors/arrays, else ``base``."""
        return self.element if self.base in (BaseType.VECTOR, BaseType.VECTOR64, BaseType.ARRAY) else self.base

    @property
    def enum(self) -> "Enum | None":
        return self.ref if isinstance(self.ref, Enum) else None

    @property
    def object(self) -> "Object | None":
        return self.ref if isinstance(self.ref, Object) else None

    def describe(self) -> str:
        def one(bt: BaseType) -> str:
            if self.ref is not None and bt in (BaseType.OBJ, BaseType.UNION) or (
                    self.ref is not None and bt.is_scalar and bt != BaseType.UTYPE):
                return self.ref.name
            if bt == BaseType.UTYPE:
                return f"{self.ref.name if self.ref else '?'}_type"
            return bt.name.lower()
        if self.base in (BaseType.VECTOR, BaseType.VECTOR64):
            return f"[{one(self.element)}]"
        if self.base == BaseType.ARRAY:
            return f"[{one(self.element)}:{self.fixed_length}]"
        return one(self.base)


@dataclass(eq=False)
class Field:
    name: str
    type: Type
    id: int = 0
    #: For tables: the vtable slot offset (4 + 2*id). For structs: byte offset.
    offset: int = 0
    #: Default for scalars (int/float/bool), ``None`` for optional scalars
    #: and for non-scalar fields.
    default: Any = None
    deprecated: bool = False
    required: bool = False
    key: bool = False
    optional: bool = False
    #: Struct-only: padding bytes after this field.
    padding: int = 0
    attributes: dict[str, str] = dc_field(default_factory=dict)
    documentation: list[str] = dc_field(default_factory=list)

    def __repr__(self) -> str:
        flags = [f for f in ("required", "key", "optional", "deprecated") if getattr(self, f)]
        extra = f" = {self.default!r}" if self.type.base.is_scalar and not self.optional else ""
        return f"<Field {self.name}: {self.type.describe()}{extra} (id {self.id}{', ' if flags else ''}{', '.join(flags)})>"


@dataclass(eq=False)
class Object:
    """A table or struct definition."""

    name: str
    fields: list[Field] = dc_field(default_factory=list)
    is_struct: bool = False
    minalign: int = 1
    bytesize: int = 0
    attributes: dict[str, str] = dc_field(default_factory=dict)
    documentation: list[str] = dc_field(default_factory=list)
    declaration_file: str | None = None

    def __post_init__(self) -> None:
        self._by_name: dict[str, Field] | None = None
        self._key: Field | None | bool = False
        self.view_class: type | None = None  # filled lazily by the reader

    @property
    def short_name(self) -> str:
        return self.name.rpartition(".")[2]

    @property
    def namespace(self) -> str:
        return self.name.rpartition(".")[0]

    @property
    def fields_by_name(self) -> dict[str, Field]:
        if self._by_name is None:
            self._by_name = {f.name: f for f in self.fields}
        return self._by_name

    def field(self, name: str) -> Field:
        try:
            return self.fields_by_name[name]
        except KeyError:
            raise KeyError(f"{self.name} has no field {name!r}") from None

    @property
    def key_field(self) -> Field | None:
        if self._key is False:
            self._key = next((f for f in self.fields if f.key), None)
        return self._key  # type: ignore[return-value]

    def __repr__(self) -> str:
        kind = "struct" if self.is_struct else "table"
        return f"<{kind} {self.name} ({len(self.fields)} fields)>"

    def __iter__(self) -> Iterator[Field]:
        return iter(self.fields)


@dataclass(eq=False)
class EnumVal:
    name: str
    value: int
    #: For union members: the member's type (OBJ table, or NONE for NONE).
    union_type: Type | None = None
    attributes: dict[str, str] = dc_field(default_factory=dict)
    documentation: list[str] = dc_field(default_factory=list)


@dataclass(eq=False)
class Enum:
    """An enum, or (``is_union``) a union."""

    name: str
    values: list[EnumVal] = dc_field(default_factory=list)
    is_union: bool = False
    underlying: BaseType = BaseType.INT
    attributes: dict[str, str] = dc_field(default_factory=dict)
    documentation: list[str] = dc_field(default_factory=list)
    declaration_file: str | None = None

    def __post_init__(self) -> None:
        self._py: type | None = None
        self._by_value: dict[int, EnumVal] | None = None
        self._by_name: dict[str, EnumVal] | None = None

    @property
    def short_name(self) -> str:
        return self.name.rpartition(".")[2]

    @property
    def bit_flags(self) -> bool:
        return "bit_flags" in self.attributes

    @property
    def by_value(self) -> dict[int, EnumVal]:
        if self._by_value is None:
            self._by_value = {v.value: v for v in reversed(self.values)}
        return self._by_value

    @property
    def by_name(self) -> dict[str, EnumVal]:
        if self._by_name is None:
            self._by_name = {v.name: v for v in self.values}
        return self._by_name

    @property
    def python(self) -> type:
        """A Python ``IntEnum`` (``IntFlag`` for bit_flags) for this enum.

        Raises SchemaError if a value name is one Python's enum module
        reserves (``_missing_``, ``mro``, dunders...); readers then fall back
        to plain ints for this enum.
        """
        py = self.python_or_none
        if py is None:
            raise SchemaError(f"enum {self.name} cannot be a Python enum: {self._py_error}")
        return py

    @property
    def python_or_none(self) -> type | None:
        if self._py is None:
            try:
                members = [(v.name, v.value) for v in self.values]
                if self.bit_flags:
                    py = enum.IntFlag(self.short_name, members, boundary=enum.KEEP)
                else:
                    py = enum.IntEnum(self.short_name, members)
                for v in self.values:
                    m = py.__members__.get(v.name)
                    if m is None or int(m) != v.value:
                        raise ValueError(f"value name {v.name!r} is reserved by Python's enum module")
                py.__qualname__ = self.name
                self._py = py
            except (ValueError, TypeError) as e:
                self._py = False
                self._py_error = str(e)
        return self._py or None

    def convert(self, value: int) -> Any:
        """Map a raw integer to the Python enum member (raw int if unknown)."""
        py = self.python_or_none
        if py is None:
            return value
        try:
            return py(value)
        except ValueError:
            return value

    def __repr__(self) -> str:
        kind = "union" if self.is_union else "enum"
        return f"<{kind} {self.name} ({len(self.values)} values)>"


@dataclass(eq=False)
class Schema:
    objects: dict[str, Object] = dc_field(default_factory=dict)
    enums: dict[str, Enum] = dc_field(default_factory=dict)
    root: Object | None = None
    file_ident: str | None = None
    file_ext: str | None = None
    #: Where the schema came from (path or description), for messages.
    source: str | None = None

    # -- lookup --------------------------------------------------------------
    def __getitem__(self, name: str) -> Object | Enum:
        """Find a table/struct/enum by fully qualified *or* short name."""
        if name in self.objects:
            return self.objects[name]
        if name in self.enums:
            return self.enums[name]
        hits = [d for d in (*self.objects.values(), *self.enums.values())
                if d.name.rpartition(".")[2] == name]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise KeyError(f"{name!r} is ambiguous: {sorted(h.name for h in hits)}")
        raise KeyError(f"no table, struct or enum named {name!r}")

    def object(self, name: str | Object | None) -> Object:
        if name is None:
            if self.root is None:
                raise SchemaError("schema has no root_type; pass the table to use explicitly")
            return self.root
        if isinstance(name, Object):
            return name
        obj = self[name]
        if not isinstance(obj, Object):
            raise KeyError(f"{name!r} is an enum, not a table or struct")
        return obj

    def __repr__(self) -> str:
        root = self.root.name if self.root else None
        return f"<Schema root={root} objects={len(self.objects)} enums={len(self.enums)}>"

    # -- convenience entry points (implemented in other modules) -------------
    def read(self, data: Any, root: str | Object | None = None, *, verify: bool = False,
             size_prefixed: bool = False, check_identifier: bool = True):
        """Return a lazy view over the root table of a buffer."""
        from ._reader import read
        return read(self, data, root, verify=verify, size_prefixed=size_prefixed,
                    check_identifier=check_identifier)

    def verify(self, data: Any, root: str | Object | None = None, **kw) -> None:
        """Fully verify a buffer; raises :class:`flatview.VerificationError`."""
        from ._verify import verify
        verify(self, data, root, **kw)

    def encode(self, value: Any, root: str | Object | None = None, **kw) -> bytes:
        """Serialise a Python value (dicts/lists/views) into a FlatBuffer."""
        from ._builder import encode
        return encode(self, value, root, **kw)

    def to_fbs(self) -> str:
        """Render the schema back to .fbs text."""
        from ._fbs import render
        return render(self)
