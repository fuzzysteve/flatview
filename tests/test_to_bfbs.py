"""Schema.to_bfbs(): writing binary schemas.

The output must load back to the same schema, decode data the same way, and
hold what flatc writes for the same .fbs (`--bfbs-comments --bfbs-builtins`),
field for field. flatc must also accept it as a schema.
"""

import json
import math
import subprocess
from pathlib import Path

import pytest

import flatview
from flatview._fbs import BUILTIN_ATTRIBUTES

from conftest import DATA, FLATC, KITCHEN, SDE_SCHEMAS, flatc_bfbs, needs_flatc, normalise
from test_old_bfbs import KITCHEN_VALUE

SOURCES = [
    KITCHEN / "kitchen.fbs",
    DATA / "unions" / "members.fbs",
    DATA / "evolution" / "v1.fbs",
    DATA / "evolution" / "v2.fbs",
    Path(flatview.__file__).parent / "reflection.fbs",
    *sorted(SDE_SCHEMAS.glob("*.fbs")),
]
IDS = [p.name for p in SOURCES]
REFL = flatview.reflection_schema()


def _without_docs(n: dict) -> dict:
    for o in n["objects"].values():
        o["documentation"] = []
        o["fields"] = [f[:-1] + ([],) for f in o["fields"]]
    for e in n["enums"].values():
        e["documentation"] = []
        e["values"] = [v[:-1] + ([],) for v in e["values"]]
    return n


@pytest.mark.parametrize("path", SOURCES, ids=IDS)
def test_round_trip(path):
    s = flatview.load_schema(path)
    again = flatview.load_bfbs(s.to_bfbs())
    assert normalise(again, builtins=True) == normalise(s, builtins=True)


def test_without_comments_or_builtins():
    s = flatview.load_schema(KITCHEN / "kitchen.fbs")
    plain = flatview.load_bfbs(s.to_bfbs(comments=False, builtins=False))
    assert normalise(plain) == _without_docs(normalise(s))
    for o in plain.objects.values():
        for f in o.fields:
            assert not set(f.attributes) & BUILTIN_ATTRIBUTES


def test_bfbs_loaded_schema_round_trips():
    for path in sorted(SDE_SCHEMAS.glob("*.bfbs"))[:20]:
        s = flatview.load_schema(path)
        assert normalise(flatview.load_bfbs(s.to_bfbs()), builtins=True) == normalise(s, builtins=True)


def test_decodes_like_the_source_schema():
    s = flatview.load_schema(KITCHEN / "kitchen.fbs")
    t = flatview.load_bfbs(s.to_bfbs())
    data = s.encode(KITCHEN_VALUE)
    t.verify(data, strict_enums=True)
    assert t.read(data)._asdict(deprecated=True) == s.read(data)._asdict(deprecated=True)


def test_nested_flatbuffer_survives():
    s = flatview.parse_fbs('table I { x: int; } table O { n: [ubyte] (nested_flatbuffer: "I"); } root_type O;')
    t = flatview.load_bfbs(s.to_bfbs())
    assert t.read(s.encode({"n": {"x": 5}})).n.x == 5


def test_root_table_shares_the_object_entry():
    raw = REFL.read(flatview.load_schema(KITCHEN / "kitchen.fbs").to_bfbs())
    assert raw.root_table._offset in {o._offset for o in raw.objects}


def test_declaration_files_relative_to_the_main_schema():
    s = flatview.load_schema(KITCHEN / "kitchen.fbs")
    raw = REFL.read(s.to_bfbs())
    assert {o.declaration_file for o in raw.objects} == {"//kitchen.fbs", "//common.fbs"}
    # text with no file behind it records none
    raw = REFL.read(flatview.parse_fbs("table T { x: int; } root_type T;").to_bfbs())
    assert raw.objects[0].declaration_file is None


def test_public_encode_does_not_share_tables():
    s = flatview.parse_fbs("table A { x: int; } table T { a: A; b: A; } root_type T;")
    a = {"x": 1}
    root = s.read(s.encode({"a": a, "b": a}))
    assert root.a._offset != root.b._offset


def _canon(data: bytes) -> dict:
    d = REFL.read(data)._asdict(enums="value")
    d.pop("fbs_files")                 # the include graph isn't part of the model
    return json.loads(json.dumps(d, default=str).replace("NaN", '"nan"'))


@needs_flatc
@pytest.mark.parametrize("path", SOURCES, ids=IDS)
def test_same_content_as_flatc(path):
    s = flatview.load_schema(path)
    assert _canon(s.to_bfbs()) == _canon(flatc_bfbs(path, builtins=True))


@needs_flatc
def test_flatc_uses_it_as_a_schema(tmp_path):
    s = flatview.load_schema(KITCHEN / "kitchen.fbs")
    (tmp_path / "k.bfbs").write_bytes(s.to_bfbs())
    (tmp_path / "k.bin").write_bytes(s.encode(KITCHEN_VALUE))
    subprocess.run([FLATC, "--json", "--strict-json", "--raw-binary", "-o", str(tmp_path),
                    str(tmp_path / "k.bfbs"), "--", str(tmp_path / "k.bin")], check=True, capture_output=True)
    out = json.loads((tmp_path / "k.json").read_text())
    assert out["ships"][0]["name"] == "Rifter" and math.isclose(out["ships"][0]["ratio"], 0.25)
