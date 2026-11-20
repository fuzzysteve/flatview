"""Regression tests for the security review: untrusted schemas, buffers and files."""

import builtins
import struct
import importlib.util
import json
import os
import sys
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
    s._clear_compiled()
    assert list(s.read(data)._asdict()) == [s.objects["A"].fields[0].name]


def test_encoders_never_embed_schema_names():
    # the compiled encoders follow the same rule as the converters
    s = parse_fbs("table A { v: int; w: string; } root_type A;")
    s.objects["A"].name = INJECT
    s.objects["A"].fields[0].name = "x'] = 1; import builtins; builtins.FLATVIEW_PWNED = True; d['"
    s.objects["A"].fields[1].name = "w\nimport builtins; builtins.FLATVIEW_PWNED = True"
    s.objects["A"]._by_name = None
    s.objects["A"].view_class = None
    names = [f.name for f in s.objects["A"].fields]
    data = s.encode({names[0]: 5, names[1]: "s"})
    assert s.read(data)._asdict() == {names[0]: 5, names[1]: "s"}


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
    decls = ["struct S0 { a: int; }"] + [f"struct S{i} {{ s: S{i - 1}; }}" for i in range(1, n)]
    assert parse_fbs("\n".join(decls))["S2999"].bytesize == 4
    # Reverse order is invalid (flatc: inner structs must come first) and must
    # fail cleanly, not by recursion.
    with pytest.raises(SchemaError, match="Incomplete type in struct"):
        parse_fbs("\n".join(decls[::-1]))


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


# ===========================================================================
# Third pass

# 1. schema-driven quadratic cost

def test_bfbs_with_gapped_field_ids_rejected():
    d = bfbs_dict((SDE / "compressibleTypes.bfbs").read_bytes())
    row = next(o for o in d["objects"] if o["name"].endswith(".CompressibleTypes"))
    row["fields"] = [dict(row["fields"][0], id=6000, offset=4 + 2 * 6000, key=False)]
    with pytest.raises(SchemaError, match="without gaps"):
        flatview.load_bfbs(REFL.encode(d))


def test_wide_table_converter_is_cheap():
    import tracemalloc
    n = 32765                     # the most fields a FlatBuffers table can have
    s = parse_fbs("table T { " + " ".join(f"f{i}: int;" for i in range(n)) + " } root_type T;")
    data = s.encode({"f0": 1, f"f{n - 1}": 2})
    tracemalloc.start()
    out = s.read(data)._asdict(defaults=False)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert out == {"f0": 1, f"f{n - 1}": 2}
    assert peak < 64 << 20        # was ~4 GB (quadratic), then ~400 MB (compiling a 32k-branch function)


@pytest.mark.parametrize("opts", [{}, {"defaults": False}, {"enums": "name"}, {"enums": "value", "deprecated": True}])
def test_generic_and_compiled_converters_agree(kitchen_schema, monkeypatch, opts):
    from test_kitchen import blueprint_bytes
    import flatview._todict as td
    data = kitchen_schema.encode({"name": "x", "ships": [
        {"typeID": 1, "name": "a", "race": "Amarr", "position": [1.0, 2.0, 3.0],
         "modules": [{"name": "m", "flags": "Published Dynamic"}], "tags": ["t"], "slots": ["Rig"],
         "primary_type": "Charge", "primary": {"typeID": 5}, "cargo_type": ["Module"],
         "cargo": [{"name": "c"}], "blueprint": blueprint_bytes(kitchen_schema), "maybe": True}]})
    compiled = kitchen_schema.read(data)._asdict(**opts)
    monkeypatch.setattr(td, "WIDE_TABLE", 0)
    kitchen_schema._clear_compiled()
    generic = kitchen_schema.read(data)._asdict(**opts)
    kitchen_schema._clear_compiled()
    assert generic == compiled


