"""The compiled per-table verifiers against the general field-by-field code.

A compiled verifier must accept exactly what the general one accepts and,
when it rejects, give the very same error (it re-checks the table with the
general code). Checked on seeded random corruptions of real buffers under
every option set; WIDE_TABLE = 0 makes every table use the general code.
"""

import random
import struct

import pytest

import flatview
import flatview._verify as fv
from flatview import parse_fbs

from conftest import DATA, KITCHEN, SDE
from test_old_bfbs import KITCHEN_VALUE
from test_union_members import CASES

OPTS = [{}, {"strict_enums": True}, {"strict_table_size": True}, {"check_alignment": False},
        {"check_utf8": False, "check_sorted": False}, {"max_refs": 40}, {"max_depth": 2}]

NESTED = parse_fbs('struct V (force_align: 16) { x: float; } table I { v: V; s: string; } '
                   'table O { pad: byte; n: [ubyte] (nested_flatbuffer: "I"); ns: [I]; } root_type O;')


def _samples():
    k = flatview.load_schema(KITCHEN / "kitchen.fbs")
    yield "kitchen", k, k.encode(KITCHEN_VALUE), {}
    yield "kitchen-sp", k, k.encode(KITCHEN_VALUE, size_prefixed=True), {"size_prefixed": True}
    m = flatview.load_schema(DATA / "unions" / "members.fbs")
    for n, v in CASES.items():
        yield f"members-{n}", m, m.encode(v), {}
    yield "nested", NESTED, NESTED.encode({"pad": 1, "n": {"v": {"x": 1.0}, "s": "hi"},
                                           "ns": [{"s": "a"}, {}]}), {}
    for name in ("masteries", "dogmaEffects"):
        s = flatview.load_schema(SDE / f"{name}.bfbs")
        rows = s.read(flatview.read_bytes(SDE / f"{name}.fb"))._asdict(defaults=False, enums="value")["rows"]
        yield name, s, s.encode({"rows": rows[:30]}), {}         # 30 real rows: small enough to corrupt


SAMPLES = list(_samples())


def _corrupt(rng: random.Random, data: bytes) -> bytes:
    b = bytearray(data)
    for _ in range(rng.choice([1, 1, 2, 3])):
        kind, pos = rng.random(), rng.randrange(len(b))
        if kind < 0.4:
            b[pos] = rng.randrange(256)
        elif kind < 0.6 and pos + 2 <= len(b):
            struct.pack_into("<H", b, pos, rng.choice([0, 1, 2, 3, 4, 6, 0xFFFF, rng.randrange(65536)]))
        elif kind < 0.85 and pos + 4 <= len(b):
            struct.pack_into("<I", b, pos, rng.choice([0, 1, 4, 8, len(b), 0xFFFFFFFF, rng.randrange(len(b) + 8)]))
        elif kind < 0.95:
            b = b[:pos + 1]
        else:
            b += bytes(rng.randrange(1, 9))
    return bytes(b)


def _result(schema, data, **kw) -> str:
    try:
        schema.verify(data, **kw)
        return "OK"
    except flatview.VerificationError as e:
        return f"VerificationError {e}"


def _general(schema, data, **kw) -> str:
    old = fv.WIDE_TABLE
    fv.WIDE_TABLE = -1
    schema.__dict__.pop("_verifiers", None)
    try:
        return _result(schema, data, **kw)
    finally:
        fv.WIDE_TABLE = old
        schema.__dict__.pop("_verifiers", None)


@pytest.mark.parametrize("name,schema,data,extra", SAMPLES, ids=[s[0] for s in SAMPLES])
def test_same_results_on_corrupt_buffers(name, schema, data, extra):
    rng = random.Random(name)
    bufs = [data] + [_corrupt(rng, data) for _ in range(60)]
    compiled = [[_result(schema, b, **extra, **o) for o in OPTS] for b in bufs]
    general = [[_general(schema, b, **extra, **o) for o in OPTS] for b in bufs]
    assert compiled == general
    assert compiled[0] == ["OK"] * 3 + ["OK", "OK"] + compiled[0][5:]     # the original verifies
    assert sum(r == "OK" for row in compiled[1:] for r in row) < len(bufs) * len(OPTS)  # corruption bites


def test_failure_rechecks_from_the_same_budget():
    # a failure late in a table must not double-count the offsets followed
    # before it: the error is the general one, not "followed more than N offsets"
    s = parse_fbs("table C { s: string; } table T { a: [C]; z: string (required); } root_type T;")
    from flatview._builder import Builder
    b = Builder()
    rows = [b.table([(0, "offset", b.create_string("x"), 4)]) for _ in range(5)]
    vec = b.create_offset_vector(rows)
    data = b.finish(b.table([(0, "offset", vec, 4)]))            # z missing
    want = "z: required field is missing"
    for refs in (11, 12, 13, None):           # 1 root + 1 vector + 5 rows + 5 strings = 12 offsets
        got = _result(s, data, max_refs=refs) if refs else _result(s, data)
        assert got == _general(s, data, **({"max_refs": refs} if refs else {}))
        if refs is None or refs >= 12:
            assert got.endswith(want)


def test_verifiers_never_embed_schema_names():
    inject = "x'] ; import builtins; builtins.FLATVIEW_PWNED = True; ['"
    s = parse_fbs("table A { v: int; w: string; t: A; } root_type A;")
    a = s.objects["A"]
    a.name = inject + "\nimport builtins; builtins.FLATVIEW_PWNED = True"
    a.fields[0].name = inject
    a._by_name = None
    data = s.encode({inject: 5, "w": "s", "t": {inject: 1}})
    s.verify(data)
    import builtins
    assert not getattr(builtins, "FLATVIEW_PWNED", False)


def test_wide_table_verifier_is_cheap():
    import tracemalloc
    n = 32765
    s = parse_fbs("table T { " + " ".join(f"f{i}: int;" for i in range(n)) + " } root_type T;")
    data = s.encode({"f0": 1, f"f{n - 1}": 2})
    tracemalloc.start()
    s.verify(data)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 64 << 20        # verified field by field: no 32k-branch function compiled
