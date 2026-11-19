"""Unions whose members are strings and structs, not only tables, checked
against flatc in both directions (single union fields and vectors of unions).
"""

import pytest

import flatview

from conftest import DATA, close_enough, flatc_from_json, flatc_to_json, needs_flatc

SCHEMA_PATH = DATA / "unions" / "members.fbs"
S = flatview.load_schema(SCHEMA_PATH)

CASES = {
    "table": {"id": 1, "body_type": "Label", "body": {"text": "hi", "size": 30}},
    "string": {"id": 2, "body_type": "Note", "body": "a note ✓"},
    "struct": {"id": 3, "body_type": "Point", "body": {"x": 1.5, "y": -2.25, "tag": 7}},
    "empty string": {"id": 4, "body_type": "Note", "body": ""},
    "vector of mixed members": {
        "id": 5,
        "parts_type": ["Label", "Note", "Point", "Note", "Point"],
        "parts": [{"text": "first"}, "second", {"x": -0.5, "y": 1e300, "tag": 255}, "", {"x": 0, "y": 0, "tag": 0}],
    },
    "everything": {
        "id": 6, "body_type": "Point", "body": {"x": 3.0, "y": 4.0, "tag": 1},
        "parts_type": ["Point", "Label"], "parts": [{"x": 1, "y": 2, "tag": 3}, {"size": 1}],
    },
}


def expected(v: dict) -> dict:
    """What a decode with defaults=False, enums="name" should give."""
    out = dict(v)
    if "body" in out and out["body_type"] == "Point":
        out["body"] = {k: float(x) if k != "tag" else x for k, x in out["body"].items()}
    if "parts" in out:
        out["parts"] = [{k: float(x) if k != "tag" else x for k, x in p.items()} if t == "Point" else p
                        for t, p in zip(out["parts_type"], out["parts"])]
    return out


@pytest.mark.parametrize("name", CASES)
def test_round_trip(name):
    value = CASES[name]
    data = S.encode(value)
    S.verify(data, strict_enums=True)
    m = S.read(data)
    assert m._asdict(defaults=False, enums="name") == expected(value)
    if "body" in value:
        body = m.body
        kind = value["body_type"]
        if kind == "Note":
            assert body == value["body"] and isinstance(body, str)
        elif kind == "Point":
            assert isinstance(body, flatview.StructView)
            assert (body.x, body.y, body.tag) == tuple(float(value["body"][k]) if k != "tag" else value["body"][k]
                                                       for k in ("x", "y", "tag"))
        else:
            assert body.text == value["body"].get("text")


def test_compiled_and_view_paths_agree():
    data = S.encode(CASES["vector of mixed members"])
    m = S.read(data)
    view_side = [p if isinstance(p, str) else p._asdict() for p in m.parts]
    assert m._asdict()["parts"] == view_side


def test_struct_member_is_aligned_and_verified():
    data = bytearray(S.encode(CASES["struct"]))
    S.verify(bytes(data))
    off = S.read(bytes(data)).body._offset
    assert off % 8 == 0                       # Point contains a double
    with pytest.raises(flatview.VerificationError):
        S.verify(bytes(data[:off + 4]))       # truncated in the middle of the struct


@needs_flatc
@pytest.mark.parametrize("name", CASES)
def test_flatc_reads_our_encoding(name):
    value = CASES[name]
    close_enough(flatc_to_json(SCHEMA_PATH, S.encode(value)), expected(value))


@needs_flatc
@pytest.mark.parametrize("name", CASES)
def test_we_read_flatc_encoding(name):
    value = CASES[name]
    data = flatc_from_json(SCHEMA_PATH, value)
    S.verify(data, strict_enums=True)
    close_enough(S.read(data)._asdict(defaults=False, enums="name"), expected(value))