def test_wide_table_encoder_is_cheap():
    import tracemalloc
    n = 32765
    s = parse_fbs("table T { " + " ".join(f"f{i}: int;" for i in range(n)) + " } root_type T;")
    tracemalloc.start()
    data = s.encode({"f0": 1, f"f{n - 1}": 2})
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert s.read(data)._asdict(defaults=False) == {"f0": 1, f"f{n - 1}": 2}
    assert peak < 64 << 20        # no 32k-branch function compiled


def test_generated_code_refuses_non_integers():
    s = parse_fbs("table T { a: int; } root_type T;")
    s["T"].fields[0].id = "0] + __import__('os').getpid() + e[0"
    s["T"].view_class = None
    with pytest.raises(TypeError, match="non-integer"):
        s.read(s.encode({}))._asdict()


# 2. empty nested flatbuffers

def test_empty_nested_flatbuffer_rejected():
    s = parse_fbs('table In { v: int; } table Out { n: [ubyte] (nested_flatbuffer: "In"); } root_type Out;')
    with pytest.raises(flatview.EncodeError, match="must not be empty"):
        s.encode({"n": b""})
    b = Builder()
    vec = b.create_scalar_vector(flatview.BaseType.UBYTE, [])
    data = b.finish(b.table([(0, "offset", vec, 4)]))
    with pytest.raises(VerificationError, match="nested flatbuffer is empty"):
        s.verify(data)


# 3. bounded reads of special files

@pytest.mark.skipif(sys.platform == "win32", reason="no /dev/zero on Windows")
def test_special_files_are_read_with_a_cap():
    s = parse_fbs("table T { a: int; } root_type T;")
    t = time.perf_counter()
    with pytest.raises(DecompressionLimitError):
        flatview.open("/dev/zero", s, max_size=1 << 20)
    with pytest.raises(DecompressionLimitError):
        flatview.read_bytes("/dev/zero", max_size=1 << 20)
    assert time.perf_counter() - t < 2


def test_oversized_regular_file_rejected_before_mmap(tmp_path):
    p = tmp_path / "big.fb"
    p.write_bytes(b"\0" * 4096)
    s = parse_fbs("table T { a: int; } root_type T;")
    with pytest.raises(DecompressionLimitError):
        flatview.open(p, s, max_size=1024)


# 4. numeric literals

@pytest.mark.parametrize("src", [
    "table T { a: int (id: abc); }",
    "struct S (force_align: x) { a: int; }",
    "table T { a: int (id: " + "9" * 5000 + "); }",
    "struct S { a: [int:" + "9" * 5000 + "]; }",
])
def test_bad_numbers_are_schema_errors(src):
    with pytest.raises(SchemaError):
        parse_fbs(src)


# 5. no leaked handles on a failed open

def test_failed_open_releases_file(tmp_path):
    import gc
    import warnings
    s = parse_fbs('table T { a: int; } root_type T; file_identifier "ABCD";')
    p = tmp_path / "x.fb"
    p.write_bytes(s.encode({"a": 1}, file_identifier="ZZZZ"))
    with warnings.catch_warnings():
        warnings.simplefilter("error", ResourceWarning)
        for _ in range(3):
            with pytest.raises(flatview.FlatBufferError, match="identifier"):
                flatview.open(p, s)
        gc.collect()          # an unclosed file would raise ResourceWarning here


# ===========================================================================
# Fourth pass: reading what you shouldn't

SECRET = "SECRET_TOKEN_hunter2"


@pytest.fixture
def secret_tree(tmp_path):
    (tmp_path / "secret.txt").write_text(SECRET + "\n")
    schemas = tmp_path / "schemas"
    (schemas / "sub").mkdir(parents=True)
    (schemas / "common.fbs").write_text("table Common { v: int; }\n")
    (schemas / "sub" / "deep.fbs").write_text("table Deep { v: int; }\n")
    return tmp_path, schemas


def _main(schemas, inc):
    p = schemas / "main.fbs"
    p.write_text(f'include "{inc}";\ntable T {{ a: int; }}\nroot_type T;\n')
    return p


