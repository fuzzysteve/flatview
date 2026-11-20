"""API edge cases: size-prefixed verified reads, identifiers with an explicit
root, encoder errors, struct hashing, malformed buffers read without verify."""

import math
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


# -- encoder limits --------------------------------------------------------------

DEEP = parse_fbs("table N { n: N; ns: [N]; v: int; } root_type N;")


def _nest(depth: int, vector: bool) -> dict:
    d = {"v": 0}
    for i in range(depth):
        d = {"ns": [d], "v": i} if vector else {"n": d, "v": i}
    return d


@pytest.mark.parametrize("vector,depth", [(False, 900), (True, 450)])
def test_deep_nesting_encodes(vector, depth):
    # the compiled encoder had cut the reachable depth (400 failed; 480 worked before it)
    data = DEEP.encode(_nest(depth, vector))
    node, n = DEEP.read(data), 0
    while node is not None:
        node = node.ns[0] if vector and node.ns else (None if vector else node.n)
        n += 1
    assert n == depth + 1


def test_too_deep_is_an_encode_error():
    with pytest.raises(EncodeError, match="nested too deeply"):
        DEEP.encode(_nest(5000, False))


BIG = parse_fbs("struct B { a: [ubyte:40000]; } table T { x: B; y: B; } root_type T;")


@pytest.mark.parametrize("wide", [False, True])
def test_table_over_64k_is_an_encode_error(monkeypatch, wide):
    import flatview._builder as fb
    if wide:                                   # the field-by-field path too
        monkeypatch.setattr(fb, "WIDE_TABLE", 0)
        BIG._clear_compiled()
    v = {"a": [0] * 40000}
    with pytest.raises(EncodeError, match=r"table T: 80,0\d\d bytes of inline data"):
        BIG.encode({"x": v, "y": v})
    BIG._clear_compiled()
    assert BIG.read(BIG.encode({"x": v})).x.a[0] == 0       # one struct fits


@pytest.mark.parametrize("text", ["nan", "NaN", "-nan", "inf", "+inf", "-inf", "Infinity", "-infinity"])
def test_non_finite_float_strings(text):
    s = parse_fbs("table T { d: double; f: float; v: [double]; } root_type T;")
    root = s.read(s.encode({"d": text, "f": text, "v": [text]}))
    want = float(text)
    for got in (root.d, root.f, root.v[0]):
        assert (math.isnan(got) and math.isnan(want)) or got == want


def test_other_strings_are_still_not_floats():
    s = parse_fbs("table T { d: double; } root_type T;")
    for bad in ("1.5", "nope", ""):
        with pytest.raises(EncodeError, match="expected a number"):
            s.encode({"d": bad})


FKEY = parse_fbs("table R { k: double (key); } table T { rs: [R]; } root_type T;")


@pytest.mark.parametrize("keys,want", [
    (["+inf", "-inf", 1, 2.5], [float("-inf"), 1.0, 2.5, float("inf")]),
    (["inf", 5.0, "-Infinity"], [float("-inf"), 5.0, float("inf")]),
])
def test_float_keys_sorted_by_their_value(keys, want):
    # keys were sorted as the raw strings ("+inf" < "-inf"), so the encoder
    # wrote vectors its own verifier rejected as unsorted
    data = FKEY.encode({"rs": [{"k": k} for k in keys]})
    FKEY.verify(data)
    assert FKEY.read(data).rs.keys() == want


@pytest.mark.parametrize("nan", ["nan", "-NaN", float("nan")])
def test_nan_key_refused_however_spelled(nan):
    with pytest.raises(EncodeError, match=r"rs\[0\]\.k: NaN cannot be used as a key"):
        FKEY.encode({"rs": [{"k": nan}]})


def test_bad_key_type_names_the_key():
    with pytest.raises(EncodeError, match=r"rs\[1\]\.k: expected a number"):
        FKEY.encode({"rs": [{"k": 1}, {"k": "x"}]})


def test_foreign_tables_are_not_kept_alive():
    # compiled code lives on its table, so using another schema's table via
    # root= leaves nothing of it (or its schema) in this schema
    import gc
    import weakref
    a = parse_fbs("table X { s: string; } root_type X;")
    b = parse_fbs("table X { n: int; } root_type X;")
    data = b.encode({"n": 1})
    a.verify(data, b.root)
    a.read(data, b.root)._asdict()
    a.encode({"n": 2}, b.root)
    gone = weakref.ref(b.root)
    del b
    gc.collect()
    assert gone() is None
