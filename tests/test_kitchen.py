"""Every schema feature, cross-checked with flatc in both directions."""

import copy
import math
import struct

import pytest

import flatview
from flatview import EncodeError, FlatBufferError, VerificationError

from conftest import KITCHEN, SDE, close_enough, flatc_from_json, flatc_to_json, needs_flatc

SCHEMA_PATH = KITCHEN / "kitchen.fbs"


def blueprint_bytes(schema):
    return schema.encode({"typeID": 999, "runs": 7}, "Blueprint")


@pytest.fixture
def fleet(kitchen_schema):
    return {
        "name": "Home Defence",
        "ships": [
            {
                "typeID": 587,
                "name": "Rifter",
                "race": "Minmatar",
                "mass": 1067000.0,
                "hp": 1200,
                "position": {"x": 1.0, "y": -2.5, "z": 3e10},
                "bounds": {
                    "min": {"x": 0.0, "y": 0.0, "z": 0.0}, "tag": 7, "counts": [1, 2, 3],
                    "pairs": [{"a": -1, "b": 2**40}, {"a": 32767, "b": -(2**63)}], "slot": "Rig",
                },
                "modules": [
                    {"name": "Small Shield Booster I", "slot": "Mid", "cpu": 15.0,
                     "flags": "Published Repackable"},
                    {"name": "200mm AutoCannon I", "slot": "High", "flags": "Published Dynamic"},
                    {"name": "Damage Control I", "slot": "Low"},
                ],
                "tags": ["frigate", "t1", "ünïcödé ✓"],
                "slots": ["High", "High", "Mid", "Low", "Rig"],
                "raw": [0, 1, 254, 255],
                "points": [{"x": 1.0, "y": 2.0, "z": 3.0}, {"x": -1.0, "y": -2.0, "z": -3.0}],
                "primary_type": "Module",
                "primary": {"name": "Afterburner I", "slot": "Mid"},
                "cargo_type": ["Charge", "Ammo", "Module"],
                "cargo": [{"typeID": 178, "volume": 0.0025}, {"typeID": 179}, {"name": "Spare Booster"}],
                "blueprint": list(blueprint_bytes(kitchen_schema)),
                "big": 2**64 - 1,
                "tiny": -128,
                "ratio": 0.5,
                "enabled": False,
                "maybe": False,
            },
            {"typeID": 24698, "name": "Drake", "race": "Caldari", "enabled": True},
            {"typeID": 0, "name": "Zero Key"},
        ],
        "by_name": [{"name": "zeta"}, {"name": "alpha"}, {"name": "Mu"}, {"name": "beta"}],
    }


# ---------------------------------------------------------------------------
# reading

@needs_flatc
def test_read_flatc_encoded(kitchen_schema, fleet):
    data = flatc_from_json(SCHEMA_PATH, fleet)
    check_fleet(kitchen_schema, data)


def test_read_flatview_encoded(kitchen_schema, fleet):
    check_fleet(kitchen_schema, kitchen_schema.encode(fleet))


