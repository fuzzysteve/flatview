"""Schema evolution: old data read with a newer schema, and new data with an
older one (tests/data/evolution/v1.fbs -> v2.fbs: fields appended, a field
deprecated, an enum value and a union member added).

Both read paths are checked (view properties and the compiled `_asdict`
converter), plus the verifier; with flatc available, flatview's decoding is
compared with flatc's JSON for the same buffers.
"""

import pytest

import flatview

from conftest import DATA, close_enough, flatc_from_json, flatc_to_json, needs_flatc

EVO = DATA / "evolution"
V1 = flatview.load_schema(EVO / "v1.fbs")
V2 = flatview.load_schema(EVO / "v2.fbs")

OLD_VALUE = {"ships": [
    {"name": "Rifter", "hp": 350, "color": "Red", "item_type": "Weapon",
     "item": {"name": "Autocannon", "damage": 22}, "tags": ["frigate", "t1"], "legacy": 7},
    {"name": "Merlin"},
]}

NEW_VALUE = {"motto": "fly safe", "ships": [
    {"name": "Rifter", "hp": 350, "color": "Blue", "item_type": "Weapon",
     "item": {"name": "Autocannon", "damage": 22, "range": 9.5}, "tags": ["frigate"],
     "speed": 410.0, "armour": 0, "crew": ["pilot"], "flagship": {"name": "Hel", "hp": 1}},
    {"name": "Merlin", "item_type": "Shield", "item": {"strength": 40}},
]}


def both_paths(view):
    """The compiled converter and the view properties must agree."""
    fast = view._asdict(defaults=True, enums="value", deprecated=True)
    slow = {f.name: flatview.to_python(view[f.name], enums="value", deprecated=True)
            for f in view._object.fields}
    assert fast == slow
    return fast


# --- old data, new schema ------------------------------------------------------

def test_old_data_new_schema():
    data = V1.encode(OLD_VALUE)
    V2.verify(data, strict_enums=True)
    fleet = V2.read(data)
    assert fleet.motto is None                               # new field, absent
    rifter = fleet.ships.lookup("Rifter")
    assert (rifter.hp, rifter.color.name, rifter.item_type.name) == (350, "Red", "Weapon")
    assert rifter.item.name == "Autocannon" and rifter.item.damage == 22
    assert rifter.item.range == 1.5                          # new field on the member: its default
    assert rifter.speed == 2.5 and rifter.armour is None     # new defaulted / optional scalars
    assert rifter.crew is None and rifter.flagship is None
    assert rifter.legacy == 7                                # deprecated, still readable...
    assert "legacy" not in rifter._asdict()                  # ...but left out by default
    assert rifter._asdict(deprecated=True)["legacy"] == 7
    merlin = fleet.ships.lookup("Merlin")
    assert merlin.hp == 100 and merlin.color.name == "Green" and merlin.item is None
    for ship in fleet.ships:
        both_paths(ship)
    both_paths(fleet)


# --- new data, old schema ------------------------------------------------------

def test_new_data_old_schema():
    data = V2.encode(NEW_VALUE)
    V1.verify(data)                                          # unknown fields are simply ignored
    fleet = V1.read(data)
    assert fleet._fields == ("ships",)                       # motto isn't known to v1
    rifter = fleet.ships.lookup("Rifter")
    assert rifter.hp == 350 and rifter.tags.tolist() == ["frigate"]
    assert rifter.color == 2 and type(rifter.color) is int  # Blue: unknown to v1, raw value
    assert rifter.item.name == "Autocannon" and rifter.item.damage == 22
    merlin = fleet.ships.lookup("Merlin")
    assert int(merlin.item_type) == 2 and merlin.item is None   # Shield: unknown member
    d = both_paths(merlin)
    assert d["item"] is None and d["item_type"] == 2
    assert both_paths(rifter)["color"] == 2
    with pytest.raises(flatview.VerificationError, match="not a"):
        V1.verify(data, strict_enums=True)                   # strict mode flags the unknowns


def test_new_data_round_trips_through_old_schema_view():
    # Re-encoding a v1 view keeps what v1 knows and drops what it doesn't.
    v1_view = V1.read(V2.encode(NEW_VALUE))
    again = V2.read(V1.encode(v1_view))
    assert again.ships.lookup("Rifter").speed == 2.5          # dropped -> default
    assert again.ships.lookup("Rifter").hp == 350


def test_deprecated_field_not_written_by_new_schema():
    data = V2.encode({"ships": [{"name": "x"}]})
    assert V1.read(data).ships[0].legacy == 0                 # absent -> v1's default


# --- cross-check with flatc ----------------------------------------------------

@needs_flatc
@pytest.mark.parametrize("writer, reader", [("v1", "v2"), ("v2", "v1")])
def test_evolution_matches_flatc(writer, reader):
    w_schema, r_path = EVO / f"{writer}.fbs", EVO / f"{reader}.fbs"
    value = OLD_VALUE if writer == "v1" else {
        # flatc's JSON printer can't print an unknown union member, so leave
        # Shield out of the cross-check (covered above without flatc)
        **NEW_VALUE, "ships": [NEW_VALUE["ships"][0], {"name": "Merlin"}]}
    reader_schema = V1 if reader == "v1" else V2
    for data in (flatc_from_json(w_schema, value), (V1 if writer == "v1" else V2).encode(value)):
        expected = flatc_to_json(r_path, data)
        got = reader_schema.read(data)._asdict(defaults=False, enums="name", deprecated=True)
        close_enough(got, expected)
