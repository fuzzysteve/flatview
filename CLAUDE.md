# CLAUDE.md

flatview: a pure-Python, schema-driven FlatBuffers library. Schemas (`.fbs` text or `.bfbs`
binary) are loaded at runtime; there is no code generation. It is a general-purpose
library. The EVE Online SDE is its main real-world test and benchmark dataset, not its
scope: keep SDE-specific behaviour out of `src/`.

## Commands (uv only)

```bash
uv sync                                   # dev env (pytest)
uv run pytest -m "not sde_full"           # fast suite, ~1 s; run after every change
uv run pytest                             # + full SDE files (~40 s) if tests/data/sde-full exists
uv run python scripts/fetch_sde_examples.py   # download SDE .fb/.fbs/.bfbs into tests/data/sde-full (gitignored)
uv run --group bench python benchmarks/bench_vs_official.py
uv run --group bench python benchmarks/bench_three_way.py   # also needs the SDE JSONL export (2nd arg)
uv run --group bench python benchmarks/bench_memory.py
```

Cross-checks compare against the latest flatc release, pinned in `scripts/get_flatc.py`
(currently 25.12.19). Run `uv run python scripts/get_flatc.py` once to fetch it into `.tools/`.
Tests and fuzzers use it in preference to whatever `flatc` is on PATH (`FLATVIEW_FLATC`
overrides it), and pytest's header shows which flatc it used. Without any flatc the
cross-check tests are skipped. To move to a newer flatc, bump the pin and re-run the
fuzzers. CI (`.github/workflows/tests.yml`) runs the fast suite with the pinned flatc on Linux
(Python 3.11-3.14 and free-threaded 3.14t), macOS and Windows on every push.
The official `flatbuffers` package lives only in the `bench` dependency group. The
library itself has no runtime dependencies; keep it that way.

## Layout

- `_schema.py`: the schema model (`Schema`, `Object`, `Field`, `Type`, `Enum`). It mirrors
  `reflection.fbs` on purpose, so both schema sources produce identical objects.
- `_fbs.py`: `.fbs` parser, resolver (namespaces, union `_type` fields, ids, struct
  layout) and `render()` back to `.fbs`.
- `_bfbs.py`: `.bfbs` loader. It is bootstrapped: the bundled `reflection.fbs` is parsed
  with `_fbs.py`, then `.bfbs` files are read with `_reader.py`.
- `_reader.py`: lazy views. There is one generated class per table/struct, with one
  property per field, plus `Vector` subclasses (key lookup is on `TableVector`).
- `_todict.py`: whole-tree conversion. It generates source for one converter function per
  table and mode and `exec`s it; `_asdict()` and `to_python()` go through it.
- `_verify.py`: the verifier. `_builder.py`: the back-to-front builder and the
  schema-driven encoder. `_io.py`: files, gzip/zstd, mmap, schema discovery.
  `__main__.py`: the CLI.

## Invariants (tests enforce these; keep them true)

- The `.fbs` parser must produce exactly what flatc writes to `.bfbs`. This is checked
  for all 104 SDE schemas and the kitchen-sink schema, with and without
  `--bfbs-builtins`, via `tests/conftest.py::normalise`. Reflection semantics we copy
  from flatc: non-scalar table fields are `optional` unless required; string `key`
  fields are implicitly `required`; union value id N has its `_type` at id N-1; bit_flags
  values are stored as masks; built-in attributes are left out of `.bfbs` unless
  `--bfbs-builtins` is given.
- Decoded data must match `flatc --json` (`_asdict(defaults=False, enums="name")`).
  `key` fields are always emitted; bit flags use flatc's space-separated names.
- The compiled converters (`_todict.py`) and the view properties (`_reader.py`) are two
  implementations of the same decoding. Any change to field semantics goes in both.
  `test_asdict_same_for_bytes_and_memoryview` covers both string-decode paths.