@pytest.mark.parametrize("inc", ["../secret.txt", "{abs}", "sub/../../secret.txt", "link.fbs"])
def test_include_cannot_escape_schema_directory(secret_tree, inc):
    tmp, schemas = secret_tree
    (schemas / "link.fbs").symlink_to(tmp / "secret.txt")
    # forward slashes: a raw Windows path would put "\\U..." escapes in the .fbs string
    p = _main(schemas, inc.format(abs=(tmp / "secret.txt").as_posix()))
    with pytest.raises(SchemaError, match="refusing to include") as e:
        flatview.load_schema(p)
    assert SECRET not in str(e.value)


def test_include_of_system_file_refused(secret_tree):
    _, schemas = secret_tree
    with pytest.raises(SchemaError, match="refusing to include"):
        flatview.load_schema(_main(schemas, "/etc/hostname"))


def test_legitimate_includes_still_work(secret_tree, tmp_path):
    _, schemas = secret_tree
    assert "Common" in flatview.load_schema(_main(schemas, "common.fbs")).objects
    assert "Deep" in flatview.load_schema(_main(schemas, "sub/deep.fbs")).objects
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "lib.fbs").write_text("table Lib { v: int; }\n")
    assert "Lib" in flatview.load_schema(_main(schemas, "lib.fbs"), include_paths=[other]).objects


def test_include_from_text_needs_include_paths(secret_tree):
    _, schemas = secret_tree
    with pytest.raises(SchemaError, match="pass include_paths"):
        parse_fbs('include "common.fbs"; table T { a: int; }')
    assert "Common" in parse_fbs('include "common.fbs"; table T { a: int; }', include_paths=[schemas]).objects


def test_includes_can_be_disabled(secret_tree):
    _, schemas = secret_tree
    with pytest.raises(SchemaError, match="includes are disabled"):
        flatview.load_schema(_main(schemas, "common.fbs"), allow_includes=False)


# --- an untrusted .bfbs must not be able to inject text into to_fbs() -------------

def _render_reparse(d):
    s = flatview.load_bfbs(REFL.encode(d))
    text = s.to_fbs()
    assert all(line.lstrip().startswith("///") for line in text.splitlines() if "include" in line)
    again = parse_fbs(text, allow_includes=False)      # would fail if an include got through
    return s, again


def test_doc_comment_newlines_cannot_inject():
    d = categories_bfbs()
    d["objects"][0]["documentation"] = [' x\ninclude "../secret.txt";\ntable Evil { a: int; }']
    s, again = _render_reparse(d)
    assert "Evil" not in {o.short_name for o in again.objects.values()}
    name = d["objects"][0]["name"]
    assert again.objects[name].documentation == [" x", 'include "../secret.txt";', "table Evil { a: int; }"]


@pytest.mark.parametrize("where, value, message", [
    ("attribute key", 'x;\ninclude "../secret.txt";\nattribute "y', "bad attribute name"),
    ("file_ident", 'ABCD";\ninclude "../secret.txt";\n//', "not 4 bytes"),
    ("file_ext", 'x";\ninclude "../secret.txt";\n//', "bad file extension"),
])
def test_bfbs_cannot_smuggle_declarations(where, value, message):
    d = categories_bfbs()
    if where == "attribute key":
        d["objects"][0]["attributes"] = [{"key": value, "value": "0"}]
    elif where == "file_ident":
        d["file_ident"] = value
    else:
        d["file_ext"] = value
    with pytest.raises(SchemaError, match=message):
        flatview.load_bfbs(REFL.encode(d))


def test_render_refuses_non_identifiers_from_any_source():
    s = parse_fbs('attribute "a"; table T { x: int (a: "v"); } root_type T;')
    s["T"].fields[0].name = 'x; } include "/etc/passwd"; table U { y'
    with pytest.raises(SchemaError, match="not an identifier"):
        s.to_fbs()
    s = parse_fbs('attribute "a"; table T { x: int (a: "v"); } root_type T;')
    s["T"].fields[0].attributes["a"] = 'v"); } include "/etc/passwd"; table U { y: int ("'
    again = parse_fbs(s.to_fbs(), allow_includes=False)          # value is quoted, not injected
    assert again["T"].field("x").attributes["a"] == s["T"].fields[0].attributes["a"]


