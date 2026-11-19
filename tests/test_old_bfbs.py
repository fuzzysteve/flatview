""".bfbs files written by historic flatc releases (1.12.0, 2.0.0, 22.9.24).

reflection.fbs has gained fields over the years, so older .bfbs files simply
lack some (`optional` before 2.0, struct `padding` before 2.0.x). flatview
must load them, see the same schema, and decode data exactly as it does with
the .fbs. Fixtures come from scripts/make_old_bfbs.py.
"""

import pytest

import flatview

from conftest import DATA, SDE, SDE_SCHEMAS, normalise
from test_evolution import NEW_VALUE, OLD_VALUE
from test_union_members import CASES as UNION_CASES

OLD = DATA / "old-bfbs"
FIXTURES = sorted(OLD.glob("*/*.bfbs"))

SOURCES = {
    "kitchen": DATA / "kitchen" / "kitchen.fbs",
    "v1": DATA / "evolution" / "v1.fbs",
    "v2": DATA / "evolution" / "v2.fbs",
    "members": DATA / "unions" / "members.fbs",
    "reflection": DATA.parent.parent / "src" / "flatview" / "reflection.fbs",
    "categories": SDE / "categories.fbs",
    "masteries": SDE / "masteries.fbs",
    "dogmaEffects": SDE / "dogmaEffects.fbs",
}

KITCHEN_VALUE = {
    "name": "fleet", "by_name": [{"name": "b"}, {"name": "a", "slot": "Rig", "cpu": 3.0}],
    "ships": [{"typeID": 587, "name": "Rifter", "race": "Minmatar", "mass": 1.5e6, "hp": 9,
               "position": [1.0, 2.0, 3.0],
               "bounds": {"min": [0, 0, 0], "tag": 1, "counts": [1, 2, 3], "pairs": [[1, 2], [3, 4]], "slot": "Low"},
               "modules": [{"name": "m", "flags": "Published Dynamic"}], "tags": ["t"], "slots": ["High"],
               "raw": [1, 2], "points": [[0, 0, 1]], "primary_type": "Charge", "primary": {"typeID": 3},
               "cargo_type": ["Module", "Ammo"], "cargo": [{"name": "c"}, {"typeID": 4}],
               "big": 1, "tiny": -1, "ratio": 0.25, "enabled": False, "maybe": True}],
}


def samples(name: str, schema: flatview.Schema) -> list[bytes]:
    """Buffers to decode with both schemas."""
    if name == "kitchen":
        return [schema.encode(KITCHEN_VALUE)]
    if name in ("v1", "v2"):
        return [schema.encode(OLD_VALUE if name == "v1" else NEW_VALUE)]
    if name == "members":
        return [schema.encode(v) for v in UNION_CASES.values()]
    if name == "reflection":
        return [p.read_bytes() for p in sorted(SDE_SCHEMAS.glob("*.bfbs"))]      # all 104 SDE schemas
    return [flatview.read_bytes(SDE / f"{name}.fb")]


def projection(schema: flatview.Schema) -> dict:
    """normalise(), minus what older reflection.fbs versions never stored."""
    n = normalise(schema, builtins=True)
    for obj in n["objects"].values():
        # field tuple: ... [11] optional, [12] padding, ...
        obj["fields"] = [f[:11] + f[13:] for f in obj["fields"]]
    return n


def test_fixtures_present():
    versions = {p.parent.name for p in FIXTURES}
    assert versions == {"1.12.0", "2.0.0", "22.9.24"} and len(FIXTURES) == 20


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_old_bfbs_matches_fbs(path):
    old = flatview.load_schema(path)
    new = flatview.load_schema(SOURCES[path.stem])
    assert projection(old) == projection(new)


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_old_bfbs_decodes_like_fbs(path):
    old = flatview.load_schema(path)
    new = flatview.load_schema(SOURCES[path.stem])
    for data in samples(path.stem, new):
        old.verify(data, strict_enums=True)
        assert old.read(data)._asdict(deprecated=True) == new.read(data)._asdict(deprecated=True)
        # and encoding with the old schema produces data the new one reads the same
        again = old.encode(old.read(data))
        assert new.read(again)._asdict(deprecated=True) == new.read(data)._asdict(deprecated=True)
