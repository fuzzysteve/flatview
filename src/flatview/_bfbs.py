"""Load binary schemas (.bfbs, as written by ``flatc --schema -b``).

A .bfbs is itself a FlatBuffer of ``reflection.fbs``. We bootstrap by parsing
the bundled reflection.fbs with our own .fbs parser and then reading the
.bfbs with our own reader - no generated code anywhere.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
from typing import Any

from ._fbs import parse_fbs
from ._reader import read
from ._schema import BaseType, Enum, EnumVal, Field, Object, Schema, SchemaError, Type


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


def load_bfbs(data: Any, source: str | None = None) -> Schema:
    """Build a :class:`Schema` from .bfbs bytes."""
    refl = reflection_schema()
    raw = read(refl, data)
    objects: list[Object] = []
    enums: list[Enum] = []
    raw_objects = list(raw.objects)
    raw_enums = list(raw.enums)
    for ro in raw_objects:
        objects.append(Object(ro.name, is_struct=ro.is_struct, minalign=ro.minalign,
                              bytesize=ro.bytesize, attributes=_attrs(ro.attributes),
                              documentation=_docs(ro.documentation),
                              declaration_file=ro.declaration_file))
    for re_ in raw_enums:
        enums.append(Enum(re_.name, is_union=re_.is_union,
                          underlying=BaseType(int(re_.underlying_type.base_type)),
                          attributes=_attrs(re_.attributes), documentation=_docs(re_.documentation),
                          declaration_file=re_.declaration_file))

    def make_type(rt) -> Type:
        base = BaseType(int(rt.base_type))
        element = BaseType(int(rt.element))
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
            ut = make_type(rv.union_type) if rv.union_type is not None else None
            if not enum.is_union:
                ut = None
            enum.values.append(EnumVal(rv.name, rv.value, ut, _attrs(rv.attributes),
                                       _docs(rv.documentation)))

    schema = Schema(objects={o.name: o for o in objects}, enums={e.name: e for e in enums},
                    file_ident=raw.file_ident or None, file_ext=raw.file_ext or None, source=source)
    rt = raw.root_table
    if rt is not None:
        schema.root = schema.objects.get(rt.name)
    return schema
