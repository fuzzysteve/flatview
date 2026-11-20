"""API edge cases: size-prefixed verified reads, identifiers with an explicit
root, encoder errors, struct hashing, malformed buffers read without verify."""

import struct

import pytest

import flatview
from flatview import EncodeError, FlatBufferError, VerificationError, parse_fbs

WIDE = parse_fbs('table T { a: double; b: long; } root_type T; file_identifier "WIDE";')


def test_verified_read_of_size_prefixed_buffer(tmp_path):
    # read() cut off the prefix before verifying, so 8-byte fields looked misaligned
    data = WIDE.encode({"a": 1.5, "b": 2}, size_prefixed=True)
    WIDE.verify(data, size_prefixed=True)
    assert WIDE.read(data, verify=True, size_prefixed=True).a == 1.5
    p = tmp_path / "x.bin"
    p.write_bytes(data)
    with flatview.open(p, WIDE, verify=True, size_prefixed=True) as fb:
        assert fb.root.b == 2


@pytest.mark.parametrize("root", [None, "T", "WIDE"])
def test_identifier_checked_however_root_is_named(root):
    bad = bytearray(WIDE.encode({"a": 1.0}))
    bad[4:8] = b"NOPE"
    root = WIDE.root if root == "WIDE" else root
    with pytest.raises(FlatBufferError, match="identifier"):
        WIDE.read(bytes(bad), root)
    with pytest.raises(VerificationError, match="identifier"):
        WIDE.verify(bytes(bad), root)
    WIDE.read(bytes(bad), root, check_identifier=False)


def test_identifier_not_checked_for_other_tables():
    s = parse_fbs('table A { x: int; } table R { a: A; } root_type R; file_identifier "RRRR";')
    data = s.encode({"x": 3}, "A")             # no identifier: A is not the root type
    assert s.read(data, "A", verify=True).x == 3


def test_huge_int_for_float_field_is_an_encode_error():
    with pytest.raises(EncodeError, match="does not fit"):
        WIDE.encode({"a": 10 ** 400})


def test_union_type_vector_out_of_range():
    s = parse_fbs("table M { x: int; } union U { M } table T { v: [U]; } root_type T;")
    for bad in (300, -1):
        with pytest.raises(EncodeError, match="out of range"):
            s.encode({"v_type": [bad], "v": [{"x": 1}]})
    assert s.read(s.encode({"v_type": ["M"], "v": [{"x": 1}]}))._asdict(enums="name") == \
        {"v_type": ["M"], "v": [{"x": 1}]}


STRUCTS = parse_fbs("""
    enum C : ubyte { Red, Green }
    struct P { x: float; c: C; }
    struct Q { p: P; n: int; }
    table T { p: P; q: Q; ps: [P]; } root_type T;
""")


def test_struct_hash_consistent_with_equality():
    root = STRUCTS.read(STRUCTS.encode({"p": {"x": 1.5, "c": "Green"}, "q": {"p": [0.5, 0], "n": 4},
                                        "ps": [[1.5, 1], [float("nan"), 0], [float("nan"), 0]]}))
    p = root.p
    assert p == (1.5, 1) and hash(p) == hash((1.5, 1))
    assert {(1.5, 1): "found"}[p] == "found"
    assert root.ps[0] == p and hash(root.ps[0]) == hash(p)
    assert root.ps[1] == root.ps[2] and hash(root.ps[1]) == hash(root.ps[2])   # NaN, same bytes
    hash(root.q)                                                                 # nested struct


def _corrupt_vtable(data: bytes) -> bytes:
    bad = bytearray(data)
    root = struct.unpack_from("<I", bad, 0)[0]
    struct.pack_into("<i", bad, root, root + 4)     # vtable 4 bytes before byte 0
    return bytes(bad)


def test_vtable_before_buffer_start_is_an_error_not_a_wraparound():
    # struct.unpack_from treats negative offsets as counting from the end
    data = _corrupt_vtable(WIDE.encode({"a": 1.0}))
    with pytest.raises(FlatBufferError, match="before the start"):
        WIDE.read(data).a
    with pytest.raises(FlatBufferError, match="before the start"):
        WIDE.read(data)._asdict()
    with pytest.raises(VerificationError):
        WIDE.verify(data)


def test_vtable_before_start_in_key_lookup():
    s = parse_fbs("table R { k: int (key); } table T { rs: [R]; } root_type T;")
    data = bytearray(s.encode({"rs": [{"k": 1}, {"k": 2}]}))
    rs = s.read(bytes(data)).rs
    pos = rs._pos_at(0)
    struct.pack_into("<i", data, pos, pos + 2)
    with pytest.raises(FlatBufferError, match="before the start"):
        s.read(bytes(data)).rs.find(1)


def test_union_vector_without_types_same_error_in_both_decoders():
    s = parse_fbs("table M { x: int; } union U { M } table T { v: [U]; } root_type T;")
    from flatview._builder import Builder
    b = Builder()
    m = b.table([(0, "i", 5, 4)])
    vec = b.create_offset_vector([m])
    data = b.finish(b.table([(1, "offset", vec, 4)]))          # v without v_type
    with pytest.raises(FlatBufferError):
        s.read(data).v
    with pytest.raises(FlatBufferError):
        s.read(data)._asdict()
