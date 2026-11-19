"""Parser for FlatBuffers schema text (.fbs) -> :class:`Schema`.

Supports the full declaration grammar in practical use: namespaces, includes,
tables, structs (with fixed-size arrays and force_align), enums (incl.
bit_flags), unions (incl. aliases and vectors of unions), explicit/implicit
field ids, defaults (numeric, enum names, ``null`` for optional scalars),
user attributes, doc comments, root_type, file_identifier, file_extension.
rpc_service blocks are parsed and ignored.
"""

from __future__ import annotations

import math
import os
import re
import stat
import sys
from dataclasses import dataclass, field as dc_field
from typing import Any

from ._schema import (
    BaseType, Enum, EnumVal, Field, Object, Schema, SchemaError, Type,
    INT_RANGE, SCALAR_SIZE, TYPE_NAMES,
)

#: Attributes flatc treats as built in (not written to .bfbs by default).
BUILTIN_ATTRIBUTES = frozenset({          # flatc's known_attributes_ (idl.h), exactly
    "deprecated", "required", "key", "shared", "hash", "id", "force_align",
    "bit_flags", "original_order", "nested_flatbuffer", "csharp_partial",
    "streaming", "idempotent", "cpp_type", "cpp_ptr_type", "cpp_ptr_type_get",
    "cpp_str_type", "cpp_str_flex_ctor", "native_inline", "native_custom_alloc",
    "native_type", "native_type_pack_name", "native_default", "flexbuffer",
    "private", "offset64", "vector64",
})

#: flatc's FLATBUFFERS_MAX_ALIGNMENT
MAX_ALIGNMENT = 32
_HASHES = {16: ("fnv1_16", "fnv1a_16"), 32: ("fnv1_32", "fnv1a_32"), 64: ("fnv1_64", "fnv1a_64")}

_TOKEN = re.compile(r"""
    (?P<ws>[ \t\r\n]+)
  | (?P<doc>///[^\n]*)
  | (?P<comment>//[^\n]*|/\*.*?\*/)
  | (?P<string>"(?:[^"\\]|\\.)*")
  | (?P<number>[-+]?(?:0[xX](?:[0-9a-fA-F]+\.?[0-9a-fA-F]*|\.[0-9a-fA-F]+)(?:[pP][-+]?\d+)?
                       |(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?))
  | (?P<ident>[-+]?[A-Za-z_][A-Za-z0-9_]*)
  | (?P<punct>[{}()\[\]:;,=.])
""", re.S | re.X)


@dataclass
class _Tok:
    kind: str
    text: str
    line: int
    docs: list[str]


def _tokenize(text: str, filename: str) -> list[_Tok]:
    toks: list[_Tok] = []
    pos, line, docs = 0, 1, []
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise SchemaError(f"{filename}:{line}: unexpected character {text[pos]!r}")
        kind, val = m.lastgroup, m.group()
        if kind == "doc":
            docs.append(val[3:])
        elif kind not in ("ws", "comment"):
            toks.append(_Tok(kind, val, line, docs))
            docs = []
        line += val.count("\n")
        pos = m.end()
    toks.append(_Tok("eof", "", line, []))
    return toks


# --------------------------------------------------------------------------
# Parse tree (unresolved)

@dataclass
class _FieldDecl:
    name: str
    type: tuple            # ("name", str) | ("vector", t) | ("array", t, n)
    default: Any           # None, or a literal token text / "null"
    attrs: dict[str, str]
    docs: list[str]
    line: int
    seq: int = 0           # parse position (flatc's checks depend on what came before)


@dataclass
class _ObjDecl:
    name: str
    namespace: str
    is_struct: bool
    fields: list[_FieldDecl]
    attrs: dict[str, str]
    docs: list[str]
    file: str
    line: int
    order: int = 0          # position among all declarations (flatc is order-sensitive)
    seq: int = 0            # parse position of the declaration's start


@dataclass
class _EnumValDecl:
    name: str
    value: str | None
    attrs: dict[str, str]
    docs: list[str]
    union_type: str | None = None


@dataclass
class _EnumDecl:
    name: str
    namespace: str
    is_union: bool
    underlying: str | None
    values: list[_EnumValDecl]
    attrs: dict[str, str]
    docs: list[str]
    file: str
    line: int
    order: int = 0


@dataclass
class _Ctx:
    objects: list[_ObjDecl] = dc_field(default_factory=list)
    enums: list[_EnumDecl] = dc_field(default_factory=list)
    root: tuple[str, str, str, int, int] | None = None   # (name, namespace, file, line, order)
    roots: list[tuple[str, str, str, int, int]] = dc_field(default_factory=list)  # every root_type
    seq: int = 0
    services: list[tuple[str, str, str, int]] = dc_field(default_factory=list)  # (type, ns, file, line)
    decl_count: int = 0
    file_ident: str | None = None
    file_ext: str | None = None
    attributes: set[str] = dc_field(default_factory=set)
    seen_files: set[str] = dc_field(default_factory=set)
    include_paths: list[str] = dc_field(default_factory=list)
    #: Real paths includes may resolve into: the main schema's directory and
    #: include_paths. Nothing outside them is ever opened.
    include_roots: list[str] = dc_field(default_factory=list)
    allow_includes: bool = True


