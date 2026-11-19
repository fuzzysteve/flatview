"""Regression tests for the security review: untrusted schemas, buffers and files."""

import builtins
import struct
import importlib.util
import json
import time
import zlib
from pathlib import Path

import pytest

import flatview
from flatview import DecompressionLimitError, SchemaError, VerificationError, parse_fbs
from flatview._builder import Builder

from conftest import KITCHEN, SDE, flatc_bfbs, needs_flatc

REFL = flatview.reflection_schema()


def bfbs_dict(data: bytes) -> dict:
    return REFL.read(data)._asdict(defaults=False, enums="value")


def categories_bfbs() -> dict:
    return bfbs_dict((SDE / "categories.bfbs").read_bytes())


# ---------------------------------------------------------------------------
# 1. code injection through names in a .bfbs

INJECT = "A(buf, pos):\n    pass\nimport builtins; builtins.FLATVIEW_PWNED = True\ndef c_x"


@pytest.fixture(autouse=True)
def _no_pwn():
    yield
    assert not getattr(builtins, "FLATVIEW_PWNED", False), "injected code executed"


@pytest.mark.parametrize("where", ["object", "field", "enum_value"])
def test_bfbs_with_code_in_names_is_rejected(where):
    d = categories_bfbs()
    if where == "object":
        d["objects"][0]["name"] = INJECT
    elif where == "field":
        d["objects"][0]["fields"][0]["name"] = INJECT
    else:
        d["enums"] = [{"name": "E", "values": [{"name": INJECT, "value": 0}],
                       "underlying_type": {"base_type": 7}}]
    with pytest.raises(SchemaError, match="bad .* name"):
        flatview.load_bfbs(REFL.encode(d))


def test_converters_never_embed_schema_names():
    # Defence in depth: even if a hostile name reaches a Schema object some
    # other way, the generated converter source must not contain it.
    s = parse_fbs("table A { v: int; } root_type A;")
    s.objects["A"].name = INJECT        # bypasses every loader-side check
    data = s.encode({"v": 5})
    assert s.read(data)._asdict() == {"v": 5}
    s.objects["A"].fields[0].name = "x'] = 1; import builtins; builtins.FLATVIEW_PWNED = True; d['"
    s.objects["A"]._by_name = None
    s.objects["A"].view_class = None
    s.__dict__.pop("_converters", None)
    assert list(s.read(data)._asdict()) == [s.objects["A"].fields[0].name]


# ---------------------------------------------------------------------------
# 2. reference amplification

def amplification_buffer(width: int = 2000) -> tuple[flatview.Schema, bytes]:
    s = parse_fbs("table C { v: int; } table B { c: [C]; } table A { b: [B]; } root_type A;")
    b = Builder()
    c = b.table([(0, "i", 7, 4)])
    bt = b.table([(0, "offset", b.create_offset_vector([c] * width), 4)])
    root = b.table([(0, "offset", b.create_offset_vector([bt] * width), 4)])
    return s, b.finish(root)


def test_reference_amplification_rejected_quickly():
    s, data = amplification_buffer()
    assert len(data) < 20_000                     # ~16 KB referencing 4M tables
    t = time.perf_counter()
    with pytest.raises(VerificationError, match="amplification"):
        s.verify(data)
    assert time.perf_counter() - t < 1.0


def test_shared_tables_allowed_when_trusted():
    s, data = amplification_buffer(width=50)
    with pytest.raises(VerificationError, match="amplification"):
        s.verify(data)
    s.verify(data, max_refs=10_000)


def test_unshared_buffers_fit_default_budget():
    for table in ("dogmaEffects", "certificates", "masteries"):
        flatview.open(SDE / f"{table}.fb").verify()


# ---------------------------------------------------------------------------
# 3. decompression bombs

def gzip_of_zeros(n: int, members: int = 1) -> bytes:
    out = b""
    for _ in range(members):
        z = zlib.compressobj(9, zlib.DEFLATED, 31)
        out += z.compress(b"\0" * n) + z.flush()
    return out


def test_gzip_bomb_stopped(tmp_path):
    p = tmp_path / "bomb.fb"
    p.write_bytes(gzip_of_zeros(50 << 20))        # ~50 KB -> 50 MB
    assert p.stat().st_size < 100_000
    with pytest.raises(DecompressionLimitError):
        flatview.read_bytes(p, max_size=1 << 20)
    with pytest.raises(DecompressionLimitError):
        flatview.load_schema(p, max_size=1 << 20)
    (tmp_path / "bomb.fbs").write_text("table T { a: int; } root_type T;")
    with pytest.raises(DecompressionLimitError):
        flatview.open(p, max_size=1 << 20)


