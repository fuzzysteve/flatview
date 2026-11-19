"""Reading real EVE SDE FlatBuffers (as published at sde.evetech.net).

Expected values were checked against the SDE SQLite conversion of the same
build (3720952).
"""

import gzip

import pytest

import flatview
from flatview import TableVector

from conftest import SDE, SDE_FULL, close_enough, flatc_to_json, needs_flatc, sde_tables


@pytest.fixture(scope="module")
def categories():
    return flatview.open(SDE / "categories.fb")


def test_files_are_gzipped_like_ccp_serves_them():
    assert (SDE / "categories.fb").read_bytes()[:2] == b"\x1f\x8b"


def test_sde_metadata():
    meta = flatview.open(SDE / "_sde.fb").rows[0]
    assert meta.key == "sde"
    assert meta.buildNumber == 3720952
    assert meta.releaseDate == "2026-11-19T11:07:08Z"


def test_categories(categories):
    rows = categories.rows
    assert isinstance(rows, TableVector)
    ship = rows.lookup(6)
    assert ship.name.en == "Ship"
    assert ship.published is True
    assert rows.lookup(25).name.en == "Asteroid"
    system = rows.lookup(0)
    assert system.name.en == "#System" and system.published is False
    assert rows.find(999_999) is None
    with pytest.raises(KeyError):
        rows.lookup(999_999)


def test_key_omitted_when_default_still_found(categories):
    # categoryID 0 equals the default, so the writer left it out of the buffer
    row = categories.rows.lookup(0)
    assert not row._has("categoryID")
    assert row.categoryID == 0
    assert row._asdict(defaults=False)["categoryID"] == 0


def test_optional_scalars(categories):
    assert categories.rows.lookup(4).iconID == 22
    ship = categories.rows.lookup(6)
    assert not ship._has("iconID") and ship.iconID is None   # `= null`: absent is None, not 0
    system = categories.rows.lookup(0)
    assert not system._has("iconID") and system.iconID is None   # `= null`: absent is None, not 0


def test_localized_strings(categories):
    name = categories.rows.lookup(6).name
    assert name.de and name.en == "Ship" and name.ja
    assert name["en"] == "Ship"
    assert set(name._asdict()) == {"de", "en", "es", "fr", "ja", "ko", "ru", "zh"}


def test_compressible_types():
    rows = flatview.open(SDE / "compressibleTypes.fb").rows
    assert len(rows) == 220
    assert rows.lookup(18).compressedTypeID == 62528
    assert rows.lookup(19).compressedTypeID == 62572
    assert rows.is_sorted()


def test_masteries_nested_keyed_vectors():
    m = flatview.open(SDE / "masteries.fb").rows.lookup(582)
    level4 = m.levels.lookup(4)
    assert sorted(level4.certificateIDs) == sorted([96, 139, 85, 118, 87, 94])
    assert level4.certificateIDs.tolist() == [96, 139, 85, 118, 87, 94]   # stored order preserved


def test_planet_schematics():
    rows = flatview.open(SDE / "planetSchematics.fb").rows
    assert len(rows) == 68
    s = rows.lookup(65)
    assert s.name.en == "Superconductors"
    assert s.cycleTime == 3600


def test_dogma_effects_enums():
    fb = flatview.open(SDE / "dogmaEffects.fb")
    rows = fb.rows
    assert len(rows) == 3499
    assert rows.lookup(4).name == "shieldBoosting"
    assert rows.lookup(4).guid == "effects.ShieldBoosting"
    assert rows.lookup(13).name == "medPower"
    EffectCategory = fb.schema["EffectCategory"].python
    cat = rows.lookup(4).effectCategoryID
    assert cat == 1 and cat is EffectCategory.activation
    # enums inside modifierInfo carry the SDE's custom attribute
    mi_obj = fb.schema["DogmaEffectsModifierInfo"]
    assert "sde_enum_name" in mi_obj.field("func").attributes
    with_mods = next(r for r in rows if r.modifierInfo)
    func = with_mods.modifierInfo[0].func
    assert isinstance(func, fb.schema["Func"].python)
    assert with_mods.modifierInfo[0]._asdict(enums="name")["func"] == func.name


