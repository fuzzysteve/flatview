# flatview

Schema-driven FlatBuffers for Python. Load a `.fbs` or `.bfbs` schema at
runtime and get lazy, zero-copy, Pythonic views over the data: no `flatc`
code generation, no `FooLength()` / `Foo(j)` accessor pairs, and key lookup
that actually exists.

```python
import flatview

fb = flatview.open("types.fb")          # finds types.bfbs / types.fbs next to it
rifter = fb.rows.lookup(587)            # binary search on the schema's `key` field
rifter.name.en                          # 'Rifter'
rifter.groupID, rifter.portionSize      # (25, 1)
rifter.factionID                        # 500002
rifter.metaLevel                        # 0  - present in the buffer
fb.rows.lookup(0).basePrice             # None - `double = null`, absent means None, not 0.0
rifter._asdict()                        # plain dicts/lists, recursively
```

Built and tested against CCP's EVE SDE FlatBuffers
(`https://sde.evetech.net/tranquility/latest/`). All 104 SDE schemas parse
identically from `.fbs` and `.bfbs`, and every SDE table tested decodes
exactly like `flatc --json`, `types.fb` (150 MB) included.

## Why not the official `flatbuffers` package?

| | official `flatbuffers` + `flatc --python` | flatview |
|---|---|---|
| Schema | generate code per schema, regenerate on change | load `.fbs`/`.bfbs` at runtime |
| Field access | `r.Name().En().decode()`, `r.GroupId()` (names re-cased) | `r.name.en`, `r.groupID` (schema names) |
| Vectors | `r.PlanetIdsLength()` + `r.PlanetIds(j)` | real `Sequence`: `len`, slicing, iteration, `.tolist()` |
| Key lookup | none in Python, so a linear scan | `rows.lookup(587)`, `find`, `bisect`, `index_by`, `group_by` |
| Optional scalars (`= null`) | supported | `None` when absent, `_has(name)` for presence |
| Enums | bare ints | `IntEnum` / `IntFlag` members |
| Verifier | none | full: bounds, alignment, UTF-8, required, unions, key order, depth |
| Files | you read bytes yourself | mmap, gzip/zstd by magic number, schema discovery |
| Writing | hand-order `StartObject`/`Add…`/`EndObject` | `schema.encode(dict_or_view)`, keyed vectors auto-sorted |
| To Python | object API (`--gen-object-api`) | `_asdict()` on any view |

### Speed

`benchmarks/bench_vs_official.py`, best of several runs, same machine, SDE build 3720952:

| Operation | flatview | official | speed-up |
|---|---|---|---|
| Scan 53,941 types, 4 fields each | 105 ms | 468 ms | 4.5× |
| 1,000 lookups by typeID (official: one shared scan) | 13 ms | 261 ms | ~20× |
| Single lookup, typeID 587 | ~0.01 ms | 1.1 ms | ~80× |
| Sum one attribute over ~1M typeDogma entries | 0.57 s | 3.2 s | 5.7× |
| All `planetIDs` vectors to lists | 19 ms | 205 ms | 11× |
| Decode all of types.fb to Python objects | 0.81 s | 4.38 s | 5.4× |

Why: each schema field becomes a generated property that does one vtable
probe and one `struct.unpack_from`, with nothing worked out per call; scalar
vectors decode in a single C call; keyed vectors are binary-searched. Whole-tree
conversion (`_asdict()`) uses a converter compiled per table type, which reads
each vtable in one call and creates no intermediate view objects.

### Against JSON too

`benchmarks/bench_three_way.py`: flatview and the official library read
`types.fb`, the standard library `json` module reads the SDE's `types.jsonl`
export of the same build (153 MB). Each run is a fresh process and includes
opening the file; cells are wall time / peak Python heap.

| Scenario | flatview | official | json |
|---|---|---|---|
| Look up one type (early id 587) | 0.7 ms / 0 MB | 76 ms / 143 MB | 4.0 ms / 0.1 MB |
| Look up one type (last id) | 0.8 ms / 0 MB | 334 ms / 143 MB | 757 ms / 0.3 MB |
| 1,000 lookups (others build an index first) | 20 ms / 0.1 MB | 355 ms / 153 MB | 917 ms / 262 MB |
| Scan all rows, 4 fields | 186 ms / 0 MB | 774 ms / 143 MB | 782 ms / 0.3 MB |
| Everything as Python objects | 780 ms / 215 MB | 4440 ms / 332 MB | 899 ms / 260 MB |

For the official library, "everything as Python objects" means its object API
(`--gen-object-api`); flatview and json produce plain dicts and lists.

### Memory

`benchmarks/bench_memory.py`, fresh process per scenario, `types.fb` (150 MB):

| Scenario | flatview heap | official heap |
|---|---|---|
| Open the file | 0.0 MB | 142.7 MB |
| Scan all rows | 0.1 MB | 142.7 MB |
| 1,000 key lookups | 0.1 MB | 142.9 MB |
| Keep all 53,941 row objects | 7.0 MB | 149.3 MB |
| Decode everything to Python | 215 MB | 332 MB |

Uncompressed files are memory-mapped, so the file is never copied onto the
heap. Pages you touch show up in RSS as clean page cache, which is shared
and which the OS can drop.

## Install / develop

