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
_FILE_EXT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}")


@lru_cache(maxsize=1)
def reflection_schema() -> Schema:
    """The schema of .bfbs files themselves (``reflection.fbs``)."""
    text = resources.files("flatview").joinpath("reflection.fbs").read_text(encoding="utf-8")
    return parse_fbs(text, "reflection.fbs")


def _attrs(kvs) -> dict[str, str]:
    if kvs is None:
        return {}
    out = {}
    for kv in kvs:
        if not _IDENT.fullmatch(kv.key):
            raise SchemaError(f"invalid .bfbs: bad attribute name {kv.key!r}")
        out[kv.key] = kv.value if kv.value is not None else ""
    return out


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
                              declaration_file=ro.declaration_file or None))
    for re_ in raw_enums:
        if not _QUALIFIED.fullmatch(re_.name):
            raise SchemaError(f"invalid .bfbs: bad enum name {re_.name!r}")
        enums.append(Enum(re_.name, is_union=re_.is_union,
                          underlying=_base(re_.underlying_type.base_type, re_.name),
                          attributes=_attrs(re_.attributes), documentation=_docs(re_.documentation),
                          declaration_file=re_.declaration_file or None))

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

    file_ident = raw.file_ident or None
    if file_ident is not None and len(file_ident.encode("utf-8")) != 4:
        raise SchemaError(f"invalid .bfbs: file identifier {file_ident!r} is not 4 bytes")
    file_ext = raw.file_ext or None
    if file_ext is not None and not _FILE_EXT.fullmatch(file_ext):
        raise SchemaError(f"invalid .bfbs: bad file extension {file_ext!r}")
    schema = Schema(objects={o.name: o for o in objects}, enums={e.name: e for e in enums},
                    file_ident=file_ident, file_ext=file_ext, source=source)
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
            if sorted(f.id for f in o.fields) != list(range(len(o.fields))):
                fail(f"table {o.name}: field ids must be 0..{len(o.fields) - 1} without gaps")
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
                    # same shape (scalar/vector) and the same union, or readers misread it
                    tf = o.fields_by_name.get(f.name + "_type")
                    if (tf is None or tf.type.item != BaseType.UTYPE or tf.type.base != (
                            BaseType.VECTOR if t.base == BaseType.VECTOR else BaseType.UTYPE)
                            or tf.type.ref is not t.ref):
                        fail(f"{owner}: union field without a matching {f.name}_type field")
                nested = f.attributes.get("nested_flatbuffer")
                if nested is not None:
                    # as the .fbs parser: reader and verifier only agree on [ubyte]
                    if t.base != BaseType.VECTOR or t.element != BaseType.UBYTE:
                        fail(f"{owner}: nested_flatbuffer field must be [ubyte]")
                    from ._reader import _resolve_in
                    try:
                        target = _resolve_in(schema, nested, o.namespace)
                    except (KeyError, SchemaError):
                        target = None
                    if target is None or target.is_struct:
                        fail(f"{owner}: nested_flatbuffer {nested!r} is not a table in the schema")
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


# --------------------------------------------------------------------------
# Writing .bfbs

#: flatc's SizeOf() for each base type (offsets are 4 bytes, NONE counts as 1).
_SIZE_OF = {BaseType.NONE: 1, BaseType.STRING: 4, BaseType.VECTOR: 4, BaseType.OBJ: 4,
            BaseType.UNION: 4, BaseType.ARRAY: 4, BaseType.VECTOR64: 8, **SCALAR_SIZE}

_ADV_ARRAYS, _ADV_UNIONS, _ADV_OPTIONAL, _ADV_DEFAULTS = 1, 2, 4, 8


