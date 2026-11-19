Adding a data point in support of this PR, since #9033 was auto-closed as stale without a fix and `master` still has the bare `FLATBUFFERS_ASSERT(prev_val)`.

**The buffer that crashes the printer passes flatc's own verifier.** #9033 describes the input as malformed and notes that `GenText()` doesn't call the `Verifier`. But a union value whose type field is absent is a valid NONE union as far as the verifier is concerned, so "verify untrusted buffers before printing them" doesn't help here. The generated `Verify*Buffer` returns `true`, and `GenText` then dereferences null.

Minimal reproduction, 36 bytes:

```fbs
// u.fbs
table Inner { v: int; }
union U { Inner }
table Root { u: U; }
root_type Root;
```

```sh
echo 0c0000000800080000000400080000000c0000000000060008000400060000002a000000 | xxd -r -p > none_value.bin
flatc --json --strict-json --raw-binary u.fbs -- none_value.bin   # Segmentation fault, exit 139
```

`Root` has `u` (field id 1) set to an `Inner` and `u_type` (field id 0) absent. With `flatc --cpp u.fbs` and a release build (`-DNDEBUG`), `VerifyRootBuffer()` returns `true` for these bytes. I reproduced the segfault with the released flatc 25.2.10. The code path is unchanged on `master`.

One question about the approach. With this PR, the buffer above makes `GenText` return `"union type field not present"`. Since the verifier treats it as a valid buffer whose union is NONE, would it be more consistent for the printer to do the same and omit `u` (or print it as absent), rather than fail? Either way, an error is far better than a crash, and I'd be glad to see this merged as is.

(Found by differential fuzzing of an independent FlatBuffers reader against flatc's generated verifier and JSON printer.)