@pytest.mark.parametrize("table", sde_tables(SDE))
def test_sample_rows_sorted_and_verified(table):
    fb = flatview.open(SDE / f"{table}.fb")
    fb.verify(strict_enums=True)
    assert fb.rows.is_sorted()


def test_bytes_mmap_and_gzip_agree(tmp_path):
    raw = gzip.decompress((SDE / "masteries.fb").read_bytes())
    plain = tmp_path / "masteries.fb"
    plain.write_bytes(raw)
    for ext in ("fbs", "bfbs"):
        (tmp_path / f"masteries.{ext}").write_bytes((SDE / f"masteries.{ext}").read_bytes())
    a = flatview.open(SDE / "masteries.fb")                 # gzip -> memory
    with flatview.open(plain) as b:                          # mmap
        assert b._mmap is not None
        c = a.schema.read(raw)                               # plain bytes
        assert a.root._asdict() == b.root._asdict() == c._asdict()


def test_schema_discovery_prefers_bfbs(tmp_path):
    (tmp_path / "x.fb").write_bytes((SDE / "categories.fb").read_bytes())
    with pytest.raises(FileNotFoundError, match="no schema found"):
        flatview.open(tmp_path / "x.fb")
    (tmp_path / "x.fbs").write_bytes((SDE / "categories.fbs").read_bytes())
    assert flatview.open(tmp_path / "x.fb").schema.source.endswith("x.fbs")
    (tmp_path / "x.bfbs").write_bytes((SDE / "categories.bfbs").read_bytes())
    assert flatview.open(tmp_path / "x.fb").schema.source.endswith("x.bfbs")


def test_explicit_schema_object():
    schema = flatview.load_schema(SDE / "categories.bfbs")
    fb = flatview.open(SDE / "categories.fb", schema=schema)
    assert fb.schema is schema


def test_index_by_and_group_by(categories):
    by_name = categories.rows.index_by(lambda r: r.name.en)
    assert by_name["Ship"].categoryID == 6
    groups = categories.rows.group_by("published")
    assert {True, False} <= set(groups)
    assert sum(len(v) for v in groups.values()) == len(categories.rows)


def test_repr_is_readable(categories):
    r = repr(categories.rows.lookup(6))
    assert r.startswith("<Categories categoryID=6") and "en='Ship'" in r


# ---------------------------------------------------------------------------
# Byte-for-byte agreement with flatc's own decoder

def _json_against_flatc(directory, table):
    fb = flatview.open(directory / f"{table}.fb")
    raw = bytes(fb.data)
    expected = flatc_to_json(directory / f"{table}.fbs", raw)
    close_enough(fb.root._asdict(defaults=False, enums="name"), expected)


@needs_flatc
@pytest.mark.parametrize("table", sde_tables(SDE))
def test_samples_match_flatc(table):
    _json_against_flatc(SDE, table)


@needs_flatc
@pytest.mark.sde_full
@pytest.mark.skipif(not sde_tables(SDE_FULL), reason="run scripts/fetch_sde_examples.py first")
@pytest.mark.parametrize("table", sde_tables(SDE_FULL))
def test_full_sde_matches_flatc(table):
    _json_against_flatc(SDE_FULL, table)


@pytest.mark.sde_full
@pytest.mark.skipif(not sde_tables(SDE_FULL), reason="run scripts/fetch_sde_examples.py first")
@pytest.mark.parametrize("table", sde_tables(SDE_FULL))
def test_full_sde_verifies_and_round_trips(table):
    fb = flatview.open(SDE_FULL / f"{table}.fb")
    fb.verify(strict_enums=True)
    again = fb.schema.read(fb.schema.encode(fb.root))
    assert again._asdict() == fb.root._asdict()