def test_gzip_multi_member_and_limit_boundary(tmp_path):
    p = tmp_path / "x.gz"
    p.write_bytes(gzip_of_zeros(1000, members=3))
    assert flatview.read_bytes(p) == b"\0" * 3000
    assert flatview.read_bytes(p, max_size=3000) == b"\0" * 3000
    with pytest.raises(DecompressionLimitError):
        flatview.read_bytes(p, max_size=2999)


def test_gzip_truncated(tmp_path):
    p = tmp_path / "t.gz"
    p.write_bytes(gzip_of_zeros(10_000)[:-12])
    with pytest.raises(OSError, match="truncated"):
        flatview.read_bytes(p)


def test_zstd_bomb_stopped(tmp_path):
    zstandard = pytest.importorskip("zstandard")
    p = tmp_path / "bomb.fb"
    p.write_bytes(zstandard.ZstdCompressor().compress(b"\0" * (20 << 20)))
    with pytest.raises(DecompressionLimitError):
        flatview.read_bytes(p, max_size=1 << 20)
    small = tmp_path / "ok.fb"
    small.write_bytes(zstandard.ZstdCompressor().compress(b"abc"))
    assert flatview.read_bytes(small) == b"abc"


def test_real_gzip_sde_still_reads():
    assert len(flatview.read_bytes(SDE / "categories.fb")) == 10284


# ---------------------------------------------------------------------------
# 4. fetch script trusts nothing from the remote index

def load_fetch_script():
    path = Path(__file__).parent.parent / "scripts" / "fetch_sde_examples.py"
    spec = importlib.util.spec_from_file_location("fetch_sde_examples", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("name, url", [
    ("../../evil", "https://sde.evetech.net/x.fb"),
    ("good", "https://attacker.example/x.fb"),
    ("good", "http://sde.evetech.net/x.fb"),
])
def test_fetch_script_refuses_unsafe_index(tmp_path, monkeypatch, name, url, capsys):
    mod = load_fetch_script()
    index = {"release": "r", "buildNumber": 1, "tables": {name: {"flatbuffers": {
        "binaryUrl": url, "schemaUrl": url, "binarySchemaUrl": url}}}}
    fetched = []

    def fake_fetch(u):
        fetched.append(u)
        if u == mod.INDEX_URL:
            return json.dumps(index).encode()
        raise AssertionError(f"must not download {u}")
    monkeypatch.setattr(mod, "fetch", fake_fetch)
    monkeypatch.setattr("sys.argv", ["fetch", "--all", "--out", str(tmp_path / "out")])
    assert mod.main() == 2
    assert fetched == [mod.INDEX_URL]
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())
    assert "refusing" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 5. hostile layout values in a .bfbs

def test_corrupt_bfbs_buffer_rejected():
    data = bytearray((SDE / "categories.bfbs").read_bytes())
    data[40:48] = b"\xff" * 8
    with pytest.raises(SchemaError, match="invalid .bfbs"):
        flatview.load_bfbs(bytes(data))


@needs_flatc
@pytest.mark.parametrize("mutate, message", [
    (lambda o: o.update(bytesize=0), "invalid size"),
    (lambda o: o.update(minalign=3), "invalid alignment"),
    (lambda o: o["fields"][0].update(offset=1000), "overruns struct"),
])
def test_hostile_struct_layout_rejected(mutate, message):
    d = bfbs_dict(flatc_bfbs(KITCHEN / "kitchen.fbs"))
    vec3 = next(o for o in d["objects"] if o["name"] == "kitchen.common.Vec3")
    mutate(vec3)
    with pytest.raises(SchemaError, match=message):
        flatview.load_bfbs(REFL.encode(d))


def test_hostile_table_layout_rejected():
    d = categories_bfbs()
    table = next(o for o in d["objects"] if o["name"].endswith(".Categories"))
    table["fields"][1]["offset"] = 4          # slot no longer matches id 1
    with pytest.raises(SchemaError, match="does not match id"):
        flatview.load_bfbs(REFL.encode(d))
    d = categories_bfbs()
    table = next(o for o in d["objects"] if o["name"].endswith(".Categories"))
    table["fields"][0]["type"]["base_type"] = 99
    with pytest.raises(SchemaError):
        flatview.load_bfbs(REFL.encode(d))


def test_valid_bfbs_still_load():
    for p in (SDE.parent / "sde-schemas").glob("*.bfbs"):
        flatview.load_schema(p)


# ---------------------------------------------------------------------------
# 6. dunder field names

def test_dunder_field_names_do_not_hijack_protocols():
    s = parse_fbs("table T { __len__: int; __iter__: int; __class__: int; ok: int; } root_type T;")
    t = s.read(s.encode({"__len__": 3, "__iter__": 5, "__class__": 1, "ok": 2}))
    assert type(t).__dict__.get("__len__") is None
    assert t["__len__"] == 3 and t["__iter__"] == 5 and t["__class__"] == 1
    assert t.ok == 2
    assert isinstance(t, flatview.TableView)
    assert t._asdict() == {"__len__": 3, "__iter__": 5, "__class__": 1, "ok": 2}
    assert repr(t).startswith("<T ")