class _Parser:
    def __init__(self, text: str, filename: str, ctx: _Ctx, is_main: bool):
        self.toks = _tokenize(text, filename)
        self.i = 0
        self.file = filename
        self.ctx = ctx
        self.ns = ""
        self.is_main = is_main

    # -- token helpers -----------------------------------------------------
    @property
    def tok(self) -> _Tok:
        return self.toks[self.i]

    def err(self, msg: str, tok: _Tok | None = None) -> SchemaError:
        tok = tok or self.tok
        return SchemaError(f"{self.file}:{tok.line}: {msg}")

    def next(self) -> _Tok:
        t = self.toks[self.i]
        if t.kind != "eof":           # never step past the end
            self.i += 1
        return t

    def accept(self, text: str) -> bool:
        if self.tok.text == text and self.tok.kind in ("punct", "ident"):
            self.i += 1
            return True
        return False

    def expect(self, text: str) -> _Tok:
        if self.tok.text != text:
            raise self.err(f"expected {text!r}, got {self.tok.text or 'end of file'!r}")
        return self.next()

    def ident(self) -> str:
        t = self.tok
        if t.kind != "ident" or t.text[0] in "+-":
            raise self.err(f"expected identifier, got {t.text or 'end of file'!r}")
        self.i += 1
        return t.text

    def dotted(self) -> str:
        parts = [self.ident()]
        while self.accept("."):
            parts.append(self.ident())
        return ".".join(parts)

    def string(self) -> str:
        t = self.tok
        if t.kind != "string":
            raise self.err(f"expected string literal, got {t.text!r}")
        self.i += 1
        return self.unquote(t)

    def unquote(self, t: _Tok) -> str:
        try:
            return _unquote(t.text)
        except ValueError as e:
            raise self.err(f"bad string literal: {e}", t) from None

    def qualify(self, name: str) -> str:
        return f"{self.ns}.{name}" if self.ns else name

    # -- grammar -----------------------------------------------------------
    def parse(self) -> None:
        # includes must come first, but be lenient about ordering
        while self.tok.kind != "eof":
            t = self.tok
            kw = t.text
            if kw in ("include", "native_include"):
                self.next()
                path = self.string()
                self.expect(";")
                if kw == "include":
                    self.include(path, t)
            elif kw == "namespace":
                self.next()
                self.ns = "" if self.tok.text == ";" else self.dotted()
                self.expect(";")
            elif kw == "attribute":
                self.next()
                name = self.string() if self.tok.kind == "string" else self.ident()
                self.ctx.attributes.add(name)
                self.expect(";")
            elif kw == "{":
                raise self.err("JSON data in schema files is not supported")
            elif kw in ("table", "struct"):
                self.parse_object()
            elif kw in ("enum", "union"):
                self.parse_enum()
            elif kw == "root_type":
                self.next()
                name = self.dotted()
                self.expect(";")
                if self.is_main:
                    # flatc resolves root_type immediately: the table must precede it
                    self.ctx.root = (name, self.ns, self.file, t.line, self.ctx.decl_count)
                    self.ctx.roots.append(self.ctx.root)
            elif kw == "file_identifier":
                self.next()
                ident = self.string()
                if len(ident.encode()) != 4:
                    raise self.err("file_identifier must be exactly 4 characters", t)
                self.expect(";")
                if self.is_main:
                    self.ctx.file_ident = ident
            elif kw == "file_extension":
                self.next()
                ext = self.string()
                self.expect(";")
                if self.is_main:
                    self.ctx.file_ext = ext or None
            elif kw == "rpc_service":
                self.parse_service()
            else:
                raise self.err(f"unexpected {kw!r}")

    def include(self, path: str, tok: _Tok) -> None:
        """Resolve and parse an include, confined to the include roots.

        Candidates are tried relative to the including file, then each
        include path. A candidate is used only if its real path (symlinks
        resolved) lies inside the main schema's directory or an include
        path, so an untrusted schema cannot read arbitrary files.
        """
        ctx = self.ctx
        if not ctx.allow_includes:
            raise self.err("includes are disabled (allow_includes=False)", tok)
        if not ctx.include_roots:
            raise self.err(f"cannot include {path!r}: schema text has no directory; "
                           "pass include_paths to allow includes", tok)
        here = os.path.dirname(self.file) if os.path.isfile(self.file) else None
        bases = ([here] if here else []) + ctx.include_paths
        outside = None
        for base in bases:
            cand = os.path.join(base, path)
            real = os.path.realpath(cand)
            if not os.path.isfile(real):
                continue
            if not any(_within(real, root) for root in ctx.include_roots):
                outside = path
                continue
            break
        else:
            if outside is not None:
                raise self.err(f"refusing to include {path!r}: outside the schema directory "
                               "and include_paths", tok)
            raise self.err(f"cannot find included file {path!r}", tok)
        if real in ctx.seen_files:
            return
        ctx.seen_files.add(real)
        try:
            data = _read_confined(real, ctx.include_roots)
        except _Unsafe as e:
            raise self.err(f"refusing to include {path!r}: {e}", tok) from None
        except OSError as e:
            raise self.err(f"cannot read included file {path!r}: {e.strerror or e}", tok) from None
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise self.err(f"included file {path!r} is not UTF-8 text", tok) from None
        _Parser(text, real, ctx, is_main=False).parse()

    def parse_attrs(self) -> dict[str, str]:
        """Attribute list, with flatc's rules: at least one entry; names are
        built in or declared earlier with ``attribute``; values are strings or
        32-bit integers (stored in decimal); a repeated name keeps its first
        value."""
        attrs: dict[str, str] = {}
        if not self.accept("("):
            return attrs
        while True:
            t = self.tok
            if t.kind == "string":
                self.next()
                name = self.unquote(t)
            elif t.kind == "ident" and t.text[0] not in "+-":
                self.next()
                name = t.text
            else:
                raise self.err(f"attribute name must be either identifier or string: {t.text!r}", t)
            if name not in BUILTIN_ATTRIBUTES and name not in self.ctx.attributes:
                raise self.err(f"user define attributes must be declared before use: {name}", t)
            val = "0"
            if self.accept(":"):
                v = self.next()
                if v.kind == "string":
                    val = self.unquote(v)
                elif v.kind == "number" and re.fullmatch(r"[-+]?(0[xX][0-9a-fA-F]+|[0-9]+)", v.text):
                    n = int(v.text, 0) if len(v.text) < 64 else None
                    if n is None or not -(1 << 31) <= n < (1 << 31):
                        raise self.err(f"invalid number {v.text[:40]!r}: attribute values must fit a 32-bit "
                                       "integer", v)
                    val = str(n)
                else:
                    raise self.err(f"attribute value must be a string or integer, got {v.text!r}", v)
            attrs.setdefault(name, val)
            if not self.accept(","):
                self.expect(")")
                break
        return attrs

    def parse_type(self, nested: bool = False) -> tuple:
        if self.accept("["):
            if nested:
                raise self.err("nested vectors/arrays are not allowed")
            inner = self.parse_type(nested=True)
            if self.accept(":"):
                t = self.next()
                if t.kind != "number":
                    raise self.err("expected array length", t)
                n = _to_int(t.text, f"{self.file}:{t.line}", "array length")
                self.expect("]")
                return ("array", inner, n)
            self.expect("]")
            return ("vector", inner)
        return ("name", self.dotted())

    def parse_object(self) -> None:
        start = self.next()
        docs = start.docs
        self.ctx.seq += 1
        decl_seq = self.ctx.seq
        name = self.ident()
        attrs = self.parse_attrs()
        self.expect("{")
        fields = []
        while not self.accept("}"):
            ft = self.tok
            fname = self.ident()
            self.expect(":")
            ftype = self.parse_type()
            default = None
            if self.accept("="):
                t = self.next()
                if t.kind == "punct" and t.text == "[":
                    self.expect("]")
                    default = "[]"
                else:
                    default = t.text
            fattrs = self.parse_attrs()
            self.expect(";")
            self.ctx.seq += 1
            fields.append(_FieldDecl(fname, ftype, default, fattrs, ft.docs, ft.line, self.ctx.seq))
        self.ctx.decl_count += 1
        self.ctx.objects.append(_ObjDecl(self.qualify(name), self.ns, start.text == "struct",
                                         fields, attrs, docs, self.file, start.line, self.ctx.decl_count,
                                         decl_seq))

    def parse_enum(self) -> None:
        start = self.next()
        is_union = start.text == "union"
        name = self.ident()
        underlying = None
        if self.accept(":"):
            underlying = self.dotted()
        attrs = self.parse_attrs()
        self.expect("{")
        values = []
        while not self.accept("}"):
            vt = self.tok
            vname = self.dotted() if is_union else self.ident()
            utype = None
            if is_union:
                if self.accept(":"):
                    utype = self.dotted()
                else:
                    utype = vname
                    if vname == "string":
                        raise self.err("type referenced but not defined (check namespace): string "
                                       "(a string union member needs a name: `S: string`)", vt)
                vname = vname.replace(".", "_")
            value = None
            if self.accept("="):
                value = self.next().text
            vattrs = self.parse_attrs()
            values.append(_EnumValDecl(vname, value, vattrs, vt.docs, utype))
            if not self.accept(","):
                self.expect("}")
                break
        self.ctx.decl_count += 1
        self.ctx.enums.append(_EnumDecl(self.qualify(name), self.ns, is_union, underlying,
                                        values, attrs, start.docs, self.file, start.line,
                                        self.ctx.decl_count))

    def parse_service(self) -> None:
        """rpc_service Name (attrs)? { Method(Request): Response (attrs)?; ... }

        Parsed and validated (request/response must be tables), not kept.
        """
        self.next()
        self.ident()
        self.parse_attrs()
        self.expect("{")
        while not self.accept("}"):
            self.ident()
            self.expect("(")
            req = self.tok
            self.ctx.services.append((self.dotted(), self.ns, self.file, req.line))
            self.expect(")")
            self.expect(":")
            resp = self.tok
            self.ctx.services.append((self.dotted(), self.ns, self.file, resp.line))
            self.parse_attrs()
            self.expect(";")