- The encoder sorts vectors of keyed tables (bytewise for strings). Error paths use the
  caller's indices, not post-sort positions. Vtables are de-duplicated on their full
  bytes, size included (see google/flatbuffers#9413).
- The verifier's defaults match flatc's C++ verifier (fields are bounded by the buffer).
  `strict_table_size=True` is the stricter flatcc-style check. Alignment is relative to
  the start of the whole buffer, so size-prefixed and nested buffers carry a `base`.
- Lazily built per-schema state must be thread-safe. View classes are built under
  `_BUILD_LOCK` and published on the `Object` only when complete (`_BUILDING` holds the
  in-progress class for the building thread). Python enums are built once under
  `_ENUM_LOCK`. `tests/test_threads.py` checks this, and the suite passes on free-threaded
  Python (`uv run --isolated --no-project --python 3.14t --with pytest --with-editable .
  python -m pytest`).
- View helper methods are `_`-prefixed (namedtuple style) so they never shadow schema
  fields. Fields named `__x...` never become attributes (they would replace Python
  protocol methods); they are reachable as `view["__x"]`.

## Security rules (tests in `tests/test_security.py`)

- Treat `.bfbs` files and buffers as untrusted. **Never interpolate schema-derived
  text into generated source** in `_todict.py`. Use indexes, `repr()`, or `self.const()`
  values only; integers come from struct unpacking.
- `load_bfbs` must keep verifying the buffer against `reflection.fbs`, rejecting
  non-identifier names, and running `validate()` (layout sanity) before returning.
- Every new kind of offset the verifier follows must go through `deref()`, so it counts
  against `max_refs` (amplification protection).
- All decompression goes through `_io._decompress` with a size cap (`max_size`, default
  2 GiB).
- `.fbs` includes resolve only inside `ctx.include_roots` (the main schema's directory
  plus `include_paths`), checked on the real path. Never add a cwd fallback or follow a
  path outside the roots. Includes are read only via `_read_confined()`: it opens
  non-blocking, requires a regular file, then re-checks what was actually opened (the
  fd's own path, or a device/inode match), so a swap between check and open is caught.
  An include path must first pass the lexical gate (`_lexically_inside`: written inside a
  root, case-insensitive, platform path rules) before any file-system call, so UNC shares
  and automount paths are never touched. Containment is by identity (`_Roots.contains`:
  device/inode of the path or a parent), never by comparing path strings.
- `render()` must validate every name as an identifier and emit strings only via
  `_quote()`. Doc comments are split per line, because rendered text gets parsed again.
- Never auto-detect size prefixes. A valid buffer's root offset can equal `len - 4`
  (e.g. a root table with no fields set). Reader and verifier must agree on every
  interpretation of a buffer, nested ones included.
- Nothing driven by schema depth may recurse in Python. Struct layout, the cycle check
  and converter generation use explicit work stacks.
- Costs driven by schema *width* must stay linear. vtable-reader caches in `_todict.py`
  are lazy (`_Unpackers`, `_ZeroPads`), and tables over `WIDE_TABLE` fields use
  `_generic_table` instead of compiled code. Every number in generated source goes
  through `_int()`.
- Every file read is capped (`_read_capped`); only regular files are mmapped.
- Reader and verifier must agree on edge cases. For example, empty nested flatbuffers
  are rejected by the verifier and refused by the encoder, as flatc does.

## Differential testing (fuzz/)

`fuzz/verifier_parity.py`, `fuzz/schema_parity.py` and `fuzz/encoder_parity.py` compare
against flatc's own C++, parser and encoder (README, "Differential testing"). Any "ours-accepts-flatc-rejects" or decode
difference is a bug. Pin each fixed case in `tests/test_flatc_parity.py` (verifier) or
the `CORPUS` list (schemas), and keep the README's list of intentional differences
current.

## Testing conventions

- Cross-check new features against flatc in both directions. Encode with flatview and
  decode with flatc, then encode with flatc and read with flatview (helpers in
  `conftest.py`). Use the original schema in `tests/data/kitchen/`, not the upstream
  Monster sample.
- flatc's JSON prints doubles to 12 decimal places, so compare floats with
  `close_enough`. It needs `--json-nested-bytes` for nested flatbuffers and
  `--bfbs-comments` to keep doc comments.
- Expected SDE values in tests come from real SDE data of the same build, never from
  memory. Check them against the data before writing them.
- `examples/quickstart.py` is the README quick start. `tests/test_examples.py` runs it
  and compares each `print` with its trailing comment, so update both together.

## Performance work

Measure, don't assert. Benchmarks run each scenario in a fresh process. Report heap
(tracemalloc) separately from RSS, because mmapped pages count toward RSS but are
reclaimable page cache. Record new numbers in the README tables only after running the
benchmark. Per-call FFI (ctypes/cffi) is slower than `struct.unpack_from` here, so don't
go that way.

## Not supported (deliberately, for now)

64-bit offsets/vectors (`offset64`/`vector64`), FlexBuffers, and JSON data blocks inside
`.fbs` files.
