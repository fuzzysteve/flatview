"""Load binary schemas (.bfbs, as written by ``flatc --schema -b``).

A .bfbs is itself a FlatBuffer of ``reflection.fbs``. We bootstrap by parsing
the bundled reflection.fbs with our own .fbs parser and then reading the
.bfbs with our own reader - no generated code anywhere.
"""

from __future__ import annotations

import re
from functools import lru_cache
from importlib import resources
from typing import Any

from ._fbs import parse_fbs
from ._reader import read
from ._schema import (
    BaseType, Enum, EnumVal, Field, Object, Schema, SchemaError, Type, INT_RANGE, SCALAR_SIZE,
)
from ._verify import VerificationError

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_QUALIFIED = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")


@lru_cache(maxsize=1)
def reflection_schema() -> Schema:
    """The schema of .bfbs files themselves (``reflection.fbs``)."""
    text = resources.files("flatview").joinpath("reflection.fbs").read_text(encoding="utf-8")
    return parse_fbs(text, "reflection.fbs")


def _attrs(kvs) -> dict[str, str]:
    if kvs is None:
        return {}
    return {kv.key: kv.value if kv.value is not None else "" for kv in kvs}


def _docs(docs) -> list[str]:
    return list(docs) if docs is not None else []


def _base(value: int, what: str) -> BaseType:
    try:
        return BaseType(int(value))
    except ValueError:
        raise SchemaError(f"invalid .bfbs: unknown base type {value} in {what}") from None


def load_bfbs(data: Any, source: str | None = None) -> Schema:
    """Build a :class:`Schema` from .bfbs bytes.

    A .bfbs is treated as untrusted input: the buffer is fully verified
    against reflection.fbs, every name must be a valid identifier, and all
    layout values are checked before the schema is used. Raises SchemaError.
    """
    refl = reflection_schema()
    try:
        raw = read(refl, data, verify=True)
    except (VerificationError, ValueError) as e:
        raise SchemaError(f"invalid .bfbs{f' {source}' if source else ''}: {e}") from None
    objects: list[Object] = []
    enums: list[Enum] = []
    raw_objects = list(raw.objects)
    raw_enums = list(raw.enums)
    for ro in raw_objects:
        if not _QUALIFIED.fullmatch(ro.name):
            raise SchemaError(f"invalid .bfbs: bad object name {ro.name!r}")
        objects.append(Object(ro.name, is_struct=ro.is_struct, minalign=ro.minalign,
                              bytesize=ro.bytesize, attributes=_attrs(ro.attributes),
                              documentation=_docs(ro.documentation),
                              declaration_file=ro.declaration_file))
    for re_ in raw_enums:
        if not _QUALIFIED.fullmatch(re_.name):
            raise SchemaError(f"invalid .bfbs: bad enum name {re_.name!r}")
        enums.append(Enum(re_.name, is_union=re_.is_union,
                          underlying=_base(re_.underlying_type.base_type, re_.name),
                          attributes=_attrs(re_.attributes), documentation=_docs(re_.documentation),
                          declaration_file=re_.declaration_file))

    def make_type(rt) -> Type:
        base = _base(rt.base_type, "a type")
        element = _base(rt.element, "a type")
        idx = rt.index
        ref = None
        if idx >= 0:
            item = element if base in (BaseType.VECTOR, BaseType.VECTOR64, BaseType.ARRAY) else base
            try:
                ref = objects[idx] if item == BaseType.OBJ else enums[idx]
            except IndexError:
                raise SchemaError(f"type index {idx} out of range") from None
        return Type(base, element, ref, rt.fixed_length)

    for obj, ro in zip(objects, raw_objects):
        fields = []
        for rf in ro.fields:
            if not _IDENT.fullmatch(rf.name):
                raise SchemaError(f"invalid .bfbs: bad field name {rf.name!r} in {ro.name}")
            t = make_type(rf.type)
            f = Field(rf.name, t, id=rf.id, offset=rf.offset, deprecated=rf.deprecated,
                      required=rf.required, key=rf.key, optional=rf.optional, padding=rf.padding,
                      attributes=_attrs(rf.attributes), documentation=_docs(rf.documentation))
            if t.base.is_scalar:
                if f.optional:
                    f.default = None
                elif t.base == BaseType.BOOL:
                    f.default = bool(rf.default_integer)
                elif t.base.is_float:
                    f.default = rf.default_real
                else:
                    f.default = rf.default_integer
                    if t.base == BaseType.ULONG and f.default < 0:
                        f.default &= (1 << 64) - 1  # stored in a signed long
            fields.append(f)
        # reflection sorts fields by name; tables are more useful in id order
        # (structs: id == declaration order == offset order).
        fields.sort(key=lambda f: f.id)
        obj.fields = fields

    for enum, re_ in zip(enums, raw_enums):
        for rv in re_.values:
            if not _IDENT.fullmatch(rv.name):
                raise SchemaError(f"invalid .bfbs: bad enum value name {rv.name!r} in {re_.name}")
            ut = make_type(rv.union_type) if rv.union_type is not None else None
            if not enum.is_union:
                ut = None
            enum.values.append(EnumVal(rv.name, rv.value, ut, _attrs(rv.attributes),
                                       _docs(rv.documentation)))

    schema = Schema(objects={o.name: o for o in objects}, enums={e.name: e for e in enums},
                    file_ident=raw.file_ident or None, file_ext=raw.file_ext or None, source=source)
    if len(schema.objects) != len(objects) or len(schema.enums) != len(enums):
        raise SchemaError("invalid .bfbs: duplicate type names")
    rt = raw.root_table
    if rt is not None:
        schema.root = schema.objects.get(rt.name)
        if schema.root is None or schema.root.is_struct:
            raise SchemaError(f"invalid .bfbs: root_table {rt.name!r} is not a table in the schema")
    validate(schema)
    return schema