_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "0": "\0",
                   '"': '"', "'": "'", "\\": "\\", "/": "/"}


def _to_int(text: str, where: str, what: str) -> int:
    try:
        return int(text, 0)
    except ValueError:
        raise SchemaError(f"{where}: invalid {what} {text[:40]!r}") from None


def _unquote(s: str) -> str:
    """Decode a schema string literal. Raises ValueError on a bad escape."""
    body = s[1:-1]
    if "\\" not in body:
        return body
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= len(body):
            raise ValueError("string ends with a backslash")
        esc = body[i + 1]
        if esc in _SIMPLE_ESCAPES:
            out.append(_SIMPLE_ESCAPES[esc])
            i += 2
        elif esc in "xu":
            width = 2 if esc == "x" else 4
            digits = body[i + 2:i + 2 + width]
            if len(digits) != width or any(c not in "0123456789abcdefABCDEF" for c in digits):
                raise ValueError(f"bad \\{esc} escape")
            code = int(digits, 16)
            i += 2 + width
            if esc == "u" and 0xD800 <= code <= 0xDBFF and body[i:i + 2] == "\\u":
                low = body[i + 2:i + 6]
                if len(low) == 4 and all(c in "0123456789abcdefABCDEF" for c in low) and \
                        0xDC00 <= int(low, 16) <= 0xDFFF:
                    code = 0x10000 + ((code - 0xD800) << 10) + (int(low, 16) - 0xDC00)
                    i += 6
            if 0xD800 <= code <= 0xDFFF:
                raise ValueError("unpaired surrogate in \\u escape")
            out.append(chr(code))
        else:
            raise ValueError(f"unknown escape \\{esc}")
    return "".join(out)


# --------------------------------------------------------------------------
# Resolution: parse tree -> Schema