def check_fleet(schema, data):
    assert flatview.has_identifier(data, "FLT1")
    schema.verify(data, strict_enums=True)
    root = schema.read(data)
    assert root.name == "Home Defence"
    ships = root.ships
    rifter = ships.lookup(587)
    assert rifter.name == "Rifter"
    Race = schema["Race"].python
    assert rifter.race is Race.Minmatar
    assert rifter.mass == 1067000.0
    assert rifter.hp == 1200
    assert tuple(rifter.position) == (1.0, -2.5, 3e10)
    x, y, z = rifter.position
    assert rifter.position == (1.0, -2.5, 3e10)
    b = rifter.bounds
    assert b.tag == 7 and b.counts.tolist() == [1, 2, 3] and b.slot.name == "Rig"
    assert b.pairs[1].a == 32767 and b.pairs[1].b == -(2**63) and b.pairs[0].b == 2**40
    assert tuple(b.min) == (0.0, 0.0, 0.0)
    mods = rifter.modules
    # keyed by name, so stored sorted (flatc's JSON parser sorts them too)
    assert [m.name for m in mods] == ["200mm AutoCannon I", "Damage Control I", "Small Shield Booster I"]
    Flags = schema["Flags"].python
    assert mods.lookup("Small Shield Booster I").flags == Flags.Published | Flags.Repackable
    assert mods.lookup("200mm AutoCannon I").flags == Flags.Published | Flags.Dynamic
    assert mods.lookup("Damage Control I").flags == Flags.Published  # default
    assert mods.lookup("200mm AutoCannon I").cpu == 1.5               # default
    assert mods.lookup("Small Shield Booster I").cpu == 15.0
    assert list(rifter.tags) == ["frigate", "t1", "ünïcödé ✓"]
    assert [s.name for s in rifter.slots] == ["High", "High", "Mid", "Low", "Rig"]
    assert rifter.raw.tobytes() == bytes([0, 1, 254, 255])
    assert [tuple(p) for p in rifter.points] == [(1.0, 2.0, 3.0), (-1.0, -2.0, -3.0)]
    assert rifter.primary_type.name == "Module"
    assert rifter.primary.name == "Afterburner I"
    assert [t.name for t in rifter.cargo_type] == ["Charge", "Ammo", "Module"]
    cargo = rifter.cargo
    assert cargo[0].typeID == 178 and cargo[0].volume == 0.0025
    assert cargo[1].volume == 0.01 and cargo[2].name == "Spare Booster"
    bp = rifter.blueprint
    assert bp.typeID == 999 and bp.runs == 7
    assert rifter.big == 2**64 - 1 and rifter.tiny == -128 and rifter.ratio == 0.5
    assert rifter.enabled is False and rifter.maybe is False
    drake = ships.lookup(24698)
    assert drake.mass is None and drake.hp == -1 and drake.ratio == math.inf
    assert drake.enabled is True and drake.maybe is None
    assert drake.big == 2**63 - 1
    assert drake.modules is None and drake.primary is None and drake.bounds is None
    assert ships.lookup(0).name == "Zero Key"
    assert root.by_name.lookup("Mu").name == "Mu"
    assert root.by_name.keys() == sorted(root.by_name.keys(), key=lambda k: k.encode())


@needs_flatc
def test_flatview_output_decodes_identically_in_flatc(kitchen_schema, fleet):
    ours = kitchen_schema.encode(fleet)
    theirs = flatc_from_json(SCHEMA_PATH, fleet)
    close_enough(flatc_to_json(SCHEMA_PATH, ours), flatc_to_json(SCHEMA_PATH, theirs))


@needs_flatc
def test_asdict_matches_flatc_json(kitchen_schema, fleet):
    data = flatc_from_json(SCHEMA_PATH, fleet)
    expected = flatc_to_json(SCHEMA_PATH, data)
    got = kitchen_schema.read(data)._asdict(defaults=False, enums="name")
    # flatc prints nested flatbuffers and bit flags its own way; compare those separately
    for ship_got, ship_exp in zip(got["ships"], expected["ships"]):
        if "blueprint" in ship_got:
            assert ship_got.pop("blueprint") == {"typeID": 999, "runs": 7}
            ship_exp.pop("blueprint")
    close_enough(got, expected)


def test_size_prefixed(kitchen_schema, fleet):
    data = kitchen_schema.encode(fleet, size_prefixed=True)
    assert struct.unpack_from("<I", data)[0] == len(data) - 4
    assert kitchen_schema.read(data).name == "Home Defence"          # auto-detected
    assert kitchen_schema.read(data, size_prefixed=True).name == "Home Defence"
    kitchen_schema.verify(data)


def test_wrong_identifier_rejected(kitchen_schema, fleet):
    data = bytearray(kitchen_schema.encode(fleet))
    data[4:8] = b"NOPE"
    with pytest.raises(FlatBufferError, match="file identifier"):
        kitchen_schema.read(bytes(data))
    assert kitchen_schema.read(bytes(data), check_identifier=False).name == "Home Defence"


def test_round_trip_through_views(kitchen_schema, fleet):
    first = kitchen_schema.read(kitchen_schema.encode(fleet))
    second = kitchen_schema.read(kitchen_schema.encode(first))
    assert first._asdict() == second._asdict()


