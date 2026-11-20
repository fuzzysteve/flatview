# flatview

[![tests](https://github.com/fuzzysteve/flatview/actions/workflows/tests.yml/badge.svg)](https://github.com/fuzzysteve/flatview/actions/workflows/tests.yml)

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
`benchmarks/bench_vs_official.py`, best of several runs, same machine (re-run November 2026):

| Operation | flatview | official | speed-up |
|---|---|---|---|
| Scan 53,941 types, 4 fields each | 105 ms | 473 ms | 4.5× |
| 1,000 lookups by typeID (official: one shared scan) | 13.5 ms | 266 ms | ~20× |
| Single lookup, typeID 587 | ~0.01 ms | 1.1 ms | ~80× |
| Sum one attribute over ~1M typeDogma entries | 0.57 s | 3.2 s | 5.7× |
| All `planetIDs` vectors to lists | 19 ms | 207 ms | 11× |
| Decode all of types.fb to Python objects | 0.80 s | 4.42 s | 5.5× |

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
| Look up one type (early id 587) | 1.4 ms / 0 MB | 77 ms / 143 MB | 4.0 ms / 0.1 MB |
| Look up one type (last id) | 1.4 ms / 0 MB | 334 ms / 143 MB | 762 ms / 0.3 MB |
| 1,000 lookups (others build an index first) | 21 ms / 0.1 MB | 358 ms / 153 MB | 906 ms / 262 MB |
| Scan all rows, 4 fields | 189 ms / 0 MB | 767 ms / 143 MB | 784 ms / 0.3 MB |
| Everything as Python objects | 785 ms / 215 MB | 4403 ms / 332 MB | 901 ms / 260 MB |

For the official library, "everything as Python objects" means its object API
(`--gen-object-api`); flatview and json produce plain dicts and lists.

flatview's single-lookup time is mostly the fixed cost of `open()`: about
0.9 ms to load `types.bfbs`, more than half of it verifying the schema file
itself (see [Security](#security)). To skip that on repeated opens, load the
schema once and pass it in: `flatview.open(path, schema)`.

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

### Compatibility

- **Schema evolution:** data written with an older schema reads correctly
  with a newer one, and vice versa. New fields read as their defaults.
  Unknown fields are ignored, and unknown enum values and union members come
  back as plain ints / `None` (`tests/test_evolution.py`, cross-checked with
  flatc).
- **Old `.bfbs` files:** files written by flatc 1.12.0, 2.0.0 and 22.9.24
  load and decode identically to the `.fbs` (`tests/test_old_bfbs.py`).
- **Threads:** a schema can be shared between threads. Its lazily built
  classes are built once and never seen half-built, and the suite also
  passes on free-threaded Python 3.14.
- **Not tested:** big-endian machines (all reads are explicitly
  little-endian).

## Install / develop

```bash
uv sync                       # creates .venv with pytest
uv run python scripts/get_flatc.py   # fetch the flatc the cross-checks use (latest release, pinned)
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
open("shop.bfbs", "wb").write(schema.to_bfbs())   # .fbs -> .bfbs, no flatc needed
```

`to_bfbs()` writes what `flatc --schema -b --bfbs-comments --bfbs-builtins`
writes, checked field by field against flatc for every test schema, and flatc
accepts the result as a schema. It leaves out `fbs_files` (the include graph)
and RPC services, which flatview doesn't model. `comments=False,
builtins=False` gives plain `flatc --schema -b` output, but without the
built-in attributes, a reloaded schema loses `nested_flatbuffer`,
`force_align` and the like.

`.bfbs` support is bootstrapped: flatview parses the bundled
`reflection.fbs` with its own parser, then reads `.bfbs` files with its own
reader. Note that flatc leaves out doc comments (`--bfbs-comments`) and
built-in attributes such as `nested_flatbuffer` (`--bfbs-builtins`) unless
asked. If you need those, use the `.fbs`.

### Reading

```python
fb = flatview.open(path, schema=None, verify=False, use_mmap=True)
root = schema.read(data)          # bytes / bytearray / mmap / memoryview
root = schema.read(data, "Other") # any table as root
root = schema.read(data, size_prefixed=True)   # never guessed: say so explicitly
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
              check_utf8=True, max_depth=64, max_refs=None, strict_table_size=False)
# raises VerificationError('products[17].name: string is not valid UTF-8 ...')
```

`check_sorted` matters: FlatBuffers lookups assume keyed vectors are sorted,
so an unsorted vector makes `lookup()` silently wrong. It also rejects NaN
float keys, which can't be ordered at all. `max_refs` bounds how
many offsets are followed (default: buffer size / 4, the most a buffer
without shared sub-objects can contain). See [Security](#security).

### Writing

```python
data = schema.encode({"products": [{"sku": "A-1", "name": "Anvil", "category": "Tool"}, ...]})
data = schema.encode(fb.root)                       # copy a view
d = fb.root._asdict(defaults=False); ...; schema.encode(d)   # modify a copy
```

Vectors of keyed tables are sorted by key (bytewise for strings, as
FlatBuffers requires). A NaN float key is refused, because no sorted order
exists for it. Identical vtables and strings are written once. Every
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

## Security

flatview is written to handle untrusted input. What that covers, and what
it's up to you:

* **Untrusted buffers: verify them first.** `verify()` (or `open(...,
  verify=True)`) checks every offset, length, alignment, string and union
  before you read. Reads without verification are still memory-safe (it's
  Python: every read is bounds-checked), but malformed data can raise
  ordinary exceptions such as `struct.error`, and a hostile buffer can make
  `_asdict()` build enormous output.
* **Reference amplification.** A buffer can point at the same sub-table
  many times, so a few KB can describe millions of tables. The verifier
  rejects buffers that follow more offsets than `max_refs`. Raise it for
  trusted data that deliberately shares tables.
* **Input size.** Reading stops at `max_size` (default 2 GiB, the largest
  possible FlatBuffer) with `DecompressionLimitError`. That covers
  compressed files, what they decompress to, and special files such as
  FIFOs or `/dev/zero`. Only regular files are memory-mapped.
* **Untrusted `.bfbs` schemas.** These are verified against `reflection.fbs`,
  every name must be a valid identifier, field ids must run 0..n−1, and
  struct and table layouts are checked before use. Generated converter
  code never contains schema text, and its cost grows linearly with schema
  size (tables over 256 fields use a generic converter).
* **Untrusted `.fbs` schemas.** `include` can only reach files inside the
  schema's own directory or the `include_paths` you pass. Paths are checked
  after resolving symlinks and `..`, and there is no current-directory
  fallback. The opened file itself is re-checked, so swapping in a symlink
  or FIFO between the check and the open doesn't work either. An include
  written outside the allowed directories (an absolute path, a Windows UNC
  share like `\\host\share`, another drive) is refused before anything
  touches the disk or network. Containment is checked by file identity,
  so case-insensitive file systems and other spellings of a directory work. Unlike flatc, an untrusted schema can't read `/etc/...` or
  `../secret`. Schema text with no file (`parse_fbs(text)`) can only
  include from `include_paths`. Pass `allow_includes=False` to reject
  includes outright.
* **Rendering.** `Schema.to_fbs()` (and `flatview schema`) refuses names
  that aren't identifiers and escapes every string and doc comment, so a
  schema loaded from an untrusted `.bfbs` can't inject declarations, such
  as an `include`, into the text it produces.
* **Code execution.** The only dynamic code is the per-table converter
  compiled with `exec`. Its source is built from fixed templates, `repr()`
  strings and checked integers only, never schema text. There is no
  `pickle`, `eval`, subprocess or import driven by data.
* **Size prefixes are never guessed.** A valid buffer's first word can
  equal its length minus 4, so pass `size_prefixed=True` explicitly.
* **Schema names.** Enum value names that Python's `enum` module reserves
  (`_missing_`, `mro`, dunders) make that enum read as plain ints rather
  than fail, and fields named `__x__` are reachable only as `view["__x__"]`.
* **Terminal output.** The CLI never prints control characters from the
  data raw. `dump`/`get` escape C1 controls (e.g. U+009B, a one-character
  CSI), DEL, bidi overrides and U+2028/9 as `\uXXXX`, which keeps the JSON
  exact. `to_fbs()` escapes them in strings too. `flatview schema` also
  neutralises doc comments when writing to a terminal, and leaves redirected
  output untouched.
* **Memory-mapped files.** If another process truncates a file in place
  while it's open, Python crashes with SIGBUS (true of any mmap). Replacing
  files by atomic rename is safe. `open(..., use_mmap=False)` avoids mmap
  entirely.

## Differential testing against flatc

`fuzz/` checks flatview against flatc itself: the latest release (25.12.19),
pinned in `scripts/get_flatc.py`. It needs `g++` as well. The first run
downloads the matching flatbuffers source into `fuzz/.cache/`.

```bash
uv run python fuzz/verifier_parity.py --seconds 120   # mutated buffers vs flatc's generated C++ verifier + JSON
uv run python fuzz/schema_parity.py --seconds 120     # mutated .fbs vs flatc --schema -b
uv run python fuzz/encoder_parity.py --seconds 120    # random schemas + values, encoded both ways
uv run python fuzz/explain.py schema.fbs buf.bin      # where flatc's verifier rejects a buffer
FLATVIEW_ORACLE=1 uv run pytest -m oracle             # short verifier fuzz as a test
```

The encoder fuzzer generates random schemas (enums and bit flags, unions,
structs with arrays, optional scalars, defaults, keyed vectors) and edge-case
values. Every buffer flatview encodes must pass flatc's verifier and print
the same, and every buffer flatc encodes must read back to the same value.
It doesn't yet generate vectors of unions, nested flatbuffers or string
union members. Elements with *equal* keys may come back in a different order
from flatc (FlatBuffers leaves that unspecified), and flatc drops the sign
of a `-0.0` that equals a `0.0` default. The comparison allows for both.

The verifier accepts exactly what flatc's verifier accepts and decodes
identically, except where flatview is deliberately stricter:
- key order, UTF-8 and the `max_refs` budget (each switchable);
- zero offsets in vector elements, which flatc follows to the slot itself;
- deprecated fields, which flatc skips but flatview still lets you read.

The `.fbs` parser accepts and rejects what flatc does. That includes
flatc 25.12's string and empty-vector defaults (`s: string = "x"`,
`v: [int] = []`). As in flatc's reflection data and JSON, those defaults
aren't materialised: an absent field still reads as `None`. The exceptions
are cases where flatc has bugs or writes broken output:
- a struct containing itself;
- `enum E : E`;
- duplicate enum values that flatc's uniqueness check misses;
- structs over 65,535 bytes, whose `.bfbs` offsets flatc silently wraps.

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
