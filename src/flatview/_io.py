"""File handling: gzip detection, mmap, schema discovery."""

from __future__ import annotations

import builtins
import mmap
import os
import zlib
from typing import Any

from ._bfbs import load_bfbs
from ._fbs import parse_fbs
from ._reader import TableView, read
from ._schema import Schema, SchemaError

_GZIP_MAGIC = b"\x1f\x8b"
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

#: Default cap on decompressed size. A FlatBuffer cannot exceed 2 GiB (its
#: offsets are 32-bit and signed where relative), so anything larger is
#: either not a FlatBuffer or a decompression bomb.
MAX_DECOMPRESSED = 1 << 31

PathLike = str | os.PathLike


class DecompressionLimitError(ValueError):
    """Compressed input expands beyond the allowed size."""


def _too_big(path: str, limit: int) -> DecompressionLimitError:
    return DecompressionLimitError(f"{path}: decompressed data exceeds {limit:,} bytes "
                                   "(pass max_size= to allow more)")


def _gunzip(data: bytes, path: str, limit: int) -> bytes:
    out = bytearray()
    while data:
        d = zlib.decompressobj(wbits=31)        # one gzip member
        chunk = d.decompress(data, limit + 1 - len(out))
        out += chunk
        if len(out) > limit or d.unconsumed_tail:
            raise _too_big(path, limit)
        if not d.eof:
            raise OSError(f"{path}: truncated gzip data")
        data = d.unused_data                     # next member, if any
        if data and data[:2] != _GZIP_MAGIC:
            break                                # trailing garbage: ignore, as gzip(1) does
    return bytes(out)


def _unzstd(data: bytes, path: str, limit: int) -> bytes:
    try:
        from compression import zstd  # Python 3.14+
    except ImportError:
        try:
            import zstandard  # type: ignore
        except ImportError:
            raise OSError(f"{path} is zstd-compressed; install 'zstandard' or use Python 3.14+") from None
        with zstandard.ZstdDecompressor().stream_reader(data, read_across_frames=True) as r:
            out = r.read(limit + 1)
        if len(out) > limit:
            raise _too_big(path, limit)
        return out
    out = bytearray()
    while data:
        d = zstd.ZstdDecompressor()              # one frame
        out += d.decompress(data, max_length=limit + 1 - len(out))
        if len(out) > limit:
            raise _too_big(path, limit)
        if not d.eof:
            raise OSError(f"{path}: truncated zstd data")
        data = d.unused_data
    return bytes(out)


def _decompress(data: bytes, path: str, limit: int = MAX_DECOMPRESSED) -> bytes:
    if data[:2] == _GZIP_MAGIC:
        return _gunzip(data, path, limit)
    if data[:4] == _ZSTD_MAGIC:
        return _unzstd(data, path, limit)
    return data


def read_bytes(path: PathLike, max_size: int = MAX_DECOMPRESSED) -> bytes:
    """Read a file, transparently decompressing gzip (or zstd) content.

    Compression is detected by magic number, not extension: the SDE serves
    its .fb/.fbs/.bfbs files as raw gzip bodies under their plain names.
    Decompression stops with DecompressionLimitError beyond ``max_size``.
    """
    with builtins.open(path, "rb") as fh:
        data = fh.read()
    return _decompress(data, os.fspath(path), max_size)


def load_schema(source: PathLike | bytes | Schema, include_paths: list[str] | None = None,
                max_size: int = MAX_DECOMPRESSED) -> Schema:
    """Load a schema from a .fbs or .bfbs file (or raw bytes); gzip is fine.

    The format is detected from content (``BFBS`` identifier), not extension.
    """
    if isinstance(source, Schema):
        return source
    if isinstance(source, (bytes, bytearray, memoryview)):
        data, name = _decompress(bytes(source), "<bytes>", max_size), "<bytes>"
    else:
        name = os.fspath(source)
        data = read_bytes(name, max_size)
    if len(data) >= 8 and data[4:8] == b"BFBS":
        return load_bfbs(data, source=name)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SchemaError(f"{name} is neither a .bfbs binary schema nor UTF-8 .fbs text") from None
    return parse_fbs(text, name, include_paths)


def _stem(path: str) -> str:
    base = path
    for ext in (".gz", ".zst"):
        if base.endswith(ext):
            base = base[: -len(ext)]
    return os.path.splitext(base)[0]


def find_schema(path: PathLike) -> str | None:
    """Locate the schema for a data file: same stem with .bfbs or .fbs."""
    stem = _stem(os.fspath(path))
    for ext in (".bfbs", ".fbs", ".bfbs.gz", ".fbs.gz"):
        cand = stem + ext
        if os.path.isfile(cand):
            return cand
    return None


class FlatFile:
    """An opened FlatBuffer file: ``.root`` is the lazy root table view.

    Uncompressed files are memory-mapped, so opening a multi-hundred-MB file
    is instant and only touched pages are read. Compressed files are
    decompressed into memory. Use as a context manager, or call close().
    """

    def __init__(self, path: PathLike, schema: PathLike | Schema | None = None, *,
                 root: str | None = None, verify: bool = False, use_mmap: bool = True,
                 size_prefixed: bool | None = None, check_identifier: bool = True,
                 max_size: int = MAX_DECOMPRESSED):
        self.path = os.fspath(path)
        if schema is None:
            found = find_schema(self.path)
            if found is None:
                raise FileNotFoundError(
                    f"no schema found for {self.path} (looked for {_stem(self.path)}.bfbs/.fbs); "
                    "pass schema=...")
            schema = found
        self.schema: Schema = load_schema(schema)
        self._mmap: mmap.mmap | None = None
        self._fh = None
        data: Any
        fh = builtins.open(self.path, "rb")
        head = fh.read(4)
        compressed = head[:2] == _GZIP_MAGIC or head == _ZSTD_MAGIC
        if use_mmap and not compressed and os.fstat(fh.fileno()).st_size > 0:
            self._mmap = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
            self._fh = fh
            data = self._mmap
        else:
            fh.seek(0)
            data = _decompress(fh.read(), self.path, max_size)
            fh.close()
        self.data = memoryview(data)
        self.root: TableView = read(self.schema, self.data, root, verify=verify,
                                    size_prefixed=size_prefixed, check_identifier=check_identifier)

    def verify(self, **kw) -> None:
        from ._verify import verify
        verify(self.schema, self.data, self.root._object, **kw)

    def close(self) -> None:
        """Release the mapping. Views still alive keep the memory valid."""
        if self._mmap is not None:
            try:
                self.data.release()
                self._mmap.close()
            except BufferError:
                pass  # outstanding views: mapping is freed when they are
            self._mmap = None
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "FlatFile":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __getattr__(self, name: str) -> Any:
        # Convenience: fb.rows == fb.root.rows
        if name.startswith("_") or name in ("root", "schema", "data", "path"):
            raise AttributeError(name)
        return getattr(self.root, name)

    def __repr__(self) -> str:
        return f"<FlatFile {self.path!r} root={self.root._object.name}>"


def open(path: PathLike, schema: PathLike | Schema | None = None, **kw) -> FlatFile:  # noqa: A001
    """Open a FlatBuffer file. See :class:`FlatFile`."""
    return FlatFile(path, schema, **kw)