def test_special_file_identifier_round_trips():
    s = parse_fbs('table T { a: int; } root_type T; file_identifier "A\\"\\\\B";')
    assert s.file_ident == 'A"\\B'
    assert parse_fbs(s.to_fbs()).file_ident == 'A"\\B'


# --- reads never leave the buffer they were given ---------------------------------

def test_reads_confined_to_the_given_slice(kitchen_schema):
    import random
    from test_kitchen import blueprint_bytes
    payload = kitchen_schema.encode({"name": "fleet", "ships": [
        {"typeID": 1, "name": "a", "tags": ["x", "y"], "modules": [{"name": "m"}],
         "primary_type": "Charge", "primary": {"typeID": 2}, "blueprint": blueprint_bytes(kitchen_schema)}]})
    marker = (SECRET * 50).encode()
    rng = random.Random(7)
    for _ in range(1500):
        bad = bytearray(payload)
        for _ in range(rng.randrange(1, 4)):
            bad[rng.randrange(len(bad))] = rng.randrange(256)
        whole = marker + bytes(bad) + marker
        view = memoryview(whole)[len(marker):len(marker) + len(bad)]
        try:
            out = repr(kitchen_schema.read(view, check_identifier=False)._asdict())
        except Exception:
            continue        # malformed input may fail, but must not leak
        assert "SECRET" not in out and "hunter2" not in out


# ---------------------------------------------------------------------------
# include: check-then-open race (TOCTOU)

import flatview._fbs as _fbs_mod


@pytest.fixture(params=["fd-path", "inode-fallback"])
def include_mode(request, monkeypatch):
    """Run each race test with the descriptor-path check and with the
    fallback used on platforms that cannot report an fd's path."""
    if request.param == "inode-fallback":
        monkeypatch.setattr(_fbs_mod, "_fd_path", lambda fd: None)
    elif _fbs_mod._fd_path(os.open(__file__, os.O_RDONLY)) is None:
        pytest.skip("this platform cannot report an fd's path")
    return request.param


def _swap_before_open(monkeypatch, target: Path, swap):
    """Make os.open run ``swap()`` just before it opens ``target``, i.e. in the
    window after include() has checked the path."""
    real_open = os.open
    fired = []

    def racing_open(path, flags, *a, **kw):
        if not fired and os.fspath(path) == os.path.realpath(target):
            fired.append(True)
            swap()
        return real_open(path, flags, *a, **kw)
    monkeypatch.setattr(os, "open", racing_open)
    return fired


def _swap_to_symlink(link: Path, to: Path):
    tmp = link.with_name(link.name + ".swap")
    os.symlink(to, tmp)
    os.replace(tmp, link)


def test_include_swapped_for_symlink_after_check(secret_tree, monkeypatch, include_mode):
    tmp, schemas = secret_tree
    victim = schemas / "common.fbs"
    fired = _swap_before_open(monkeypatch, victim, lambda: _swap_to_symlink(victim, tmp / "secret.txt"))
    with pytest.raises(SchemaError, match="refusing to include") as e:
        flatview.load_schema(_main(schemas, "common.fbs"))
    assert fired and SECRET not in str(e.value)


def test_include_directory_component_swapped(secret_tree, monkeypatch, include_mode):
    tmp, schemas = secret_tree
    outside = tmp / "outside"
    outside.mkdir()
    (outside / "deep.fbs").write_text(f"table {SECRET} {{ v: int; }}\n")
    sub = schemas / "sub"
    def swap():
        os.rename(sub, schemas / "sub.orig")
        os.symlink(outside, sub, target_is_directory=True)
    fired = _swap_before_open(monkeypatch, sub / "deep.fbs", swap)
    with pytest.raises(SchemaError, match="refusing to include") as e:
        flatview.load_schema(_main(schemas, "sub/deep.fbs"))
    assert fired and SECRET not in str(e.value)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_include_swapped_for_fifo_does_not_hang(secret_tree, monkeypatch, include_mode):
    _, schemas = secret_tree
    victim = schemas / "common.fbs"
    def swap():
        victim.unlink()
        os.mkfifo(victim)
    fired = _swap_before_open(monkeypatch, victim, swap)
    t = time.perf_counter()
    with pytest.raises(SchemaError, match="not a regular file"):
        flatview.load_schema(_main(schemas, "common.fbs"))
    assert fired and time.perf_counter() - t < 5