class _Resolver:
    def __init__(self, ctx: _Ctx):
        self.ctx = ctx
        self.schema = Schema()
        self.decls: dict[str, Any] = {}
        self.order: dict[str, int] = {}
        self._first_seen: dict[str, int] | None = None

    def lookup(self, name: str, ns: str, where: str) -> Object | Enum:
        parts = ns.split(".") if ns else []
        for i in range(len(parts), -1, -1):
            cand = ".".join(parts[:i] + [name])
            if cand in self.schema.objects:
                return self.schema.objects[cand]
            if cand in self.schema.enums:
                return self.schema.enums[cand]
        raise SchemaError(f"{where}: unknown type {name!r}")

    def run(self) -> Schema:
        ctx, schema = self.ctx, self.schema
        for d in ctx.objects:
            if d.name in schema.objects or d.name in schema.enums:
                raise SchemaError(f"{d.file}:{d.line}: {d.name} is defined twice")
            schema.objects[d.name] = Object(d.name, is_struct=d.is_struct, attributes=dict(d.attrs),
                                            documentation=list(d.docs), declaration_file=d.file)
        for d in ctx.enums:
            if d.name in schema.objects or d.name in schema.enums:
                raise SchemaError(f"{d.file}:{d.line}: {d.name} is defined twice")
            schema.enums[d.name] = Enum(d.name, is_union=d.is_union, attributes=dict(d.attrs),
                                        documentation=list(d.docs), declaration_file=d.file)
        for d in (*ctx.objects, *ctx.enums):
            self.order[d.name] = d.order
        for d in ctx.enums:
            self.resolve_enum(d)
        for d in ctx.objects:
            self.resolve_fields(d)
        done: set[str] = set()
        for d in ctx.objects:
            if d.is_struct:
                self.layout_struct(schema.objects[d.name], set(), done)
        for d in ctx.objects:
            if not d.is_struct:
                self.check_nested(schema.objects[d.name], d)
        for name, ns, file, line in ctx.services:
            t = self.lookup(name, ns, f"{file}:{line}")
            if not isinstance(t, Object) or t.is_struct:
                raise SchemaError(f"{file}:{line}: rpc request and response types must be tables: {name}")
        for name, ns, file, line, seen in ctx.roots:      # every root_type, as flatc checks each
            try:
                root = self.lookup(name, ns, f"{file}:{line}")
            except SchemaError:
                raise SchemaError(f"{file}:{line}: unknown root type: {name}") from None
            if self.order[root.name] > seen:
                raise SchemaError(f"{file}:{line}: unknown root type: {name} (declare it before root_type)")
            if not isinstance(root, Object) or root.is_struct:
                raise SchemaError(f"{file}:{line}: root_type {name} must be a table")
            schema.root = root
        schema.file_ident = ctx.file_ident
        schema.file_ext = ctx.file_ext
        # Match flatc/reflection: objects and enums sorted by name.
        schema.objects = dict(sorted(schema.objects.items()))
        schema.enums = dict(sorted(schema.enums.items()))
        return schema

    def resolve_enum(self, d: _EnumDecl) -> None:
        e = self.schema.enums[d.name]
        where = f"{d.file}:{d.line}"
        if d.is_union:
            e.underlying = BaseType.UTYPE
            if d.underlying:
                raise SchemaError(f"{where}: unions cannot have an underlying type")
            e.values.append(EnumVal("NONE", 0, Type(BaseType.NONE)))
        else:
            if not d.underlying:
                raise SchemaError(f"{where}: enum {d.name} needs an underlying type")
            bt = TYPE_NAMES.get(d.underlying)
            if bt is None or not bt.is_integer or bt == BaseType.BOOL:
                raise SchemaError(f"{where}: enum underlying type must be an integer type")
            e.underlying = bt
        if not d.values and not d.is_union:
            raise SchemaError(f"{where}: incomplete enum declaration, values not found")
        lo, hi = INT_RANGE[e.underlying]
        width = 8 * SCALAR_SIZE[e.underlying]
        signed = lo < 0
        nxt = 1 if d.is_union else 0
        for v in d.values:
            if v.value is not None:
                if not re.fullmatch(r"[-+]?(0[xX][0-9a-fA-F]+|[0-9]+)", v.value) or len(v.value) > 40:
                    raise SchemaError(f"{where}: bad enum value {v.value[:40]!r}")
                val = int(v.value, 0)
            else:
                val = nxt
            nxt = val + 1
            # range of the value as written (a bit position, for bit_flags)
            if not lo <= val <= hi:
                raise SchemaError(f"{where}: enum value does not fit, {v.name}={val} out of [{lo}; {hi}]")
            ut = None
            if d.is_union:
                if v.union_type == "string":
                    ut = Type(BaseType.STRING)
                else:
                    target = self.lookup(v.union_type, d.namespace, where)
                    if isinstance(target, Enum):
                        raise SchemaError(f"{where}: union member {v.union_type} must be a table")
                    ut = Type(BaseType.OBJ, ref=target)
            elif e.bit_flags:
                # checked before shifting: a huge position must never reach 1 << val
                if signed and val == width - 1:
                    raise SchemaError(f"{where}: underlying type of bit_flags enum must be unsigned")
                if val < 0 or val >= width:
                    raise SchemaError(f"{where}: bit flag out of range of underlying integral type")
                val = 1 << val
            e.values.append(EnumVal(v.name, val, ut, dict(v.attrs), list(v.docs)))
        seen = set()
        for v in e.values:
            if v.name in seen:
                raise SchemaError(f"{where}: duplicate enum value name {v.name}")
            seen.add(v.name)
        # as flatc/reflection: values sorted by value, and all distinct
        e.values.sort(key=lambda v: v.value)
        for a, b in zip(e.values, e.values[1:]):
            if a.value == b.value:
                raise SchemaError(f"{where}: all enum values must be unique: {a.name} and {b.name} "
                                  f"are both {a.value}")
        e._by_value = e._by_name = None

    def make_type(self, t: tuple, ns: str, where: str, in_struct: bool, owner: int = 1 << 62) -> Type:
        """Resolve a type. ``owner`` is the declaring object's position: like
        flatc, enums/unions and (inside structs) structs must be declared
        before they are used; tables may be referenced from anywhere."""
        kind = t[0]
        if kind == "name":
            name = t[1]
            bt = TYPE_NAMES.get(name)
            if bt is not None:
                return Type(bt)
            ref = self.lookup(name, ns, where)
            order = self.order.get(ref.name, 0)
            if isinstance(ref, Enum):
                if order > owner:
                    raise SchemaError(f"{where}: type referenced but not defined (check namespace): {name} "
                                      "(enums and unions must be declared before use)")
                return Type(BaseType.UNION if ref.is_union else ref.underlying, ref=ref)
            if in_struct and ref.is_struct and order > owner:
                raise SchemaError(f"{where}: Incomplete type in struct is not allowed, type name: {name}")
            return Type(BaseType.OBJ, ref=ref)
        inner = self.make_type(t[1], ns, where, in_struct, owner)
        if inner.base in (BaseType.VECTOR, BaseType.ARRAY):
            raise SchemaError(f"{where}: nested vectors/arrays are not allowed")
        if kind == "vector":
            if in_struct:
                raise SchemaError(f"{where}: structs cannot contain vectors (use [T:N] arrays)")
            return Type(BaseType.VECTOR, inner.base, inner.ref)
        if not in_struct:
            raise SchemaError(f"{where}: fixed-size arrays are only allowed in structs")
        if t[2] <= 0 or t[2] > 0xFFFF:
            raise SchemaError(f"{where}: bad array length {t[2]}")
        return Type(BaseType.ARRAY, inner.base, inner.ref, fixed_length=t[2])

    def resolve_fields(self, d: _ObjDecl) -> None:
        obj = self.schema.objects[d.name]
        fields: list[Field] = []
        explicit_ids = [f for f in d.fields if "id" in f.attrs]
        if explicit_ids and len(explicit_ids) != len(d.fields):
            raise SchemaError(f"{d.file}:{d.line}: either all or none of {d.name}'s fields need an id")
        next_id = 0
        for fd in d.fields:
            where = f"{d.file}:{fd.line}"
            ftype = self.make_type(fd.type, d.namespace, where, d.is_struct, d.order)
            if self.known_object_named(fd.name, d.namespace, fd.seq):
                raise SchemaError(f"{where}: field name can not be the same as table/struct name")
            if d.is_struct:
                if ftype.item in (BaseType.STRING, BaseType.UNION) or (
                        ftype.item == BaseType.OBJ and not ftype.ref.is_struct):
                    raise SchemaError(f"{where}: structs may only contain scalars, structs and arrays")
            f = Field(fd.name, ftype, attributes=dict(fd.attrs), documentation=list(fd.docs))
            f.deprecated = "deprecated" in fd.attrs
            f.required = "required" in fd.attrs
            f.key = "key" in fd.attrs
            self.check_field_attrs(f, d.is_struct, where)
            if d.is_struct and fd.default is not None and fd.default not in ("0", "0.0", "false"):
                raise SchemaError(f"{where}: default values are not supported for struct fields, table "
                                  "fields, or in structs")
            self.set_default(f, fd, where)
            if ftype.item == BaseType.UNION:
                if d.is_struct:
                    raise SchemaError(f"{where}: unions are not allowed in structs")
                if ftype.base == BaseType.VECTOR:
                    tt = Type(BaseType.VECTOR, BaseType.UTYPE, ftype.ref)
                else:
                    tt = Type(BaseType.UTYPE, ref=ftype.ref)
                tf = Field(fd.name + "_type", tt, default=0 if ftype.base != BaseType.VECTOR else None,
                           optional=ftype.base == BaseType.VECTOR)
                tf.deprecated = f.deprecated
                if explicit_ids:
                    vid = _to_int(fd.attrs["id"], where, "field id")
                    tf.id, f.id = vid - 1, vid
                    tf.attributes["id"] = str(vid - 1)     # as flatc records it
                else:
                    tf.id, f.id = next_id, next_id + 1
                    next_id += 2
                fields += [tf, f]
                continue
            if explicit_ids:
                f.id = _to_int(fd.attrs["id"], where, "field id")
            else:
                f.id = next_id
                next_id += 1
            fields.append(f)
        names = set()
        for f in fields:
            if f.name in names:
                raise SchemaError(f"{d.file}:{d.line}: duplicate field {f.name} in {d.name}")
            names.add(f.name)
        if not d.is_struct:
            ids = sorted(f.id for f in fields)
            if ids != list(range(len(ids))):
                raise SchemaError(f"{d.file}:{d.line}: field ids of {d.name} must be 0..{len(ids) - 1} "
                                  f"without gaps, got {ids}")
            fields.sort(key=lambda f: f.id)
            for f in fields:
                f.offset = 4 + 2 * f.id
            keys = [f for f in fields if f.key]
            if len(keys) > 1:
                raise SchemaError(f"{d.file}:{d.line}: {d.name} has more than one key field")
        else:
            for i, f in enumerate(fields):
                f.id = i
        obj.fields = fields
        obj._by_name = None
        obj._key = False

    def known_object_named(self, name: str, ns: str, seq: int) -> bool:
        """flatc: a field may not be named like a table/struct it already knows
        (declared, or referenced by an earlier field), resolved from ``ns``."""
        if self._first_seen is None:
            first: dict[str, int] = {}
            for d in self.ctx.objects:
                first[d.name] = min(first.get(d.name, d.seq), d.seq)
            for d in self.ctx.objects:
                for fd in d.fields:
                    t = fd.type
                    while t[0] != "name":
                        t = t[1]
                    if t[1] in TYPE_NAMES:
                        continue
                    try:
                        ref = self.lookup(t[1], d.namespace, "")
                    except SchemaError:
                        continue
                    if isinstance(ref, Object):
                        first[ref.name] = min(first.get(ref.name, fd.seq), fd.seq)
            self._first_seen = first
        try:
            ref = self.lookup(name, ns, "")
        except SchemaError:
            return False
        return isinstance(ref, Object) and self._first_seen.get(ref.name, 1 << 62) < seq

    def check_field_attrs(self, f: Field, in_struct: bool, where: str) -> None:
        """flatc's per-field attribute rules."""
        t = f.type
        if f.required and t.base.is_scalar and not in_struct:
            raise SchemaError(f"{where}: only non-scalar fields in tables may be 'required'")
        if f.key and not (t.base.is_scalar or t.base == BaseType.STRING or
                          (t.base == BaseType.ARRAY and t.element.is_scalar)):
            raise SchemaError(f"{where}: 'key' field must be string, scalar type or fixed size array of scalars")
        if "hash" in f.attributes:
            item = t.element if t.base == BaseType.VECTOR else t.base
            widths = {BaseType.SHORT: 16, BaseType.USHORT: 16, BaseType.INT: 32, BaseType.UINT: 32,
                      BaseType.LONG: 64, BaseType.ULONG: 64}
            if item not in widths or t.enum is not None:
                raise SchemaError(f"{where}: only short, ushort, int, uint, long and ulong data types "
                                  "support hashing")
            if f.attributes["hash"] not in _HASHES[widths[item]]:
                raise SchemaError(f"{where}: Unknown hashing algorithm for {widths[item]} bit types: "
                                  f"{f.attributes['hash']}")
        if "flexbuffer" in f.attributes and not (t.base == BaseType.VECTOR and t.element == BaseType.UBYTE):
            raise SchemaError(f"{where}: flexbuffer attribute may only apply to a vector of ubyte")
        if "force_align" in f.attributes:
            fa = _to_int(f.attributes["force_align"], where, "force_align")
            if fa < 1 or fa & (fa - 1) or fa > MAX_ALIGNMENT:
                raise SchemaError(f"{where}: force_align must be a power of two no larger than {MAX_ALIGNMENT}")

    def set_default(self, f: Field, fd: _FieldDecl, where: str) -> None:
        bt = f.type.base
        raw = fd.default
        if not bt.is_scalar:
            # As in reflection: non-scalar table fields are optional unless required.
            if f.key and bt == BaseType.STRING:
                f.required = True  # flatc: string keys are implicitly required
            f.optional = not f.required
            if raw is not None:
                # flatc: "Default values for strings and vectors are not supported"
                raise SchemaError(f"{where}: default values for strings, vectors and tables are not supported")
            return
        if raw == "null":
            f.optional = True
            f.default = None
            if f.required:
                raise SchemaError(f"{where}: optional scalar cannot be required")
            return
        if raw is None:
            f.default = False if bt == BaseType.BOOL else (0.0 if bt.is_float else 0)
        else:
            f.default = _parse_scalar_literal(raw, bt, f.type.enum, where)
        enum = f.type.enum
        if enum is not None and not enum.is_union and not enum.bit_flags and f.default not in enum.by_value:
            # as flatc: the (implicit or explicit) default must be a member
            raise SchemaError(f"{where}: default value of {f.default} for field {f.name} is not part of "
                              f"enum {enum.name}")


