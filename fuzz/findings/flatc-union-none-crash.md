# flatc JSON printer crashes on a verified buffer: union value with type NONE

**Component:** `flatbuffers` C++ `GenText` (the JSON printer behind `flatc --json`
and `GenerateText`)
**Version:** flatc 25.2.10 (reproduced with the released binary, not a debug build)
**Impact:** null-pointer dereference → segfault (`SIGSEGV`). The buffer passes
`Verifier` first, so "verify, then print" does not protect against it.
**Found by:** differential fuzzing flatview against flatc (`fuzz/verifier_parity.py`).

## Upstream status

Already reported as [google/flatbuffers#9033](https://github.com/google/flatbuffers/issues/9033)
(April 2026, reproduced on 25.12.19), which the stale bot auto-closed as "not
planned" with no maintainer response. A fix is open as
[#9308](https://github.com/google/flatbuffers/pull/9308) (unreviewed as of
November 2026). As of November 2026, `master` still has the unguarded
dereference. What this writeup adds: flatc's generated verifier *accepts* the
crashing buffer. Our comment on #9308 is in `pr-9308-comment.md`.

## Summary

A table can carry a union *value* field while its paired union *type* field is
absent (type `NONE`). `Verifier` accepts such a buffer: a NONE union is a valid,
well-formed "no value" state. But the JSON printer assumes that whenever it
reaches a union value field, the field it printed immediately before was that
union's type byte. When the type field is absent — and the union value is the
first field actually emitted — that assumption is `nullptr`, and the printer
dereferences it.

The verifier and the printer disagree about the same bytes: one says "valid,
union is NONE", the other says "there must be a type byte right before this".

## Minimal reproduction

```fbs
// u.fbs
table Inner { v: int; }
union U { Inner }
table Root { u: U; }
root_type Root;
```

Build a `Root` whose union **value** field (`u`, field id 1) is present but whose
union **type** field (`u_type`, field id 0) is omitted. 36 bytes:

```
0c00 0000 0800 0800 0000 0400 0800 0000   root offset; Root vtable {u_type absent, u present}
0c00 0000 0000 0600 0800 0400 0600 0000   Root table; Inner vtable
2a00 0000                                 Inner.v = 42
```

(The bytes above are in `none_value.bin` next to this file, produced by
`fuzz/verifier_parity.py`'s builder.)

```console
$ flatc --json --strict-json --raw-binary u.fbs -- none_value.bin
Segmentation fault (core dumped)      # exit 139
```

The same buffer verifies cleanly: `VerifyRootBuffer(verifier)` returns `true`,
and reflection-based readers treat `u` as absent/NONE.

## Root cause

`src/idl_gen_text.cpp`. The printer walks a table's fields and, after printing
each present field, records that field's address in `prev_val` so the *next*
field can use it — this is how a union value reads its type byte:

```cpp
// GenStruct(), ~line 355
// Track prev val for use with union types.
prev_val = table->GetAddressOf(fd.value.offset);
```

`prev_val` is updated only for fields that are actually printed
(`is_present || output_anyway`), and it starts as `nullptr`. When the field is a
union value:

```cpp
// PrintOffset(), ~line 182
case BASE_TYPE_UNION: {
  // If this assert hits, you have an corrupt buffer, a union type field
  // was not present or was out of range.
  FLATBUFFERS_ASSERT(prev_val);
  auto union_type_byte = *prev_val;   // line 186
  ...
```

Two separate problems meet here:

1. **The invariant is wrong.** `prev_val` is "the previously *printed* field",
   which is only the union's type field when that type field was present and
   immediately precedes the value in output order. A NONE union legitimately has
   no type field, so the printer can reach a union value with `prev_val` pointing
   at an unrelated field, or at `nullptr` when the value is the first field
   emitted.
2. **The guard is compiled out in release.** `FLATBUFFERS_ASSERT` is `assert`,
   which `NDEBUG` disables. So the comment's "corrupt buffer" case isn't caught:
   line 186 dereferences `nullptr` and the process crashes. (A debug build aborts
   on the assert at line 185 instead — that's the only difference.)

When the type field is absent but *some* earlier field was printed, there is no
crash but `*prev_val` reads an unrelated byte as the union type; `ReverseLookup`
usually returns the handled `"unknown enum value"`, so the specific null-deref
needs the union value to be the first emitted field.

Note the verifier does the right thing independently: a union value whose type is
NONE is simply not followed (see `Verify<Table>` / the generated
`Verify<Name>Vector`), which is why `Verify` passes.

## Why it matters

Anything that runs `GenText` / `flatc --json` on data from an untrusted source —
a debug endpoint, a "dump this buffer" tool, a log formatter — can be crashed by
a 36-byte input, and the usual advice ("verify untrusted buffers before using
them") does not help, because the verifier accepts it. It is a denial-of-service,
not memory disclosure: the read is of `*nullptr`, so it faults rather than
leaking.

## Suggested fix

In `PrintOffset`'s `BASE_TYPE_UNION` case, treat a missing/NONE type as the
printer already treats an unknown one — emit `null` (or omit the field) instead
of asserting:

```cpp
case BASE_TYPE_UNION: {
  if (!prev_val) { text += "null"; return nullptr; }   // type field absent => NONE
  auto union_type_byte = *prev_val;
  ...
```

and, more robustly, locate the union's type field by its field id rather than
relying on output order, so a NONE type is never mistaken for a neighbouring
field's bytes. The `FLATBUFFERS_ASSERT` should not be the only thing standing
between a well-formed buffer and a null dereference in release builds.

## How flatview handles the same buffer

flatview's verifier accepts it (matching flatc's verifier), and its reader and
JSON-equivalent (`_asdict`) report the union as `None`, never following the
value when the type is NONE. Regression coverage:
`tests/test_flatc_parity.py::test_none_union_type_with_value_is_ignored`.
