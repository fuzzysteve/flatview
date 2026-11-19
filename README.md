# flatview

Schema-driven [FlatBuffers](https://flatbuffers.dev/) for Python. Load a
`.fbs` or `.bfbs` schema at runtime and get lazy, zero-copy, Pythonic views
over any FlatBuffer: no `flatc` code generation, no `FooLength()` / `Foo(j)`
accessor pairs, binary-search key lookup, a full verifier, and an encoder
that takes plain Python data.

Pure Python (≥ 3.11), no runtime dependencies.

## Quick start

Given a schema, `shop.fbs` ([examples/shop.fbs](examples/shop.fbs)):

```fbs
namespace shop;

enum Category : ubyte { Tool, Toy, Food }
struct Dimensions { w: float; h: float; d: float; }

table Product {
  sku: string (key);
  name: string (required);
  price: double = null;   // optional: absent reads as None
  stock: int;
  category: Category;
  size: Dimensions;
  tags: [string];
}

table Catalog { products: [Product]; }
root_type Catalog;
file_identifier "SHOP";
```

```python
import flatview

schema = flatview.load_schema("shop.fbs")            # or a .bfbs from flatc

data = schema.encode({"products": [                  # Python data -> FlatBuffer bytes
    {"sku": "T-100", "name": "Hammer", "price": 12.5, "stock": 40, "category": "Tool",
     "size": {"w": 30.0, "h": 12.0, "d": 3.0}, "tags": ["steel", "claw"]},
    {"sku": "F-007", "name": "Tea", "stock": 0, "category": "Food"},
    {"sku": "K-042", "name": "Kite", "price": 20.0, "category": "Toy"},
]})
open("catalog.shop", "wb").write(data)

fb = flatview.open("catalog.shop", schema)            # memory-mapped; decodes lazily
products = fb.root.products
products.keys()                  # ['F-007', 'K-042', 'T-100']  (keyed vectors are sorted on write)
hammer = products.lookup("T-100")   # binary search on the `key` field
hammer.name, hammer.price        # ('Hammer', 12.5)
hammer.category.name             # 'Tool'  (an IntEnum member)
w, h, d = hammer.size            # structs unpack like tuples
list(hammer.tags)                # ['steel', 'claw']
tea = products.lookup("F-007")
tea.price, tea._has("price")     # (None, False)  optional scalar
products.find("X-999")           # None
tea._asdict(defaults=False, enums="name")
# {'sku': 'F-007', 'name': 'Tea', 'category': 'Food'}
fb.verify()                      # full structural check; raises on corruption
```

The runnable version is [examples/quickstart.py](examples/quickstart.py).
The test suite runs it and checks its output.

If the schema file sits next to the data with the same stem (`x.fb` and
`x.bfbs`/`x.fbs`), `flatview.open("x.fb")` finds it on its own.

**How it's tested:** every schema feature (structs, arrays, enums,
bit_flags, unions and vectors of unions, nested flatbuffers, includes,
optional scalars, size prefixes, file identifiers) is cross-checked against
`flatc` in both directions. It is also tested on a large real-world
dataset, the EVE Online SDE (see [the worked example](#worked-example-the-eve-online-sde)):
all 104 of its schemas, and every table decoded identically to `flatc --json`.

## Why not the official `flatbuffers` package?

| | official `flatbuffers` + `flatc --python` | flatview |
|---|---|---|
| Schema | generate code per schema, regenerate on change | load `.fbs`/`.bfbs` at runtime |
| Field access | `p.Name().decode()`; names re-cased (`typeID` → `TypeId()`) | `p.name`; schema names as written |
| Vectors | `p.TagsLength()` + `p.Tags(j)` | real `Sequence`: `len`, slicing, iteration, `.tolist()` |
| Key lookup | none in Python, so a linear scan | `products.lookup("T-100")`, `find`, `bisect`, `index_by`, `group_by` |
| Optional scalars (`= null`) | supported | `None` when absent, `_has(name)` for presence |
| Enums | bare ints | `IntEnum` / `IntFlag` members |
| Verifier | none | full: bounds, alignment, UTF-8, required, unions, key order, depth |
| Files | you read bytes yourself | mmap, gzip/zstd by magic number, schema discovery |
| Writing | hand-order `StartObject`/`Add…`/`EndObject` | `schema.encode(dict_or_view)`, keyed vectors auto-sorted |
| To Python | object API (`--gen-object-api`) | `_asdict()` on any view |

### Speed

Measured on real data: the [EVE Online SDE](#worked-example-the-eve-online-sde)
(build 3720952), whose `types.fb` is 150 MB with 53,941 rows.
`benchmarks/bench_vs_official.py`, best of several runs, same machine:

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
schema = flatview.load_schema("shop.bfbs")    # or .fbs; gzip fine; format detected by content
schema = flatview.parse_fbs(text, "x.fbs", include_paths=[...])
schema.root                     # <table shop.Catalog (1 fields)>
schema["Product"].fields        # short or fully-qualified ("shop.Product") names
schema["Product"].key_field     # <Field sku: string (id 0, required, key)>
schema["Product"].field("price").optional   # True
schema["Category"].python       # IntEnum
field.attributes                # user-defined attributes, e.g. {'my_attr': 'value'}
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
fb.products                       # shorthand for fb.root.products

row.field / row["field"]          # value, default, or None
row._has("field")                 # physically present in the buffer?
row._get("field", default)        # value only if present
row._asdict(defaults=False, enums="name")   # like flatc --json
pos.x, tuple(pos), x, y, z = pos  # structs are values (iterable, tuple-equal)
vec[3], vec[-1], vec[2:10]        # slices are lazy views
vec.tolist()                      # one C call for scalar vectors
vec.lookup(k) / vec.find(k) / vec.bisect(k) / vec.keys() / vec.is_sorted()   # keyed tables
vec.index_by("name") / vec.group_by(lambda p: p.category)                    # any field
```

Helper names start with `_` (namedtuple style) so they can never hide a
schema field; `row["name"]` always works for any field name.

Unions return the member table view (and `field_type` gives the enum member).
`nested_flatbuffer` fields return the nested root view.

### Verifying

```python
schema.verify(data, strict_enums=False, check_sorted=True, check_alignment=True,
              check_utf8=True, max_depth=64, strict_table_size=False)
# raises VerificationError('products[17].name: string is not valid UTF-8 ...')
```

`check_sorted` matters: FlatBuffers lookups assume keyed vectors are sorted,
so an unsorted vector makes `lookup()` silently wrong. Plain reads are
memory-safe without verification (it's Python), but verify untrusted input.

### Writing

```python
data = schema.encode({"products": [{"sku": "A-1", "name": "Anvil", "category": "Tool"}, ...]})
data = schema.encode(fb.root)                       # copy a view
d = fb.root._asdict(defaults=False); ...; schema.encode(d)   # modify a copy
```

Vectors of keyed tables are sorted by key (bytewise for strings, as
FlatBuffers requires). Identical vtables and strings are written once. Every
value is range-checked; enums accept names (`"Tool"`, or `"A B"` for
bit flags); unknown fields and missing required fields raise `EncodeError`
with the path to the value, using your input indices. Optional scalars
(`= null`) are written only when not `None`.

### CLI

```
flatview encode shop.fbs catalog.json -o catalog.shop
flatview info   catalog.shop -s shop.fbs      # -s is optional if catalog.fbs/.bfbs sits next to it
flatview get    catalog.shop T-100 -s shop.fbs
flatview dump   catalog.shop --jsonl --limit 5 -s shop.fbs
flatview verify catalog.shop --strict-enums -s shop.fbs
flatview schema shop.bfbs                     # print a binary schema as .fbs
```

## Worked example: the EVE Online SDE

CCP publishes EVE Online's Static Data Export as FlatBuffers at
`https://sde.evetech.net/tranquility/latest/`, with a `.fb`, `.fbs` and
`.bfbs` per table. This is where most of the benchmarks above come from.

```python
fb = flatview.open("types.fb")          # finds types.bfbs next to it; gzip handled
rifter = fb.rows.lookup(587)            # every SDE table is `rows: [X]` keyed by its ID
rifter.name.en                          # 'Rifter'
rifter.groupID, rifter.portionSize      # (25, 1)
fb.rows.lookup(0).basePrice             # None: `double = null`, absent means None, not 0.0
rifter._asdict()                        # plain dicts/lists, recursively
```

`scripts/fetch_sde_examples.py` downloads a set of tables for the
`sde_full` tests and the benchmarks. Things worth knowing about these files:

* CCP serves `.fb`, `.fbs` and `.bfbs` as raw gzip bodies under their plain
  names (no `Content-Encoding`). flatview detects compression by magic number.
* Every table is `root_type XTable { rows: [X] (required) }` with `key` on the
  ID field, and rows are sorted, so `fb.rows.lookup(id)` is the natural
  access pattern. A key equal to its default (e.g. `categoryID` 0) is omitted
  by the writer, but lookups and `_asdict()` still report it.
* Custom attributes (`sde_value`, `sde_enum_name`, `sde_name`) describe how
  the JSONL form is shaped. They're exposed in `Field.attributes` /
  `EnumVal.attributes`.
* Some SDE tables share a vtable whose recorded table size is too small for
  them. This is a bug in the Go FlatBuffers builder, which CCP's files are
  written with: vtable de-duplication ignores the object size
  ([google/flatbuffers#9413](https://github.com/google/flatbuffers/issues/9413)).
  In `blueprints.fb`, one vtable (recorded size 16) is shared by
  `Blueprints`, `Activity`, `ProductionActivity` and all 1,353
  `InventionProduct` tables, which need 20 bytes because of their
  `probability` double. The data is intact. flatc's C++ verifier only bounds
  fields against the buffer, so it accepts these files, and so does flatview
  by default. `strict_table_size=True` flags them, as flatcc's verifier would.
  flatview's own builder compares the whole vtable, size included, so it
  doesn't produce such files.

## Not supported (yet)

64-bit offsets/vectors (`offset64`, `vector64`), FlexBuffers (`flexbuffer`
attribute), big-endian hosts are untested (all access is explicitly
little-endian), and JSON-with-schema parsing in `.fbs` files.

## License

MIT. See [LICENSE](LICENSE).