_INT_LITERAL = re.compile(r"[-+]?(0[xX][0-9a-fA-F]+|[0-9]+)")
_HEX_FLOAT = re.compile(r"[-+]?0[xX](?:[0-9a-fA-F]+\.?[0-9a-fA-F]*|\.[0-9a-fA-F]+)(?:[pP][-+]?[0-9]+)?")


def _parse_scalar_literal(raw: str, bt: BaseType, enum: Enum | None, where: str) -> Any:
    """A scalar default, with flatc's rules for which literals fit which type."""
    text = raw[1:-1] if raw.startswith('"') else raw
    is_int = _INT_LITERAL.fullmatch(text) is not None and len(text) <= 40
    if bt == BaseType.BOOL:
        if text in ("true", "false"):
            return text == "true"
        if is_int:
            v = int(text, 0)
            if 0 <= v <= 255:          # flatc parses bool defaults as ubyte
                return bool(v)
            raise SchemaError(f"{where}: invalid number {text!r}, constant does not fit [0; 255]")
        raise SchemaError(f"{where}: bad bool default {raw!r}")
    if text in ("true", "false"):
        raise SchemaError(f"{where}: enum values need to be qualified by an enum type: {text} "
                          "is only a bool default")
    if bt.is_float:
        low = text.lower().lstrip("+")
        if low in ("inf", "infinity", "-inf", "-infinity", "nan", "-nan"):
            return float(low.replace("infinity", "inf"))
        if _HEX_FLOAT.fullmatch(text):
            if "p" not in text.lower():
                raise SchemaError(f"{where}: invalid number {text!r}: the exponent suffix of hexadecimal "
                                  "floating-point literals is mandatory")
            return float.fromhex(text)
        try:
            return float(text)
        except ValueError:
            raise SchemaError(f"{where}: bad float default {raw!r}") from None
    if is_int:
        value = int(text, 0)
    elif enum is not None and re.fullmatch(r"[A-Za-z_][\w.]*( +[A-Za-z_][\w.]*)*", text):
        value = 0
        for part in text.split():
            name = part.rpartition(".")[2]
            if name not in enum.by_name:
                raise SchemaError(f"{where}: {name!r} is not a value of {enum.name}")
            value |= enum.by_name[name].value
    else:
        raise SchemaError(f"{where}: bad integer default {raw[:40]!r}")
    lo, hi = INT_RANGE[bt]
    if not lo <= value <= hi:
        raise SchemaError(f"{where}: default {value} out of range for {bt.name.lower()}")
    return value