def to_bfbs(schema: Schema, *, comments: bool = True, builtins: bool = True) -> bytes:
    """Serialise ``schema`` as a .bfbs (a FlatBuffer of ``reflection.fbs``).

    The output is what ``flatc --schema -b --bfbs-comments --bfbs-builtins``
    writes for the same schema, apart from ``fbs_files`` (the include graph,
    which the model doesn't keep) and services (likewise). ``comments=False,
    builtins=False`` matches plain ``flatc --schema -b``; note that without
    built-in attributes ``nested_flatbuffer``, ``force_align`` etc. are gone
    when the file is loaded again.
    """
    from ._builder import _encode
    from ._fbs import BUILTIN_ATTRIBUTES

    # flatc: indices are positions in the name-sorted vectors (bytewise order)
    objects = sorted(schema.objects.values(), key=lambda o: o.name.encode())
    enums = sorted(schema.enums.values(), key=lambda e: e.name.encode())
    oidx = {id(o): i for i, o in enumerate(objects)}
    eidx = {id(e): i for i, e in enumerate(enums)}
    root_dir = _project_root(schema)
    features = 0

    def attrs(d: dict[str, str]) -> list[dict] | None:
        kvs = [{"key": k, "value": v} for k, v in sorted(d.items(), key=lambda kv: kv[0].encode())
               if builtins or k not in BUILTIN_ATTRIBUTES]
        return kvs or None

    def docs(lines: list[str]) -> list[str] | None:
        return list(lines) if comments and lines else None

    def type_(base: BaseType, element: BaseType = BaseType.NONE, ref: Object | Enum | None = None,
              fixed_length: int = 0) -> dict:
        index = -1
        if isinstance(ref, Object):
            index = oidx[id(ref)]
        elif isinstance(ref, Enum):
            index = eidx[id(ref)]
        element_size = _SIZE_OF[element]
        if base == BaseType.VECTOR and element == BaseType.OBJ and ref.is_struct:
            element_size = ref.bytesize
        return {"base_type": int(base), "element": int(element), "index": index,
                "fixed_length": fixed_length, "base_size": _SIZE_OF[base], "element_size": element_size}

    def field(o: Object, f: Field) -> dict:
        nonlocal features
        t = f.type
        if t.base == BaseType.ARRAY:
            features |= _ADV_ARRAYS
        if t.base == BaseType.VECTOR and t.element == BaseType.UNION:
            features |= _ADV_UNIONS
        if t.base.is_scalar and f.optional:
            features |= _ADV_OPTIONAL
        if (not o.is_struct and t.base in (BaseType.STRING, BaseType.VECTOR)
                and not f.optional and not f.required):
            features |= _ADV_DEFAULTS          # only a string/vector default makes these non-optional
        d: dict[str, Any] = {
            "name": f.name, "type": type_(t.base, t.element, t.ref, t.fixed_length),
            "id": f.id, "offset": f.offset, "deprecated": f.deprecated, "required": f.required,
            "key": f.key, "optional": f.optional, "padding": f.padding,
            "attributes": attrs(f.attributes), "documentation": docs(f.documentation),
        }
        if t.base.is_integer and f.default is not None:
            v = int(f.default)
            d["default_integer"] = v - (1 << 64) if v >= 1 << 63 else v    # stored in a signed long
        elif t.base.is_float and f.default is not None:
            d["default_real"] = float(f.default)
        return d

    obj_dicts = {}
    for o in objects:
        obj_dicts[id(o)] = {
            "name": o.name, "fields": [field(o, f) for f in o.fields], "is_struct": o.is_struct,
            "minalign": o.minalign, "bytesize": o.bytesize, "attributes": attrs(o.attributes),
            "documentation": docs(o.documentation),
            "declaration_file": _declaration(o.declaration_file, root_dir),
        }

    def enum_val(e: Enum, v) -> dict:
        ut = v.union_type if e.is_union and v.union_type is not None else None
        return {"name": v.name, "value": v.value,
                "union_type": type_(ut.base, ut.element, ut.ref) if ut is not None else type_(BaseType.NONE),
                "attributes": attrs(v.attributes), "documentation": docs(v.documentation)}

    enum_dicts = [{
        "name": e.name, "values": [enum_val(e, v) for v in e.values], "is_union": e.is_union,
        "underlying_type": type_(BaseType.UTYPE if e.is_union else e.underlying, ref=e),
        "attributes": attrs(e.attributes), "documentation": docs(e.documentation),
        "declaration_file": _declaration(e.declaration_file, root_dir),
    } for e in enums]

    value = {
        "objects": [obj_dicts[id(o)] for o in objects],
        "enums": enum_dicts,
        "file_ident": schema.file_ident or "",
        "file_ext": schema.file_ext or "",
        # the same dict as in "objects": written once, both offsets point at it
        "root_table": obj_dicts[id(schema.root)] if schema.root is not None else None,
        "services": [],
        "advanced_features": features,
    }
    return _encode(reflection_schema(), value, None, share_tables=True).output


def _project_root(schema: Schema) -> str | None:
    """Directory declaration files are written relative to (flatc's project
    root, which defaults to the main schema's directory)."""
    import os
    src = schema.source
    if src and not src.startswith("<") and os.path.isfile(src):
        return os.path.dirname(os.path.abspath(src))
    return None


def _declaration(path: str | None, root: str | None) -> str | None:
    """flatc's form: ``//`` + the path relative to the project root, ``/``-separated."""
    import os
    import posixpath
    if not path or path.startswith("<"):
        return None
    if path.startswith("//"):
        return path                         # already in flatc's form (loaded from a .bfbs)
    rel = None
    if root is not None:
        try:
            rel = os.path.relpath(os.path.abspath(path), root)
        except ValueError:                  # e.g. another drive on Windows
            rel = None
    if rel is None:
        rel = os.path.basename(path)
    return "//" + posixpath.join(*rel.replace(os.sep, "/").split("/"))
