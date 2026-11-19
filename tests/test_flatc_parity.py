"""Verifier/decoder behaviour pinned to flatc's generated C++ verifier.

Each case was found by fuzz/verifier_parity.py and its expected outcome is
what flatc does. These tests need no oracle; `FLATVIEW_ORACLE=1 pytest -m
oracle` additionally runs a short differential fuzz against flatc itself.
"""

import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

import flatview
from flatview import BaseType, VerificationError, parse_fbs
from flatview._builder import Builder

from conftest import FLATC, KITCHEN

U = parse_fbs("""
    table A { a: int; }
    table B { b: int; }
    union AB { A, B }
    enum Flags : ushort (bit_flags) { X, Y }
    table T { u: AB; us: [AB]; d: [double]; f: Flags; }
    root_type T;
""")


def table_t(items):
    b = Builder()
    return b, items


def build(fn) -> bytes:
    b = Builder()
    root = fn(b)
    return b.finish(root)


def a_table(b, v=7):
    return b.table([(0, "i", v, 4)])


# T field ids: u_type 0, u 1, us_type 2, us 3, d 4, f 5

def test_union_type_vector_without_values_rejected():
    data = build(lambda b: b.table([(2, "offset", b.create_scalar_vector(BaseType.UTYPE, [1]), 4)]))
    with pytest.raises(VerificationError, match="without its value vector"):
        U.verify(data)


def test_union_value_vector_without_types_rejected():
    data = build(lambda b: b.table([(3, "offset", b.create_offset_vector([a_table(b)]), 4)]))
    with pytest.raises(VerificationError, match="without a type vector"):
        U.verify(data)


def test_unknown_union_type_accepted_and_reads_as_none():
    # A member added by a newer schema (type 9) must not make old readers fail.
    def f(b):
        t = a_table(b)
        return b.table([(0, "B", 9, 1), (1, "offset", t, 4)])
    data = build(f)
    U.verify(data)
    assert U.read(data).u is None
    assert U.read(data)._asdict(defaults=False)["u"] is None
    with pytest.raises(VerificationError, match="not a member"):
        U.verify(data, strict_enums=True)


def test_unknown_union_type_in_vector_not_followed():
    def f(b):
        t = a_table(b)
        vals = b.create_offset_vector([t, t])
        types = b.create_scalar_vector(BaseType.UTYPE, [1, 9])
        return b.table([(2, "offset", types, 4), (3, "offset", vals, 4)])
    data = build(f)
    U.verify(data)
    r = U.read(data)
    assert r.us[0].a == 7 and r.us[1] is None
    assert r._asdict(defaults=False)["us"] == [{"a": 7}, None]


def test_none_union_type_with_value_is_ignored():
    def f(b):
        t = a_table(b)
        return b.table([(1, "offset", t, 4)])          # value present, type absent (NONE)
    data = build(f)
    U.verify(data)
    assert U.read(data).u is None


@pytest.mark.parametrize("vtlen", [0, 2])
def test_short_vtable_means_no_fields(vtlen):
    s = parse_fbs("table T { a: int; b: string; } root_type T;")
    # root offset, vtable (vtlen bytes, padded), table whose soffset points at it
    vt = struct.pack("<H", vtlen) + (b"\0\0" if vtlen == 2 else b"\0\0")
    data = struct.pack("<I", 8) + vt + struct.pack("<i", 4)
    s.verify(data)
    assert s.read(data).a == 0 and s.read(data).b is None
    assert s.read(data)._asdict() == {"a": 0, "b": None}          # compiled converter agrees
    with pytest.raises(VerificationError):
        s.verify(data, strict_table_size=True)


def test_misaligned_vtable_rejected():
    s = parse_fbs("table T { a: int; } root_type T;")
    good = s.encode({"a": 5})
    # move the root table's vtable to an odd address by rewriting soffset
    root = struct.unpack_from("<I", good)[0]
    soff = struct.unpack_from("<i", good, root)[0]
    bad = bytearray(good + b"\0")
    struct.pack_into("<i", bad, root, soff - 1 if (root - soff) % 2 == 0 else soff)
    with pytest.raises(VerificationError, match="aligned"):
        s.verify(bytes(bad))


def test_vector_elements_need_only_length_alignment():
    # [double] whose data starts 4- but not 8-aligned: flatc accepts it
    def f(b):
        b.prep(8, 0)
        b.place_bytes(struct.pack("<d", 2.5))
        b.place("<I", 1, 4)                            # length at an address = 4 mod 8
        vec = b.offset()
        return b.table([(4, "offset", vec, 4)])
    data = build(f)
    U.verify(data)
    assert U.read(data).d.tolist() == [2.5]


def test_bit_flags_with_unknown_bits_keep_the_number():
    data = U.encode({"f": 1 | 2 | 16})                # X | Y | an undefined bit
    r = U.read(data)
    assert r._asdict(enums="name", defaults=False)["f"] == 19
    assert flatview.to_python(r, enums="name")["f"] == 19
    assert U.read(U.encode({"f": 3}))._asdict(enums="name", defaults=False)["f"] == "X Y"


@pytest.mark.oracle
@pytest.mark.skipif(os.environ.get("FLATVIEW_ORACLE") != "1", reason="set FLATVIEW_ORACLE=1 (builds flatc's C++)")
@pytest.mark.skipif(not (FLATC and shutil.which("g++")), reason="needs flatc and g++")
def test_differential_fuzz_against_flatc():
    script = Path(__file__).parent.parent / "fuzz" / "verifier_parity.py"
    res = subprocess.run([sys.executable, str(script), "--seconds", "30", "--seed", "11"],
                         capture_output=True, text=True, timeout=900)
    assert res.returncode == 0, res.stderr
    assert "ours-accepts-flatc-rejects" not in res.stdout, res.stdout
    assert "decode-differs" not in res.stdout and "decode-ours-fails" not in res.stdout, res.stdout


@pytest.mark.oracle
@pytest.mark.skipif(os.environ.get("FLATVIEW_ORACLE") != "1", reason="set FLATVIEW_ORACLE=1 (builds flatc's C++)")
@pytest.mark.skipif(not (FLATC and shutil.which("g++")), reason="needs flatc and g++")
def test_encoder_fuzz_against_flatc():
    script = Path(__file__).parent.parent / "fuzz" / "encoder_parity.py"
    res = subprocess.run([sys.executable, str(script), "--seconds", "40", "--seed", "21"],
                         capture_output=True, text=True, timeout=900)
    assert res.returncode == 0, res.stderr
    assert "findings (kind, bucket): count" in res.stdout and res.stdout.rstrip().endswith("none"), res.stdout