def _scalar_align(t: Type, item: BaseType) -> tuple[int, int]:
    """(size, alignment) of one item of a struct field."""
    if item == BaseType.OBJ:
        return t.ref.bytesize, t.ref.minalign
    return SCALAR_SIZE[item], SCALAR_SIZE[item]


def _layout_struct(self: _Resolver, obj: Object, stack: set[str], done: set[str]) -> None:
    """Lay out ``obj`` and every struct it contains, innermost first.

    Iterative (explicit stack), so deeply nested struct declarations cannot
    hit Python's recursion limit.
    """
    work: list[tuple[Object, bool]] = [(obj, False)]
    while work:
        o, ready = work.pop()
        if o.name in done:
            continue
        if ready:
            stack.discard(o.name)
            _layout_one(o)
            done.add(o.name)
            continue
        if o.name in stack:
            raise SchemaError(f"struct {o.name} contains itself")
        if not o.fields:
            raise SchemaError(f"struct {o.name}: size 0 structs not allowed")
        stack.add(o.name)
        work.append((o, True))
        for f in o.fields:
            if f.type.item == BaseType.OBJ and f.type.ref.name not in done:
                if f.type.ref.name in stack:
                    raise SchemaError(f"struct {f.type.ref.name} contains itself")
                work.append((f.type.ref, False))


def _layout_one(obj: Object) -> None:
    """Compute offsets/padding/size of one struct whose members are laid out."""
    size, minalign = 0, 1
    prev: Field | None = None
    for f in obj.fields:
        isz, align = _scalar_align(f.type, f.type.item)
        fsize = isz * (f.type.fixed_length if f.type.base == BaseType.ARRAY else 1)
        pad = (-size) % align
        if prev is not None:
            prev.padding = pad
        size += pad
        f.offset = size
        size += fsize
        minalign = max(minalign, align)
        prev = f
    if "force_align" in obj.attributes:
        fa = _to_int(obj.attributes["force_align"], obj.name, "force_align")
        if fa < 1 or fa & (fa - 1) or fa < minalign or fa > MAX_ALIGNMENT:
            raise SchemaError(f"{obj.name}: unexpected force_align value {fa}, alignment must be a power "
                              f"of two from the type's natural alignment {minalign} to {MAX_ALIGNMENT}")
        minalign = fa
    pad = (-size) % minalign
    if prev is not None:
        prev.padding += pad
    obj.bytesize = size + pad
    obj.minalign = minalign
    if obj.bytesize > 0xFFFF:
        # reflection stores field offsets as uint16: flatc accepts such structs
        # but writes wrapped (wrong) offsets to .bfbs, so refuse them
        raise SchemaError(f"struct {obj.name} is {obj.bytesize} bytes; structs must fit in 65535 bytes")


