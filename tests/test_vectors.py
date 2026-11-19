from collections.abc import Sequence

import pytest

import flatview
from flatview import parse_fbs

SCHEMA = parse_fbs("""
    table Item { name: string (key); n: int; }
    table Num { id: long (key); }
    table Root { ints: [int]; names: [string]; items: [Item]; nums: [Num]; empty: [int]; }
    root_type Root;
""")


@pytest.fixture(scope="module")
def root():
    data = SCHEMA.encode({
        "ints": list(range(-5, 15)),
        "names": ["a", "bb", "ccc"],
        "items": [{"name": n, "n": i} for i, n in enumerate(["pear", "apple", "fig", "Zebra", "äpfel"])],
        "nums": [{"id": i} for i in (50, -3, 2**40, 0, 7)],
        "empty": [],
    })
    return SCHEMA.read(data)


def test_sequence_protocol(root):
    ints = root.ints
    assert isinstance(ints, Sequence)
    assert len(ints) == 20
    assert ints[0] == -5 and ints[-1] == 14
    assert list(ints) == list(range(-5, 15))
    assert 3 in ints and 99 not in ints
    assert ints.index(0) == 5
    assert list(reversed(ints))[0] == 14
    with pytest.raises(IndexError):
        ints[20]
    with pytest.raises(IndexError):
        ints[-21]


def test_slices_are_lazy_views(root):
    s = root.ints[2:6]
    assert type(s) is type(root.ints)
    assert s.tolist() == [-3, -2, -1, 0]
    assert s[-1] == 0 and len(s) == 4
    assert root.ints[::5] == [-5, 0, 5, 10]
    assert root.ints[100:] .tolist() == []
    assert root.items[1:3][0].name == root.items[1].name


def test_equality(root):
    assert root.ints == list(range(-5, 15))
    assert root.names == ("a", "bb", "ccc")
    assert root.empty == [] and len(root.empty) == 0


def test_string_keys_sorted_bytewise(root):
    # FlatBuffers compares keys as raw bytes (strcmp), so 'Z' < 'a' < 'ä'
    assert root.items.keys() == ["Zebra", "apple", "fig", "pear", "äpfel"]
    assert root.items.is_sorted()
    assert root.items.lookup("fig").n == 2
    assert root.items.lookup("äpfel").n == 4
    assert root.items.find("kiwi") is None
    assert root.items.has_key("pear") and not root.items.has_key("Pear")
    assert root.items.bisect("b") == 2


def test_integer_keys(root):
    assert root.nums.keys() == [-3, 0, 7, 50, 2**40]
    assert root.nums.lookup(2**40).id == 2**40
    assert root.nums.lookup(0).id == 0
    with pytest.raises(KeyError):
        root.nums.lookup(1)


def test_lookup_needs_key(root):
    s = parse_fbs("table A { x: int; } table R { a: [A]; } root_type R;")
    r = s.read(s.encode({"a": [{"x": 1}]}))
    with pytest.raises(TypeError, match="no key field"):
        r.a.lookup(1)
    assert r.a.index_by("x")[1].x == 1


def test_table_view_api(root):
    item = root.items.lookup("pear")
    assert item["name"] == item.name
    assert item._fields == ("name", "n")
    assert item._get("n", "missing") == "missing"     # n == 0 was not written
    assert item.n == 0
    assert "name" in dir(item)
    with pytest.raises(KeyError):
        item["nope"]
    with pytest.raises(AttributeError):
        item.nope
    assert root.items[0] == root.items[0]               # same table, same buffer
    assert root.items[0] != root.items[1]
    assert len({root.items[0], root.items[0]}) == 1


def test_to_python(root):
    assert flatview.to_python(root.ints) == list(range(-5, 15))
    assert flatview.to_python(root.items[0]) == {"name": "Zebra", "n": 3}


def test_field_named_like_a_method():
    s = parse_fbs("table T { keys: [int]; _asdict: int; get: int; } root_type T;")
    t = s.read(s.encode({"keys": [1], "_asdict": 5, "get": 2}))
    assert t.keys.tolist() == [1] and t.get == 2
    assert t["_asdict"] == 5        # attribute is the helper; item access still works
    assert t._asdict()["get"] == 2


# --- NaN keys: unordered, so they break sorting and binary search ------------

FK = parse_fbs("""
    table D { k: double (key); v: int; }
    table F { k: float (key); }
    table R { ds: [D]; fs: [F]; }
    root_type R;
""")
NAN, INF = float("nan"), float("inf")


@pytest.mark.parametrize("field, rows", [("ds", [{"k": 1.0}, {"k": NAN}]), ("fs", [{"k": NAN}])])
def test_nan_key_refused_by_encoder(field, rows):
    with pytest.raises(flatview.EncodeError, match="NaN cannot be used as a key") as e:
        FK.encode({field: rows})
    assert e.value.path == f"{field}[{len(rows) - 1}].k"          # the caller's index


def test_nan_key_detected_by_verifier_and_is_sorted():
    # [3, NaN, 1] is not sorted, but every comparison with NaN is False, so a
    # plain `k < prev` check would wave it through.
    data = FK.encode({"ds": [{"k": 3.0}, {"k": NAN}, {"k": 1.0}]}, sort_keys=False)
    with pytest.raises(flatview.VerificationError, match=r"ds\[1\]: key k is NaN"):
        FK.verify(data)
    FK.verify(data, check_sorted=False)
    assert not FK.read(data).ds.is_sorted()


def test_searching_for_nan():
    r = FK.read(FK.encode({"ds": [{"k": -1.0, "v": 1}, {"k": 2.0, "v": 2}]}))
    assert r.ds.find(NAN) is None and not r.ds.has_key(NAN)
    with pytest.raises(KeyError):
        r.ds.lookup(NAN)
    with pytest.raises(ValueError, match="NaN"):
        r.ds.bisect(NAN)


def test_infinite_and_signed_zero_keys_still_work():
    data = FK.encode({"ds": [{"k": INF, "v": 1}, {"k": -INF, "v": 2}, {"k": -0.0, "v": 3}, {"k": 5.5, "v": 4}]})
    FK.verify(data)
    ds = FK.read(data).ds
    assert ds.keys() == [-INF, -0.0, 5.5, INF] and ds.is_sorted()
    assert ds.lookup(INF).v == 1 and ds.lookup(-INF).v == 2 and ds.lookup(0.0).v == 3