def test_legitimate_symlink_inside_roots_still_works(secret_tree, include_mode):
    _, schemas = secret_tree
    os.symlink(schemas / "common.fbs", schemas / "alias.fbs")
    assert "Common" in flatview.load_schema(_main(schemas, "alias.fbs")).objects


def test_include_not_utf8(secret_tree):
    _, schemas = secret_tree
    (schemas / "bin.fbs").write_bytes(b"\xff\xfe\x00 table")
    with pytest.raises(SchemaError, match="not UTF-8"):
        flatview.load_schema(_main(schemas, "bin.fbs"))


# ---------------------------------------------------------------------------
# include confinement across platforms: lexical gate + identity containment

import ntpath
import posixpath

from flatview._fbs import _fold, _lexically_inside


@pytest.mark.parametrize("cand, inside", [
    ("C:\\Schemas\\x.fbs", True),
    ("c:\\SCHEMAS\\sub\\x.fbs", True),                 # case-insensitive, as NTFS is
    ("C:/Schemas/x.fbs", True),                        # forward slashes
    ("C:\\Schemas\\..\\secret.txt", False),
    ("C:\\Schemas2\\x.fbs", False),                    # prefix lookalike
    ("D:\\Schemas\\x.fbs", False),                     # another drive
    ("D:x.fbs", False),                                # drive-relative
    ("\\\\attacker\\share\\x.fbs", False),             # UNC: would leak NTLM credentials
    ("//attacker/share/x.fbs", False),
    ("\\\\?\\C:\\Windows\\win.ini", False),            # device namespace
    ("\\\\.\\pipe\\x", False),
])
def test_lexical_gate_windows_rules(cand, inside):
    roots = [_fold("C:\\Schemas", ntpath)]
    assert _lexically_inside(cand, roots, ntpath) is inside


@pytest.mark.parametrize("cand, inside", [
    ("/srv/schemas/x.fbs", True),
    ("/SRV/Schemas/x.fbs", True),                      # case-insensitive (macOS default)
    ("/srv/schemas/../secret", False),
    ("/srv/schemas2/x.fbs", False),
    ("/net/attacker/x.fbs", False),                    # automount
    ("relative/x.fbs", False),
])
def test_lexical_gate_posix_rules(cand, inside):
    assert _lexically_inside(cand, [_fold("/srv/schemas", posixpath)], posixpath) is inside


# (On Windows the same holds for "\\\\attacker\\share": see the ntpath gate test.
# On POSIX a backslash is an ordinary filename character, so that string is a
# local name there.)
@pytest.mark.parametrize("inc", ["//attacker/share/x.fbs", "/net/attacker/x.fbs"])
def test_outside_include_never_touches_the_file_system(secret_tree, monkeypatch, inc):
    _, schemas = secret_tree
    main_path = _main(schemas, inc)
    seen = []
    for name, mod in (("realpath", os.path), ("isfile", os.path), ("stat", os), ("open", os)):
        orig = getattr(mod, name)
        def spy(p, *a, _orig=orig, **kw):
            seen.append(os.fspath(p) if isinstance(p, (str, os.PathLike)) else p)
            return _orig(p, *a, **kw)
        monkeypatch.setattr(mod, name, spy)
    with pytest.raises(SchemaError, match="refusing to include"):
        flatview.load_schema(main_path)
    assert not [p for p in seen if isinstance(p, str) and "attacker" in p]


