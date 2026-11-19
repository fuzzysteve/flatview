"""flatview - schema-driven FlatBuffers for Python.

    >>> import flatview
    >>> fb = flatview.open("types.fb")            # finds types.bfbs/.fbs; gzip ok
    >>> rifter = fb.root.rows.lookup(587)          # binary search on the key field
    >>> rifter.name.en, rifter.groupID, rifter.basePrice
    ('Rifter', 25, None)

No flatc-generated code is needed: schemas are loaded at runtime from .fbs
text or .bfbs binary schemas.
"""

from ._schema import BaseType, Enum, EnumVal, Field, Object, Schema, SchemaError, Type
from ._fbs import parse_fbs
from ._bfbs import load_bfbs, reflection_schema
from ._reader import (
    FlatBufferError, ScalarVector, StringVector, StructVector, StructView, TableVector,
    TableView, UnionVector, Vector, has_identifier, read, to_python,
)
from ._verify import VerificationError, verify
from ._builder import EncodeError, encode
from ._io import FlatFile, load_schema, open, read_bytes

__all__ = [
    "BaseType", "Enum", "EnumVal", "Field", "Object", "Schema", "SchemaError", "Type",
    "parse_fbs", "load_bfbs", "reflection_schema", "load_schema",
    "FlatBufferError", "TableView", "StructView", "Vector", "ScalarVector", "StringVector",
    "StructVector", "TableVector", "UnionVector", "has_identifier", "read", "to_python",
    "VerificationError", "verify", "EncodeError", "encode",
    "FlatFile", "open", "read_bytes",
]

__version__ = "0.1.0"