def test_modify_a_copy(kitchen_schema, fleet):
    root = kitchen_schema.read(kitchen_schema.encode(fleet))
    d = root._asdict(defaults=False)
    d["ships"].append({"typeID": 1, "name": "Ibis"})
    new = kitchen_schema.read(kitchen_schema.encode(d))
    assert [s.typeID for s in new.ships] == [0, 1, 587, 24698]   # re-sorted by key


def test_unsorted_vector_detected(kitchen_schema, fleet):
    data = kitchen_schema.encode(fleet, sort_keys=False)
    with pytest.raises(VerificationError, match="not sorted by key"):
        kitchen_schema.verify(data)
    assert not kitchen_schema.read(data).ships.is_sorted()
    kitchen_schema.verify(data, check_sorted=False)


def test_force_defaults_and_dedup(kitchen_schema, fleet):
    small = kitchen_schema.encode(fleet)
    big = kitchen_schema.encode(fleet, force_defaults=True, share_strings=False)
    assert len(big) > len(small)
    assert kitchen_schema.read(big)._asdict() == kitchen_schema.read(small)._asdict()


def test_nested_flatbuffer_from_dict(kitchen_schema):
    data = kitchen_schema.encode({"ships": [{"typeID": 1, "name": "x", "blueprint": {"typeID": 5}}]})
    bp = kitchen_schema.read(data).ships[0].blueprint
    assert bp.typeID == 5 and bp.runs == 1


def test_union_type_inferred_from_view(kitchen_schema):
    charge = kitchen_schema.read(kitchen_schema.encode({"typeID": 42}, "Charge"), "Charge")
    data = kitchen_schema.encode({"ships": [{"typeID": 1, "name": "x", "primary": charge}]})
    ship = kitchen_schema.read(data).ships[0]
    assert ship.primary_type.name == "Charge" and ship.primary.typeID == 42


# ---------------------------------------------------------------------------
# encoder errors

@pytest.mark.parametrize("mutate, path, message", [
    (lambda f: f["ships"][0].update(tiny=-129), "ships[0].tiny", "out of range"),
    (lambda f: f["ships"][0].update(big=-1), "ships[0].big", "out of range"),
    (lambda f: f["ships"][0].update(race="Jove"), "ships[0].race", "not a value of"),
    (lambda f: f["ships"][0].update(name=None), "ships[0].name", "required"),
    (lambda f: f["ships"][0].update(colour="red"), "ships[0]", "unknown field"),
    (lambda f: f["ships"][0]["bounds"].update(counts=[1, 2]), "ships[0].bounds.counts", "exactly 3"),
    (lambda f: f["ships"][0].update(primary_type="Nope"), "ships[0].primary_type", "not a value"),
    (lambda f: f["ships"][0].update(tags="frigate"), "ships[0].tags", "expected a sequence"),
    (lambda f: f["ships"][0]["modules"][1].update(flags="Published Shiny"), "ships[0].modules", "Shiny"),
    (lambda f: f["ships"][0].update(enabled="yes"), "ships[0].enabled", "expected bool"),
])
def test_encode_errors(kitchen_schema, fleet, mutate, path, message):
    bad = copy.deepcopy(fleet)
    mutate(bad)
    with pytest.raises(EncodeError, match=message) as exc:
        kitchen_schema.encode(bad)
    assert exc.value.path.startswith(path)


# ---------------------------------------------------------------------------
# verifier on corrupt data

def _corrupt(data: bytes, pos: int, value: bytes) -> bytes:
    b = bytearray(data)
    b[pos:pos + len(value)] = value
    return bytes(b)