def test_root_spelled_differently_from_opened_path(secret_tree, monkeypatch):
    # Models macOS: include_paths given in one spelling ("~/Schemas") while the
    # opened file's path comes back in another ("~/schemas"). Two symlink
    # aliases of one directory play the two spellings. (Their names differ by
    # more than case: on macOS/Windows "LibAlias" and "libalias" are one name.)
    tmp, schemas = secret_tree
    lib = tmp / "lib"
    lib.mkdir()
    (lib / "lib.fbs").write_text("table Lib { v: int; }\n")
    os.symlink(lib, tmp / "alias-one", target_is_directory=True)
    os.symlink(lib, tmp / "alias-two", target_is_directory=True)
    monkeypatch.setattr(_fbs_mod, "_fd_path", lambda fd: str(tmp / "alias-two" / "lib.fbs"))
    s = flatview.load_schema(_main(schemas, "lib.fbs"), include_paths=[tmp / "alias-one"])
    assert "Lib" in s.objects


def test_main_schema_opened_through_symlinked_directory(secret_tree):
    tmp, schemas = secret_tree
    os.symlink(schemas, tmp / "via-link", target_is_directory=True)
    _main(schemas, "common.fbs")
    assert "Common" in flatview.load_schema(tmp / "via-link" / "main.fbs").objects


def test_include_with_nul_byte(secret_tree):
    _, schemas = secret_tree
    with pytest.raises(SchemaError, match="NUL"):
        flatview.load_schema(_main(schemas, "common.fbs\\u0000"))


# ---------------------------------------------------------------------------
# the size cap must not cost a cap-sized allocation

@pytest.mark.parametrize("kind", ["plain", "gzip", "zstd", "schema", "open-no-mmap"])
def test_capped_reads_allocate_only_what_they_read(tmp_path, kind):
    import tracemalloc
    payload = (SDE / "categories.fbs").read_bytes()
    p = tmp_path / "x.fbs"
    if kind == "gzip":
        p.write_bytes(_gz(payload))
    elif kind == "zstd":
        zstandard = pytest.importorskip("zstandard")
        p.write_bytes(zstandard.ZstdCompressor().compress(payload))
    else:
        p.write_bytes(payload)
    tracemalloc.start()
    try:
        if kind == "schema":
            flatview.load_schema(p)
        elif kind == "open-no-mmap":
            data = tmp_path / "x.fb"
            data.write_bytes(flatview.load_schema(p).encode({"rows": []}))
            tracemalloc.reset_peak()
            flatview.open(data, flatview.load_schema(p), use_mmap=False)
        else:
            assert flatview.read_bytes(p) == payload
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 16 << 20, f"{peak / 2**20:.0f} MB for a {len(payload)}-byte file"   # was 2048 MB


def _gz(data: bytes) -> bytes:
    z = zlib.compressobj(9, zlib.DEFLATED, 31)
    return z.compress(data) + z.flush()


# -- .bfbs layout checks the .fbs parser already makes -------------------------

def _crafted_bfbs(tmp_path, src: str, mutate) -> bytes:
    """flatc's .bfbs for ``src`` (with built-in attributes), edited by ``mutate``."""
    p = tmp_path / "s.fbs"
    p.write_text(src)
    refl = flatview.reflection_schema()
    raw = refl.read(flatc_bfbs(p, builtins=True))._asdict(defaults=False, enums="value")
    mutate({o["name"]: {f["name"]: f for f in o.get("fields", [])} for o in raw["objects"]})
    return refl.encode(raw)


NESTED_SRC = 'table I { x: int; } table O { n: [ubyte] (nested_flatbuffer: "I"); s: [string]; } root_type O;'


@needs_flatc
def test_bfbs_nested_flatbuffer_only_on_ubyte_vectors(tmp_path):
    # the verifier checked [string] as strings while the reader parsed it as a nested buffer
    def move(objs):
        objs["O"]["s"]["attributes"] = objs["O"]["n"].pop("attributes")
    with pytest.raises(SchemaError, match=r"O\.s: nested_flatbuffer field must be \[ubyte\]"):
        flatview.load_bfbs(_crafted_bfbs(tmp_path, NESTED_SRC, move))