# ---------------------------------------------------------------------------
# 7. schema string literals

def test_string_escapes_decoded():
    s = parse_fbs('attribute "a"; table T { x: int (a: "\\u00e9\\u4e2d \\ud83d\\ude00 \\x41 \\"q\\" \\\\ \\n"); }')
    assert s["T"].field("x").attributes["a"] == 'é中 😀 A "q" \\ \n'


@pytest.mark.parametrize("literal", ['"\\q"', '"\\u12"', '"\\ud83d"', '"\\xZZ"'])
def test_bad_string_escapes_are_schema_errors(literal):
    with pytest.raises(SchemaError, match="bad string literal"):
        parse_fbs(f'attribute "a"; table T {{ x: int (a: {literal}); }}')


def test_deeply_nested_type_brackets():
    with pytest.raises(SchemaError, match="nested vectors"):
        parse_fbs("table T { x: " + "[" * 5000 + "int" + "]" * 5000 + "; }")


# ===========================================================================
# Second pass

# A. size prefixes are never guessed (a valid buffer's root offset can equal
#    len - 4, e.g. a root table with no fields set)

def test_minimal_buffer_not_mistaken_for_size_prefixed():
    s = parse_fbs("table T { v: int; } root_type T;")
    data = s.encode({})
    assert struct.unpack_from("<I", data)[0] == len(data) - 4   # the ambiguous case
    s.verify(data)
    assert s.read(data).v == 0
    assert s.read(data)._asdict() == {"v": 0}


def test_nested_buffer_read_like_verifier():
    s = parse_fbs('table In { v: int; } table Out { n: [ubyte] (nested_flatbuffer: "In"); } root_type Out;')
    inner = struct.pack("<IHHi", 8, 4, 4, 4)      # valid; root offset 8 == len - 4
    data = s.encode({"n": inner})
    s.verify(data)
    assert s.read(data).n.v == 0                  # view and converter now agree
    assert s.read(data)._asdict() == {"n": {"v": 0}}


# B. zero-size structs

def test_empty_struct_rejected_like_flatc():
    with pytest.raises(SchemaError, match="size 0 structs not allowed"):
        parse_fbs("struct E {} table T { e: [E]; } root_type T;")


# C. enum value names Python's enum module reserves

@pytest.mark.parametrize("name", ["_missing_", "mro", "__eq__", "_generate_next_value_"])
def test_reserved_enum_names_fall_back_to_ints(name):
    s = parse_fbs(f"enum C : byte {{ A, {name} }} table T {{ c: C; cs: [C]; }} root_type T;")
    t = s.read(s.encode({"c": 1, "cs": [0, 1]}))
    assert t.c == 1 and type(t.c) is int
    assert t.cs.tolist() == [0, 1]
    assert t._asdict() == {"c": 1, "cs": [0, 1]}
    assert t._asdict(enums="name") == {"c": name, "cs": ["A", name]}   # names still available
    with pytest.raises(SchemaError, match="cannot be a Python enum"):
        s["C"].python


def test_ordinary_enum_names_still_members():
    s = parse_fbs("enum C : byte { A, name, value } table T { c: C; } root_type T;")
    assert s.read(s.encode({"c": 2})).c is s["C"].python.value


# D. deep schemas don't hit the recursion limit

def test_deep_struct_nesting_any_declaration_order():
    n = 3000
    decls = [f"struct S{i} {{ s: S{i - 1}; }}" for i in range(1, n)] + ["struct S0 { a: int; }"]
    for order in (decls, decls[::-1]):
        s = parse_fbs("\n".join(order))
        assert s["S2999"].bytesize == 4


def test_deep_table_type_chain_converts():
    n = 3000
    src = "".join(f"table T{i} {{ t: T{i + 1}; }}\n" for i in range(n)) + f"table T{n} {{ v: int; }}\nroot_type T0;"
    s = parse_fbs(src)
    data = s.encode({"t": {"t": {}}})
    assert s.read(data)._asdict(defaults=False) == {"t": {"t": {}}}


def test_struct_cycle_in_bfbs_rejected():
    from flatview._bfbs import validate
    s = parse_fbs("struct A { x: int; } struct B { a: A; } table T { b: B; } root_type T;")
    s["A"].fields[0].type = flatview.Type(flatview.BaseType.OBJ, ref=s["B"])   # A -> B -> A
    with pytest.raises(SchemaError, match="contains itself"):
        validate(s)


# G. encoding a lone surrogate

def test_lone_surrogate_is_encode_error():
    s = parse_fbs("table T { s: string; ss: [string]; } root_type T;")
    with pytest.raises(flatview.EncodeError, match="not valid Unicode") as e:
        s.encode({"ss": ["ok", "\ud800"]})
    assert e.value.path == "ss[1]"