def validate(schema: Schema) -> None:
    """Check a schema's layout is internally consistent and safe to use.

    The .fbs parser produces consistent schemas by construction; this guards
    schemas from .bfbs files, whose numbers come straight from the input.
    """
    def fail(msg: str) -> None:
        raise SchemaError(f"invalid schema: {msg}")

    for e in schema.enums.values():
        if e.is_union:
            for v in e.values:
                ut = v.union_type
                if ut is None or ut.base == BaseType.NONE:
                    continue
                if ut.base == BaseType.STRING:
                    continue
                if ut.base != BaseType.OBJ or not isinstance(ut.ref, Object):
                    fail(f"union {e.name} member {v.name} is not a table")
        else:
            if not e.underlying.is_integer or e.underlying == BaseType.UTYPE:
                fail(f"enum {e.name} has non-integer underlying type {e.underlying.name}")
            lo, hi = INT_RANGE[e.underlying]
            for v in e.values:
                if not lo <= v.value <= hi:
                    fail(f"enum {e.name} value {v.name}={v.value} out of range")

    def check_ref(owner: str, base: BaseType, t: Type, in_struct: bool) -> None:
        if base == BaseType.OBJ:
            if not isinstance(t.ref, Object):
                fail(f"{owner}: object type without a table/struct")
            if in_struct and not t.ref.is_struct:
                fail(f"{owner}: structs can only contain structs")
        elif base in (BaseType.UNION, BaseType.UTYPE):
            if base == BaseType.UNION and not (isinstance(t.ref, Enum) and t.ref.is_union):
                fail(f"{owner}: union type without a union")
            if t.ref is not None and not (isinstance(t.ref, Enum) and t.ref.is_union):
                fail(f"{owner}: union type field refers to a non-union")
        elif base.is_scalar:
            if t.ref is not None and not (isinstance(t.ref, Enum) and not t.ref.is_union
                                          and t.ref.underlying == base):
                fail(f"{owner}: scalar refers to an enum with a different underlying type")
        elif base == BaseType.STRING:
            pass
        else:
            fail(f"{owner}: invalid item type {base.name}")

    for o in schema.objects.values():
        if o.is_struct:
            if o.minalign < 1 or o.minalign > 256 or o.minalign & (o.minalign - 1):
                fail(f"struct {o.name} has invalid alignment {o.minalign}")
            if o.bytesize <= 0 or o.bytesize % o.minalign:
                fail(f"struct {o.name} has invalid size {o.bytesize}")
            for f in o.fields:
                owner = f"{o.name}.{f.name}"
                t = f.type
                if t.base == BaseType.ARRAY:
                    if t.fixed_length < 1:
                        fail(f"{owner}: array length must be positive")
                    check_ref(owner, t.element, t, True)
                    if not (t.element.is_scalar or t.element == BaseType.OBJ):
                        fail(f"{owner}: invalid array element {t.element.name}")
                    n = t.fixed_length
                    item = t.element
                elif t.base.is_scalar or t.base == BaseType.OBJ:
                    check_ref(owner, t.base, t, True)
                    n, item = 1, t.base
                else:
                    fail(f"{owner}: structs cannot hold {t.base.name}")
                size = (t.ref.bytesize if item == BaseType.OBJ else SCALAR_SIZE[item]) * n
                if f.offset < 0 or f.offset + size > o.bytesize:
                    fail(f"{owner}: field at offset {f.offset} (+{size}) overruns struct size {o.bytesize}")
        else:
            ids = set()
            for f in o.fields:
                owner = f"{o.name}.{f.name}"
                if f.id in ids:
                    fail(f"{owner}: duplicate field id {f.id}")
                ids.add(f.id)
                if f.offset != 4 + 2 * f.id:
                    fail(f"{owner}: vtable slot {f.offset} does not match id {f.id}")
                t = f.type
                if t.base in (BaseType.VECTOR, BaseType.VECTOR64):
                    if t.element in (BaseType.VECTOR, BaseType.VECTOR64, BaseType.ARRAY, BaseType.NONE):
                        fail(f"{owner}: invalid vector element {t.element.name}")
                    check_ref(owner, t.element, t, False)
                elif t.base in (BaseType.ARRAY, BaseType.NONE):
                    fail(f"{owner}: tables cannot hold {t.base.name}")
                else:
                    check_ref(owner, t.base, t, False)
                if t.item == BaseType.UNION:
                    tf = o.fields_by_name.get(f.name + "_type")
                    if tf is None or tf.type.item != BaseType.UTYPE:
                        fail(f"{owner}: union field without a matching {f.name}_type field")
                if f.key and not (t.base.is_scalar or t.base == BaseType.STRING):
                    fail(f"{owner}: key field must be a scalar or string")

    # structs must not contain themselves (directly or indirectly); iterative
    # so deep nesting cannot hit the recursion limit
    state: dict[str, int] = {}           # 1 = on the current path, 2 = done
    for start in schema.objects.values():
        if not start.is_struct or state.get(start.name) == 2:
            continue
        work: list[tuple[Object, bool]] = [(start, False)]
        while work:
            o, ready = work.pop()
            if ready:
                state[o.name] = 2
                continue
            if state.get(o.name) == 2:
                continue
            state[o.name] = 1
            work.append((o, True))
            for f in o.fields:
                if f.type.item == BaseType.OBJ:
                    sub = f.type.ref
                    if state.get(sub.name) == 1:
                        fail(f"struct {sub.name} contains itself")
                    if state.get(sub.name) != 2:
                        work.append((sub, False))
