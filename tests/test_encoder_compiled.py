"""The compiled per-table encoders against the general field-by-field path.

Both must write identical bytes and raise identical errors (message and
path). WIDE_TABLE = 0 makes every table use the general path.
"""

import math

import pytest

import flatview
import flatview._builder as fb
from flatview import EncodeError, parse_fbs

from conftest import DATA, KITCHEN
from test_evolution import NEW_VALUE, OLD_VALUE
from test_old_bfbs import KITCHEN_VALUE
from test_union_members import CASES

SCALARS = parse_fbs("""
    enum C : ubyte { Red, Green }
    enum F : ushort (bit_flags) { A, B }
    struct P { x: float; y: float; }
    table K { k: int (key); }
    table T {
        b: bool; i8: byte = -3; u8: ubyte; i16: short; u16: ushort; i32: int = 7; u32: uint;
        i64: long; u64: ulong; f: float = 1.5; d: double = -0.0; z: double; nd: double = nan;
        o: int = null; of: float = null; c: C = Green; fl: F; s: string (required);
        p: P; ks: [K]; v: [int];
    }
    root_type T;
""")

GOOD = [
    {"s": ""},
    {"s": "x", "b": True, "i8": -3, "u8": 255, "i16": -32768, "u16": 65535, "i32": 7, "u32": 1,
     "i64": -(1 << 63), "u64": (1 << 64) - 1, "f": 1.5, "d": 0.0, "nd": float("nan"),
     "o": 0, "of": 0.0, "c": "Red", "fl": "A B", "p": [1, 2], "ks": [{"k": 2}, {"k": 1}], "v": [1]},
    {"s": "y", "b": 1, "i32": 8, "f": 2, "d": -0.0, "c": 1, "fl": ["A"], "u8": True and 1},
    {"s": "z", "f": float("inf"), "d": float("-inf"), "of": float("nan"), "c": flatview.BaseType.UTYPE},
]

BAD = [
    {},                                    # required s missing
    {"s": 3},
    {"s": "\ud800"},
    {"s": "x", "nope": 1},
    {"s": "x", "u8": 256}, {"s": "x", "u8": -1}, {"s": "x", "i8": 1.0}, {"s": "x", "b": 2},
    {"s": "x", "b": "true"}, {"s": "x", "u64": 1 << 64}, {"s": "x", "f": 1e39}, {"s": "x", "f": 10 ** 400},
    {"s": "x", "d": "1"}, {"s": "x", "c": "Blue"}, {"s": "x", "fl": "A C"}, {"s": "x", "i32": True},
    {"s": "x", "p": [1]}, {"s": "x", "ks": [{"k": "a"}]}, {"s": "x", "v": ["a"]}, {"s": "x", "ks": 5},
]


def both(schema, value, **kw):
    """(compiled result, general result): bytes or the error's text."""
    def run():
        schema.__dict__.pop("_encoders", None)
        try:
            return schema.encode(value, **kw)
        except EncodeError as e:
            return f"EncodeError {e}"
    compiled = run()
    old = fb.WIDE_TABLE
    fb.WIDE_TABLE = 0
    try:
        general = run()
    finally:
        fb.WIDE_TABLE = old
        schema.__dict__.pop("_encoders", None)
    return compiled, general


@pytest.mark.parametrize("kw", [{}, {"force_defaults": True}, {"strict": False}, {"share_strings": False}])
@pytest.mark.parametrize("i", range(len(GOOD) + len(BAD)))
def test_scalars_and_errors(i, kw):
    value = (GOOD + BAD)[i]
    compiled, general = both(SCALARS, value, **kw)
    assert compiled == general
    if i < len(GOOD):
        assert isinstance(compiled, bytes)
        SCALARS.verify(compiled)
    elif not kw.get("strict", True) and "nope" in value:
        assert isinstance(compiled, bytes)
    else:
        assert compiled.startswith("EncodeError")


def test_float_defaults_and_sign():
    root = SCALARS.read(SCALARS.encode({"s": "", "f": 1.5, "z": 0.0}))   # equal to defaults: omitted
    assert not root._has("f") and not root._has("z")
    root = SCALARS.read(SCALARS.encode({"s": "", "z": -0.0}))            # -0.0 keeps its sign
    assert root._has("z") and math.copysign(1, root.z) == -1
    root = SCALARS.read(SCALARS.encode({"s": "", "nd": float("nan")}))   # NaN never equals its default
    assert root._has("nd")


def test_non_sequence_for_vector_is_an_encode_error():
    with pytest.raises(EncodeError, match=r"ks: expected a sequence"):
        SCALARS.encode({"s": "", "ks": 5})


@pytest.mark.parametrize("name,path,value", [
    ("kitchen", KITCHEN / "kitchen.fbs", KITCHEN_VALUE),
    ("v1", DATA / "evolution" / "v1.fbs", OLD_VALUE),
    ("v2", DATA / "evolution" / "v2.fbs", NEW_VALUE),
    *[(f"members-{k}", DATA / "unions" / "members.fbs", v) for k, v in CASES.items()],
])
def test_real_schemas(name, path, value):
    s = flatview.load_schema(path)
    compiled, general = both(s, value)
    assert isinstance(compiled, bytes) and compiled == general
    view = s.read(compiled)
    assert both(s, view) == (compiled, compiled)           # copying from a view


def test_layout_caches_are_bounded():
    s = parse_fbs("table T { " + " ".join(f"f{i}: int;" for i in range(16)) + " } root_type T;")
    for m in range(1, 3000):          # 3000 different field patterns
        s.encode({f"f{i}": 1 for i in range(16) if m >> i & 1})
    assert len(fb._LAYOUTS) <= 4096
    lay = fb.table_encoder(s, s.root, True, False).__globals__["LAYOUTS"]
    assert len(lay) <= 1024