def _check_nested(self: _Resolver, obj: Object, d: _ObjDecl) -> None:
    for f in obj.fields:
        nested = f.attributes.get("nested_flatbuffer")
        if nested is not None:
            if f.type.base != BaseType.VECTOR or f.type.element != BaseType.UBYTE:
                raise SchemaError(f"{d.file}: nested_flatbuffer field {obj.name}.{f.name} must be [ubyte]")
            self.lookup(nested, d.namespace, d.file)


_Resolver.layout_struct = _layout_struct
_Resolver.check_nested = _check_nested


# --------------------------------------------------------------------------
# Public API

def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:      # different drives on Windows
        return False


_MAX_SCHEMA_BYTES = 64 << 20


class _Unsafe(Exception):
    """An include resolved to something it must not read."""


def _fd_path(fd: int) -> str | None:
    """The real path of an open file, from the descriptor itself; None if the
    platform cannot tell us (then the caller falls back to an inode check)."""
    proc = f"/proc/self/fd/{fd}"
    if sys.platform.startswith("linux") and os.path.exists(proc):
        target = os.readlink(proc)
        return None if target.endswith(" (deleted)") else target
    if sys.platform == "darwin":
        import fcntl
        if hasattr(fcntl, "F_GETPATH"):
            buf = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
            return os.fsdecode(buf.split(b"\0", 1)[0])
    return None