@needs_flatc
@pytest.mark.parametrize("target", ["Nope", "P"])
def test_bfbs_nested_flatbuffer_target_must_be_a_table(tmp_path, target):
    def retarget(objs):
        for kv in objs["O"]["n"]["attributes"]:
            if kv["key"] == "nested_flatbuffer":
                kv["value"] = target
    src = NESTED_SRC.replace("table O", "struct P { a: int; } table O")
    with pytest.raises(SchemaError, match="is not a table in the schema"):
        flatview.load_bfbs(_crafted_bfbs(tmp_path, src, retarget))


@needs_flatc
def test_bfbs_union_vector_needs_a_type_vector(tmp_path):
    def scalarise(objs):
        t = objs["T"]["v_type"]["type"]
        t["base_type"] = int(flatview.BaseType.UTYPE)
        t.pop("element", None)
    src = "table M { x: int; } union U { M } table T { v: [U]; } root_type T;"
    with pytest.raises(SchemaError, match="without a matching v_type field"):
        flatview.load_bfbs(_crafted_bfbs(tmp_path, src, scalarise))


# -- compiled code is per table object, not per table name ----------------------

def test_compiled_caches_never_mix_tables_of_the_same_name():
    # root= accepts another schema's Object; caches keyed by name handed that
    # table's compiled converter/encoder/verifier to this schema's own table
    a = parse_fbs("table X { s: string; } root_type X;")
    b = parse_fbs("table X { n: int; } root_type X;")
    data_b = b.encode({"n": 12345})
    a.verify(data_b, b.root)                                   # valid as b.X ...
    assert a.read(data_b, b.root)._asdict() == {"n": 12345}
    assert a.encode({"n": 7}, b.root) == b.encode({"n": 7})
    with pytest.raises(VerificationError, match="offset to string"):
        a.verify(data_b)                                       # ... but not as a.X
    data_a = a.encode({"s": "hi"})
    assert a.read(data_a)._asdict() == {"s": "hi"}            # a.X's own converter
    assert b.read(data_b)._asdict() == {"n": 12345}
    with pytest.raises(flatview.EncodeError, match="unknown field"):
        a.encode({"n": 7})                                     # a.X's own encoder


NESTED_A = 'table I { x: int; } table O { n: [ubyte] (nested_flatbuffer: "I"); } root_type O;'
NESTED_B = 'table I { s: string; } table O { n: [ubyte] (nested_flatbuffer: "I"); } root_type O;'


@pytest.mark.parametrize("first", ["own", "foreign"])
def test_nested_flatbuffer_target_belongs_to_its_own_schema(first):
    # The nested root "I" used to be looked up in whichever schema first used
    # the table (views) or was doing the work (converters, encoder, verifier).
    a, b = parse_fbs(NESTED_A), parse_fbs(NESTED_B)
    assert b.root.field("n").nested is b.objects["I"]
    data_b = b.encode({"n": {"s": "hi"}})

    def through_a():
        a.verify(data_b, b.root)
        assert a.read(data_b, b.root).n.s == "hi"
        assert a.read(data_b, b.root)._asdict() == {"n": {"s": "hi"}}
        assert a.encode({"n": {"s": "hi"}}, b.root) == data_b

    def own():
        b.verify(data_b)
        assert b.read(data_b).n.s == "hi"
        assert b.read(data_b)._asdict() == {"n": {"s": "hi"}}

    for step in ((own, through_a) if first == "own" else (through_a, own)):
        step()
    data_a = a.encode({"n": {"x": 5}})
    assert a.read(data_a).n.x == 5 and a.read(data_a)._asdict() == {"n": {"x": 5}}


@needs_flatc
def test_bfbs_nested_target_resolved_at_load(tmp_path):
    p = tmp_path / "b.fbs"
    p.write_text(NESTED_B)
    b = flatview.load_bfbs(flatc_bfbs(p, builtins=True))
    assert b.root.field("n").nested is b.objects["I"]
