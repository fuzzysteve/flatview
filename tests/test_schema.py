import math

import pytest

import flatview
from flatview import BaseType, SchemaError, parse_fbs

from conftest import KITCHEN, SDE_SCHEMAS, flatc_bfbs, needs_flatc, normalise

SDE_SCHEMA_NAMES = sorted(p.stem for p in SDE_SCHEMAS.glob("*.fbs"))


def test_all_sde_schemas_present():
    assert len(SDE_SCHEMA_NAMES) == 104


@pytest.mark.parametrize("name", SDE_SCHEMA_NAMES)
def test_sde_fbs_matches_bfbs(name):
    """Our .fbs parser and our .bfbs loader (fed CCP's flatc output) agree exactly."""
    from_text = flatview.load_schema(SDE_SCHEMAS / f"{name}.fbs")
    from_bin = flatview.load_schema(SDE_SCHEMAS / f"{name}.bfbs")
    assert normalise(from_text) == normalise(from_bin)


@pytest.mark.parametrize("name", SDE_SCHEMA_NAMES)
def test_sde_render_round_trip(name):
    schema = flatview.load_schema(SDE_SCHEMAS / f"{name}.bfbs")
    again = parse_fbs(schema.to_fbs(), f"{name}-rendered.fbs")
    assert normalise(again) == normalise(schema)


@needs_flatc
@pytest.mark.parametrize("builtins", [False, True])
def test_kitchen_fbs_matches_flatc_bfbs(kitchen_schema, builtins):
    bfbs = flatview.load_bfbs(flatc_bfbs(KITCHEN / "kitchen.fbs", builtins=builtins))
    assert normalise(bfbs, builtins) == normalise(kitchen_schema, builtins)


def test_kitchen_render_round_trip(kitchen_schema):
    again = parse_fbs(kitchen_schema.to_fbs())
    assert normalise(again) == normalise(kitchen_schema)


def test_kitchen_layout(kitchen_schema):
    s = kitchen_schema
    pair = s["Pair"]
    assert (pair.bytesize, pair.minalign) == (16, 8)
    assert [(f.name, f.offset, f.padding) for f in pair.fields] == [("a", 0, 6), ("b", 8, 0)]
    box = s["Box"]
    assert box.minalign == 16 and box.bytesize % 16 == 0
    assert [f.name for f in box.fields] == ["min", "tag", "counts", "pairs", "slot"]
    ship = s["Ship"]
    # union value field id N, its implied _type field id N-1
    assert ship.field("primary_type").id + 1 == ship.field("primary").id
    assert ship.field("primary_type").type.base == BaseType.UTYPE
    assert ship.field("cargo_type").type.element == BaseType.UTYPE
    assert ship.field("mass").optional and ship.field("mass").default is None
    assert ship.field("big").default == 2**63 - 1
    assert ship.field("ratio").default == math.inf
    assert ship.field("enabled").default is True
    assert ship.field("old_field").deprecated
    assert ship.field("typeID").key
    assert ship.field("typeID").attributes["priority"] == "1"
    assert ship.attributes["priority"] == "3"
    assert ship.field("typeID").documentation == [" the ship's type"]
    flags = s["Flags"]
    assert flags.bit_flags
    assert [(v.name, v.value) for v in flags.values] == [("Published", 1), ("Repackable", 2), ("Dynamic", 32)]
    fitting = s["Fitting"]
    assert [(v.name, v.value, v.union_type.ref and v.union_type.ref.name) for v in fitting.values] == [
        ("NONE", 0, None), ("Module", 1, "kitchen.Module"), ("Charge", 2, "kitchen.Charge"),
        ("Ammo", 3, "kitchen.Charge")]
    assert s.root is s["Fleet"]
    assert s.file_ident == "FLT1" and s.file_ext == "flt"
    assert s["Race"].name == "kitchen.common.Race"   # resolved through the include


def test_reflection_schema_bootstraps():
    refl = flatview.reflection_schema()
    assert refl.file_ident == "BFBS"
    assert refl.root.name == "reflection.Schema"


def test_python_enums(kitchen_schema):
    Slot = kitchen_schema["Slot"].python
    assert Slot.Rig == 10 and Slot(-1).name == "Low"
    Flags = kitchen_schema["Flags"].python
    assert Flags.Published | Flags.Repackable == 3


def test_short_and_qualified_lookup(kitchen_schema):
    assert kitchen_schema["Ship"] is kitchen_schema["kitchen.Ship"]
    with pytest.raises(KeyError):
        kitchen_schema["Nope"]


@pytest.mark.parametrize("text, message", [
    ("table T { a: Missing; }", "unknown type 'Missing'"),
    ("table T { a: int; a: int; }", "duplicate field a"),
    ("table T { a: int (id: 0); b: int; }", "either all or none"),
    ("table T { a: int (id: 0); b: int (id: 2); }", "without gaps"),
    ("struct S { a: string; }", "structs may only contain"),
    ("struct S { a: [int]; }", "structs cannot contain vectors"),
    ("table T { a: [int:3]; }", "only allowed in structs"),
    ("enum E : float { A }", "integer type"),
    ("enum E : ubyte { A = 300 }", "out of range"),
    ("table T { a: ubyte = 256; }", "out of range"),
    ("table T { a: int; } root_type S;", "unknown type 'S'"),
    ('file_identifier "TOOLONG";', "exactly 4"),
    ("table T { a: int = null (required); }", "cannot be required"),
    ("struct S { s: S; }", "contains itself"),
    ("table T { a: int }", "expected ';'"),
])
def test_schema_errors(text, message):
    with pytest.raises(SchemaError, match=message):
        parse_fbs(text)


def test_error_has_line_number():
    with pytest.raises(SchemaError, match=r"<string>:3:"):
        parse_fbs("namespace a;\n\ntable T { x: Nope; }")


def test_namespace_resolution():
    s = parse_fbs("""
        namespace a.b; table Inner { v: int; }
        namespace a.c; table Other { v: int; }
        namespace a.b.d; table Outer { i: Inner; o: a.c.Other; }
        root_type Outer;
    """)
    outer = s.root
    assert outer.field("i").type.ref.name == "a.b.Inner"
    assert outer.field("o").type.ref.name == "a.c.Other"


def test_defaults_and_literals():
    s = parse_fbs("""
        enum Color : byte { Red = 1, Green, Blue = 8 }
        table T { a: int = 0x10; b: double = -inf; c: float = nan; d: Color = Blue;
                  e: bool = false; f: short = -32768; g: double = 1e3; h: int = null; }
    """)
    t = s["T"]
    assert t.field("a").default == 16
    assert t.field("b").default == -math.inf
    assert math.isnan(t.field("c").default)
    assert t.field("d").default == 8
    assert t.field("e").default is False
    assert t.field("f").default == -32768
    assert t.field("g").default == 1000.0
    assert t.field("h").default is None and t.field("h").optional
    assert [v.value for v in s["Color"].values] == [1, 2, 8]


def test_rpc_service_is_ignored():
    s = parse_fbs("""
        table Req { a: int; } table Resp { b: int; }
        rpc_service Svc { Call(Req): Resp (streaming: "none"); }
        root_type Req;
    """)
    assert s.root.name == "Req"