def _read_confined(real: str, roots: list[str]) -> bytes:
    """Read an include whose path was checked to lie inside ``roots``.

    The check and the open are separate steps, so a path component could be
    swapped for a symlink (or the file for a FIFO) in between. This opens
    first and then re-checks what was *actually* opened: the descriptor's own
    path where the OS provides it, otherwise the checked path must still
    name the very same file (device and inode). Opening is non-blocking, so
    a FIFO cannot hang the parser, and only regular files are read.
    """
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0) \
        | getattr(os, "O_BINARY", 0)
    fd = os.open(real, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _Unsafe("not a regular file")
        actual = _fd_path(fd)
        if actual is not None:
            if not any(_within(actual, root) for root in roots):
                raise _Unsafe("it resolved outside the schema directory and include_paths")
        else:
            try:
                now = os.stat(real)
            except OSError:
                raise _Unsafe("the file changed while it was being opened") from None
            if (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino) or os.path.realpath(real) != real:
                raise _Unsafe("the file changed while it was being opened")
        if hasattr(os, "set_blocking"):
            os.set_blocking(fd, True)
        chunks, size = [], 0
        while size <= _MAX_SCHEMA_BYTES:
            chunk = os.read(fd, min(1 << 20, _MAX_SCHEMA_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > _MAX_SCHEMA_BYTES:
            raise _Unsafe(f"it is larger than {_MAX_SCHEMA_BYTES:,} bytes")
        return b"".join(chunks)
    finally:
        os.close(fd)


def parse_fbs(text: str, filename: str = "<string>", include_paths: list[str] | None = None,
              allow_includes: bool = True) -> Schema:
    """Parse schema text. ``filename`` is used for includes and messages.

    ``include`` statements may only reach files inside the directory of
    ``filename`` (when it is a real file) or one of ``include_paths``;
    symlinks are resolved before checking. ``allow_includes=False`` rejects
    every include, for schemas from untrusted sources.
    """
    ctx = _Ctx(include_paths=[os.fspath(p) for p in (include_paths or [])], allow_includes=allow_includes)
    roots = [os.path.realpath(p) for p in ctx.include_paths]
    if os.path.isfile(filename):
        ctx.seen_files.add(os.path.realpath(filename))
        roots.insert(0, os.path.dirname(os.path.realpath(filename)))
    ctx.include_roots = roots
    _Parser(text, filename, ctx, is_main=True).parse()
    schema = _Resolver(ctx).run()
    schema.source = filename
    return schema


# --------------------------------------------------------------------------
# Rendering: Schema -> .fbs text

def _fmt_float(x: float) -> str:
    if math.isnan(x):
        return "nan"
    if math.isinf(x):
        return "inf" if x > 0 else "-inf"
    return repr(x)


def _rel(name: str, ns: str) -> str:
    """Name as written from inside namespace ``ns`` (fully qualified if needed)."""
    return name.rpartition(".")[2] if name.rpartition(".")[0] == ns else name


def _type_text(t: Type, ns: str) -> str:
    def one(bt: BaseType) -> str:
        if t.ref is not None and bt != BaseType.UTYPE:
            return _rel(t.ref.name, ns)
        return bt.name.lower() if bt != BaseType.UTYPE else "ubyte"
    if t.base in (BaseType.VECTOR, BaseType.VECTOR64):
        return f"[{one(t.element)}]"
    if t.base == BaseType.ARRAY:
        return f"[{one(t.element)}:{t.fixed_length}]"
    return one(t.base)


_RENDER_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_RENDER_QUALIFIED = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")


def _ident(name: str, what: str, qualified: bool = False) -> str:
    """Refuse to render anything that is not a plain (dotted) identifier:
    rendered text is parsed again, so other characters could inject schema
    declarations (an ``include``, say)."""
    pattern = _RENDER_QUALIFIED if qualified else _RENDER_IDENT
    if not isinstance(name, str) or not pattern.fullmatch(name):
        raise SchemaError(f"cannot render {what} {name!r}: not an identifier")
    return name


def _attr_text(attrs: dict[str, str], extra: list[str] = ()) -> str:
    parts = list(extra)
    for k, v in attrs.items():
        _ident(k, "attribute name")
        if k in ("id", "key", "required", "deprecated"):
            continue
        parts.append(k if v == "0" and k not in ("force_align",) else f"{k}: {_quote_attr(v)}")
    return f" ({', '.join(parts)})" if parts else ""


def _quote_attr(v: str) -> str:
    if re.fullmatch(r"[-+]?[0-9]+", v):
        return v
    return _quote(v)


_INVISIBLE_CONTROLS = frozenset("\u061c\u200e\u200f\u2028\u2029\u202a\u202b\u202c\u202d\u202e"
                                "\u2066\u2067\u2068\u2069")


def _quote(v: str) -> str:
    """A schema string literal that decodes back to exactly ``v``."""
    out = []
    for ch in v:
        if ch in '"\\':
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\x{ord(ch):02x}")
        elif 0x80 <= ord(ch) <= 0x9F or ch in _INVISIBLE_CONTROLS:
            # C1 controls and bidi/separator characters: escaped so rendered
            # text can't drive a terminal or hide what it says
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _docs(lines: list[str], indent: str) -> str:
    # A doc string containing a line break must not end its comment early:
    # the rest would be parsed as declarations.
    return "".join(f"{indent}///{part}\n" for d in lines for part in (d.splitlines() or [""]))


def _declaration_order(schema: Schema) -> list:
    """An order flatc (and our parser) accept: enums and unions before
    everything (they must be declared before use), then structs with the
    structs they contain first, then tables (which may reference anything)."""
    enums = sorted(schema.enums.values(), key=lambda e: e.name)
    structs: list[Object] = []
    done: set[str] = set()
    for start in sorted((o for o in schema.objects.values() if o.is_struct), key=lambda o: o.name):
        work: list[tuple[Object, bool]] = [(start, False)]
        while work:                         # iterative post-order DFS
            o, ready = work.pop()
            if o.name in done:
                continue
            if ready:
                done.add(o.name)
                structs.append(o)
                continue
            work.append((o, True))
            for f in reversed(o.fields):
                if f.type.item == BaseType.OBJ and f.type.ref.name not in done:
                    work.append((f.type.ref, False))
    tables = sorted((o for o in schema.objects.values() if not o.is_struct), key=lambda o: o.name)
    return [*enums, *structs, *tables]


def render(schema: Schema) -> str:
    """Render a schema as .fbs text (round-trips through :func:`parse_fbs`).

    Every name is checked to be an identifier and every string is escaped,
    so a schema from an untrusted .bfbs cannot inject declarations.
    """
    for d in (*schema.objects.values(), *schema.enums.values()):
        _ident(d.name, "type name", qualified=True)
        for k in d.attributes:
            _ident(k, "attribute name")
    for o in schema.objects.values():
        for f in o.fields:
            _ident(f.name, "field name")
            for k in f.attributes:
                _ident(k, "attribute name")
    for e in schema.enums.values():
        for v in e.values:
            _ident(v.name, "enum value name")
            for k in v.attributes:
                _ident(k, "attribute name")
    out: list[str] = []
    builtin = BUILTIN_ATTRIBUTES
    user_attrs = sorted({k for d in (*schema.objects.values(), *schema.enums.values())
                         for k in d.attributes if k not in builtin}
                        | {k for o in schema.objects.values() for f in o.fields
                           for k in f.attributes if k not in builtin}
                        | {k for e in schema.enums.values() for v in e.values
                           for k in v.attributes if k not in builtin})
    for a in user_attrs:
        out.append(f'attribute "{a}";\n')
    if user_attrs:
        out.append("\n")
    decls = _declaration_order(schema)
    ns = ""
    for d in decls:
        dns = d.name.rpartition(".")[0]
        if dns != ns:
            ns = dns
            out.append(f"namespace {ns};\n\n" if ns else "namespace ;\n\n")
        out.append(_docs(d.documentation, ""))
        if isinstance(d, Enum):
            if d.is_union:
                out.append(f"union {d.short_name}{_attr_text(d.attributes)} {{\n")
                for v in d.values:
                    if v.value == 0 and v.name == "NONE":
                        continue
                    target = _rel(v.union_type.ref.name, ns)
                    member = v.name if v.name == target else f"{v.name}: {target}"
                    out.append(f"{_docs(v.documentation, '  ')}  {member} = {v.value},\n")
            else:
                ut = d.underlying.name.lower()
                out.append(f"enum {d.short_name}: {ut}{_attr_text(d.attributes)} {{\n")
                for v in d.values:
                    val = v.value
                    if d.bit_flags:
                        val = val.bit_length() - 1
                    out.append(f"{_docs(v.documentation, '  ')}  {v.name} = {val}{_attr_text(v.attributes)},\n")
            out.append("}\n\n")
            continue
        kind = "struct" if d.is_struct else "table"
        out.append(f"{kind} {d.short_name}{_attr_text(d.attributes)} {{\n")
        for f in d.fields:
            if f.type.item == BaseType.UTYPE and f.name.endswith("_type") and not d.is_struct:
                vf = d.fields_by_name.get(f.name[:-5])
                if vf is not None and vf.type.item == BaseType.UNION:
                    continue  # implied by the union field
            extra = []
            if not d.is_struct:
                extra.append(f"id: {f.id}")
            for flag in ("deprecated", "required", "key"):
                if getattr(f, flag):
                    extra.append(flag)
            default = ""
            if f.type.base.is_scalar and f.type.base != BaseType.UTYPE:
                if f.optional:
                    default = " = null"
                elif f.default not in (0, False, None) or (isinstance(f.default, float) and
                                                           (math.isnan(f.default) or math.copysign(1, f.default) < 0)):
                    if isinstance(f.default, bool):
                        default = f" = {'true' if f.default else 'false'}"
                    elif isinstance(f.default, float):
                        default = f" = {_fmt_float(f.default)}"
                    else:
                        default = f" = {f.default}"
            out.append(f"{_docs(f.documentation, '  ')}  {f.name}: {_type_text(f.type, ns)}"
                       f"{default}{_attr_text(f.attributes, extra)};\n")
        out.append("}\n\n")
    if schema.root is not None:
        if ns != schema.root.namespace:
            rns = schema.root.namespace
            out.append(f"namespace {rns};\n\n" if rns else "namespace ;\n\n")
        out.append(f"root_type {schema.root.short_name};\n")
    if schema.file_ident:
        out.append(f"file_identifier {_quote(schema.file_ident)};\n")
    if schema.file_ext:
        out.append(f"file_extension {_quote(schema.file_ext)};\n")
    return "".join(out)