```bash
uv sync                       # creates .venv with pytest
uv run pytest                 # full suite
uv run pytest -m "not sde_full"   # skip the big-file tests (fast, ~1 s)
uv run python scripts/fetch_sde_examples.py   # fetch SDE files for sde_full tests/benchmarks
uv run --group bench python benchmarks/bench_vs_official.py
uv run --group bench python benchmarks/bench_three_way.py   # + stdlib json on the JSONL export
uv run --group bench python benchmarks/bench_memory.py
```

There are no runtime dependencies (Python ≥ 3.11). Tests that compare with
`flatc` are skipped if it isn't on `PATH`.

## Usage

### Schemas

```python
schema = flatview.load_schema("types.bfbs")   # or .fbs; gzip fine; format detected by content
schema = flatview.parse_fbs(text, "x.fbs", include_paths=[...])
schema.root                     # <table sde.types.TypesTable>
schema["Types"].fields          # short or fully-qualified names
schema["Types"].key_field       # <Field typeID: int (id 0, key)>
schema["DogmaEffectsModifierInfo"].field("func").attributes   # {'sde_enum_name': '0'}
schema["EffectCategory"].python # IntEnum
print(schema.to_fbs())          # e.g. turn a .bfbs back into readable .fbs
```

`.bfbs` support is bootstrapped: flatview parses the bundled
`reflection.fbs` with its own parser, then reads `.bfbs` files with its own
reader. Note that flatc leaves out doc comments (`--bfbs-comments`) and
built-in attributes such as `nested_flatbuffer` (`--bfbs-builtins`) unless
asked. If you need those, use the `.fbs`.

### Reading

```python
fb = flatview.open(path, schema=None, verify=False, use_mmap=True)
root = schema.read(data)          # bytes / bytearray / mmap / memoryview
root = schema.read(data, "Other") # any table as root; size prefix auto-detected

row.field / row["field"]          # value, default, or None
row._has("field")                 # physically present in the buffer?
row._get("field", default)        # value only if present
row._asdict(defaults=False, enums="name")   # like flatc --json
pos.x, tuple(pos), x, y, z = pos  # structs are values (iterable, tuple-equal)
vec[3], vec[-1], vec[2:10]        # slices are lazy views
vec.tolist()                      # one C call for scalar vectors
rows.lookup(k) / rows.find(k) / rows.bisect(k) / rows.keys() / rows.is_sorted()
rows.index_by("name") / rows.group_by(lambda r: r.groupID)
```

Helper names start with `_` (namedtuple style) so they can never hide a
schema field; `row["name"]` always works for any field name.

Unions return the member table view (and `field_type` gives the enum member).
`nested_flatbuffer` fields return the nested root view.

### Verifying

```python
schema.verify(data, strict_enums=False, check_sorted=True, check_alignment=True,
              check_utf8=True, max_depth=64, strict_table_size=False)
# raises VerificationError('rows[17].name.en: string is not valid UTF-8 ...')
```

`check_sorted` matters: FlatBuffers lookups assume keyed vectors are sorted,
so an unsorted vector makes `lookup()` silently wrong. Plain reads are
memory-safe without verification (it's Python), but verify untrusted input.

### Writing

```python
data = schema.encode({"rows": [{"typeID": 2, "name": {"en": "x"}}, ...]})
data = schema.encode(fb.root)                       # copy a view
d = fb.root._asdict(defaults=False); ...; schema.encode(d)   # modify a copy
```

Vectors of keyed tables are sorted by key (bytewise for strings, as
FlatBuffers requires). Identical vtables and strings are written once. Every
value is range-checked; enums accept names (`"Minmatar"`, or `"A B"` for
bit flags); unknown fields and missing required fields raise `EncodeError`
with the path to the value, using your input indices. Optional scalars
(`= null`) are written only when not `None`.

### CLI

```
flatview info   types.fb
flatview get    types.fb 587
flatview dump   types.fb --jsonl --limit 5
flatview verify typeDogma.fb --strict-enums
flatview schema types.bfbs
flatview encode schema.fbs data.json -o out.bin
```

## Notes on the EVE SDE files

* CCP serves `.fb`, `.fbs` and `.bfbs` as raw gzip bodies under their plain
  names (no `Content-Encoding`). flatview detects compression by magic number.
* Every table is `root_type XTable { rows: [X] (required) }` with `key` on the
  ID field, and rows are sorted, so `fb.rows.lookup(id)` is the natural
  access pattern. A key equal to its default (e.g. `categoryID` 0) is omitted
  by the writer, but lookups and `_asdict()` still report it.
* Custom attributes (`sde_value`, `sde_enum_name`, `sde_name`) describe how
  the JSONL form is shaped. They're exposed in `Field.attributes` /
  `EnumVal.attributes`.
* Some SDE tables record a vtable table size 4 bytes short of a trailing
  8-byte field (e.g. `blueprints` invention `probability`). The data is
  intact, and flatc's verifier doesn't check field offsets against that size
  either, so neither does flatview by default. `strict_table_size=True` will
  flag it.

## Not supported (yet)

64-bit offsets/vectors (`offset64`, `vector64`), FlexBuffers (`flexbuffer`
attribute), big-endian hosts are untested (all access is explicitly
little-endian), and JSON-with-schema parsing in `.fbs` files.