def test_truncation_detected(kitchen_schema, fleet):
    data = kitchen_schema.encode(fleet)
    # (the last few bytes can be legitimate trailing padding, so cut deeper)
    for cut in (4, 7, len(data) // 2, len(data) - 24):
        with pytest.raises(VerificationError):
            kitchen_schema.verify(data[:cut], size_prefixed=False)


def test_random_corruption_never_crashes_verifier(kitchen_schema, fleet):
    import random
    data = kitchen_schema.encode(fleet)
    rng = random.Random(1234)
    caught = 0
    for _ in range(2000):
        pos = rng.randrange(8, len(data))
        bad = _corrupt(data, pos, bytes([rng.randrange(256)]))
        try:
            kitchen_schema.verify(bad, size_prefixed=False)
        except VerificationError:
            caught += 1
    assert caught > 100


def test_bad_utf8_detected(kitchen_schema):
    data = kitchen_schema.encode({"name": "abc"})
    i = data.index(b"abc")
    with pytest.raises(VerificationError, match="UTF-8"):
        kitchen_schema.verify(_corrupt(data, i, b"\xff"))


def test_missing_terminator_detected(kitchen_schema):
    data = kitchen_schema.encode({"name": "abc"})
    i = data.index(b"abc")
    with pytest.raises(VerificationError, match="zero-terminated"):
        kitchen_schema.verify(_corrupt(data, i + 3, b"x"))


def test_strict_enums(kitchen_schema):
    data = kitchen_schema.encode({"ships": [{"typeID": 1, "name": "x", "race": 2}]})
    kitchen_schema.verify(data, strict_enums=True)
    root = kitchen_schema.read(data)
    pos = root.ships[0]._field_offset("race")
    bad = _corrupt(data, pos, b"\x63")
    kitchen_schema.verify(bad)
    with pytest.raises(VerificationError, match="not a value of kitchen.common.Race"):
        kitchen_schema.verify(bad, strict_enums=True)
    assert kitchen_schema.read(bad).ships[0].race == 0x63   # unknown values come back as int


def test_depth_limit(kitchen_schema, fleet):
    data = kitchen_schema.encode(fleet)
    with pytest.raises(VerificationError, match="nesting deeper"):
        kitchen_schema.verify(data, max_depth=1)


def test_strict_table_size(kitchen_schema):
    # Shrink the recorded table size: flatc accepts this (fields are only
    # bounded by the buffer); strict_table_size flags it.
    data = kitchen_schema.encode({"name": "abc", "ships": [{"typeID": 1, "name": "x", "hp": 5}]})
    ship = kitchen_schema.read(data).ships[0]
    vt = ship._vt
    tlen = struct.unpack_from("<H", data, vt + 2)[0]
    bad = _corrupt(data, vt + 2, struct.pack("<H", 4))
    assert tlen > 4
    kitchen_schema.verify(bad)
    with pytest.raises(VerificationError, match="past the end of its table"):
        kitchen_schema.verify(bad, strict_table_size=True)


@pytest.mark.parametrize("opts", [{}, {"defaults": False}, {"enums": "name"}, {"enums": "value", "deprecated": True}])
def test_asdict_same_for_bytes_and_memoryview(kitchen_schema, fleet, opts):
    plain = kitchen_schema.encode(fleet)                       # bytes: fast "raw" decode path
    prefixed = kitchen_schema.encode(fleet, size_prefixed=True)  # sliced memoryview path
    a = kitchen_schema.read(plain)._asdict(**opts)
    b = kitchen_schema.read(prefixed)._asdict(**opts)
    assert a == b
    assert a["ships"][1]["blueprint"]["runs"] == 7               # nested buffer converted
    assert flatview.to_python(kitchen_schema.read(plain).ships, **opts) == a["ships"]


def test_builder_does_not_share_vtables_across_sizes():
    # Regression for google/flatbuffers#9413 (Go builder): tables whose field
    # offsets match but whose sizes differ must not share a vtable.
    s = flatview.parse_fbs("""
        table Small { a: int; b: int; c: int; }
        table Big { a: int; b: int; c: double; }
        table R { small: [Small]; big: [Big]; }
        root_type R;
    """)
    data = s.encode({"small": [{"a": 1, "b": 2, "c": 3}] * 3, "big": [{"a": 1, "b": 2, "c": 3.5}] * 3})
    s.verify(data, strict_table_size=True)
    r = s.read(data)
    assert r.small[0]._vt != r.big[0]._vt
    assert r.small[0]._vt == r.small[1]._vt          # same-size tables still share
    assert r.big[2].c == 3.5


def test_sde_shared_vtable_flagged_only_when_strict():
    fb = flatview.open(SDE.parent / "sde-full" / "blueprints.fb") if (SDE.parent / "sde-full" / "blueprints.fb").exists() else None
    if fb is None:
        pytest.skip("run scripts/fetch_sde_examples.py first")
    fb.verify()
    with pytest.raises(VerificationError, match=r"invention\.products\[0\]\.probability.*past the end of its table"):
        fb.verify(strict_table_size=True)
