"""Helper utilities for building Arrow results and deriving catalog OIDs.

Provides `to_arrow` for constructing an Arrow C Stream from a simple schema
description and row data (handy for small, programmatic results without
depending on a database engine), and `stable_oid` for deriving reproducible
PostgreSQL object identifiers for lazy-catalog sources.
"""

import pyarrow as pa

# PostgreSQL reserves OIDs below 16384 for its own built-in objects. Derived
# OIDs must stay above this floor so they can never collide with a built-in one.
FIRST_USER_OID = 16384


def stable_oid(salt: str, *parts: str) -> int:
    """Derive a stable, above-the-built-in-range OID from a name.

    The same ``salt`` and ``parts`` always return the same OID, so identifiers
    that must agree across independent catalog scans -- ``pg_class.oid`` and the
    ``pg_attribute.attrelid`` that references it, for instance -- stay consistent
    and catalog joins resolve. Distinct object classes should pass distinct
    salts (for example ``"db"``, ``"ns"``, ``"rel"``) so a database and a table
    that happen to share a name do not land on the same OID. The result is always
    at or above ``FIRST_USER_OID``, keeping it clear of PostgreSQL's built-in
    OID range.

    Args:
        salt: A short tag identifying the object class, mixed into the hash so
            different classes with the same name produce different OIDs.
        parts: The name components that identify the object within its class,
            such as ``(database, schema, relation)`` for a table.

    Returns:
        A deterministic OID in the range ``[FIRST_USER_OID, FIRST_USER_OID + 2e9)``.
    """
    accumulator = 5381
    for character in (salt + "\x00" + "\x00".join(parts)):
        accumulator = (accumulator * 33 + ord(character)) & 0x7FFFFFFF
    return FIRST_USER_OID + (accumulator % 2_000_000_000)

_type = {
    "int": pa.int64(),
    "float": pa.float64(),
    "bool": pa.bool_(),
    "str": pa.utf8(),
    "string": pa.utf8(),
    "date": pa.date32(),
    "datetime": pa.timestamp("us"),
}


def to_arrow(schema_desc:list[dict], rows:list) -> 'pa._ffi.lib.PyCapsule':
    """Build an Arrow C Stream from schema and rows for regular python values

    The schema is a list of dicts like `{ "name": str, "type": str }` where
    `type` is one of: `int`, `float`, `bool`, `str`/`string`, `date`,
    `datetime`. Rows are sequences whose positional items match the schema
    order.

    Example usage:
    >>> callback(to_arrow([{"name": "val", "type": "int"}], [
        [1], 
        [2]
    ]))


    Args:
        schema_desc: Column descriptors in display order.
        rows: Iterable of row sequences aligned to `schema_desc`.

    Returns:
        A PyCapsule containing an Arrow C Stream suitable for returning to the
        server callback.
    """
    arrays = []
    for col_ix, col in enumerate(schema_desc):
        ty = _type[col["type"]]
        arr = pa.array([r[col_ix] for r in rows], type=ty)
        arrays.append(arr)
    batch = pa.RecordBatch.from_arrays(arrays, names=[c["name"] for c in schema_desc])
    reader = pa.RecordBatchReader.from_batches(batch.schema, [batch])
    return reader.__arrow_c_stream__()
