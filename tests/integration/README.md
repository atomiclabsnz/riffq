# Driver integration tests (pyodbc + JDBC)

These tests prove that riffq's PostgreSQL wire protocol and its `pg_catalog` /
`information_schema` emulation work under two client stacks beyond psycopg:

- ODBC: `pyodbc` on unixODBC and the official PostgreSQL ODBC driver
  (`psqlodbc`), both the Unicode and ANSI flavours.
- JDBC: the official pgjdbc driver, driven from small Java harnesses, plus an
  optional second JDBC tool (SQL Workbench/J).

These drivers issue their own catalog SQL on connect and on every metadata call
(`SQLTables`, `SQLColumns`, `DatabaseMetaData.getTables`, `getColumns`,
`getTypeInfo`, `::regclass` casts, and so on). The tests assert on the
driver-level metadata results, not on hand-written SQL, so a regression in the
catalog emulation fails a test even when a plain `SELECT` still works.

## Running

The drivers, driver manager, JDK, and jars are not committed; a setup script
builds and downloads them into a gitignored `.toolchain/` directory. From the
`riffq/` project root:

```
make integration-toolchain      # one-time: build/download the toolchain
make integration-test           # run the suite
```

Add the optional SQL Workbench/J client with:

```
make integration-toolchain ARGS=--with-jdbc-tool
```

`make test` does not run these tests: they live in a non-package subdirectory
that `unittest discover -s tests` does not recurse into, so a bare machine stays
green. `make all-tests` runs `integration-test` last; tests whose toolchain
piece is missing skip themselves with a message naming what to install.

The toolchain build (unixODBC and psqlodbc from source, plus a JDK and jar
downloads) takes a few minutes the first time and is idempotent afterwards. Every
download is pinned to a SHA-256, so a changed upstream artifact fails loudly.

### Running one layer in isolation

With the toolchain built, run a single module (the ODBC runtime environment is
applied automatically via a re-exec in `toolchain.py`):

```
python -m unittest tests.integration.test_pyodbc_catalog
python -m unittest tests.integration.test_jdbc_metadata
```

Run these from the `tests/integration/` directory (so the fixture modules are
importable), or via `make integration-test` which sets the paths up.

## Layout

- `setup_toolchain.sh` -- builds/downloads the toolchain into `.toolchain/`.
- `toolchain.py` -- locators, skip decorators, ODBC runtime environment.
- `fixture_dataset.py` -- the three tables and their expected metadata (single
  source of truth for both driver suites).
- `fixture_server.py` -- the riffq server under test, backed by an in-memory
  DuckDB via teleduck's `DuckdbCatalogSource`, with a JSON-lines query log.
- `server_case.py` -- shared TestCase base that runs one fixture server per class.
- `jdbc.py` -- compiles and runs the Java harnesses.
- `test_fixture_smoke.py` -- validates the fixture with plain psycopg.
- `test_pyodbc_catalog.py`, `test_pyodbc_queries.py` -- ODBC tests.
- `test_jdbc_metadata.py`, `test_jdbc_extended.py` -- JDBC tests.
- `test_jdbc_tool.py` -- optional SQL Workbench/J tier.
- `java/` -- the JDBC harness sources.

## Reading the query log

`fixture_server.py` appends every statement its data path receives to a per-port
JSON-lines file under the system temp directory
(`riffq_integration_<port>.log`), each line recording the SQL and whether it
errored. When a driver test fails, the failure message includes the tail of this
log so you can see the exact statement the driver sent. Note that `pg_catalog` /
`information_schema` queries are answered by riffq's catalog emulation and do not
reach this log; it captures the data path only.

## Known gaps

Each gap below is a real driver-issued query riffq cannot yet answer. They are
recorded as `expectedFailure` tests (each naming its cause) so the suite stays
green while the gap is visible; an `expectedFailure` that starts passing is
reported by unittest as an unexpected success, which is the signal to promote it
back to a plain assertion. These are follow-up work in riffq / pg_catalog, not
test bugs.

The gaps the first versions of this suite found -- for the ANSI driver,
`getTables`, `getImportedKeys` / `getIndexInfo`, `getPrimaryKeys`, `getTypeInfo`,
and boolean/timestamp / ODBC parameter binding -- have since been fixed. Only the
one below remains.

1. ODBC `SQLPrimaryKeys` / `SQLStatistics` error instead of returning empty.
   - Calls: ODBC `SQLPrimaryKeys`, `SQLStatistics` (the JDBC `getPrimaryKeys`
     equivalents are fixed; psqlodbc issues different SQL).
   - Symptoms: `SQLStatistics` selects `i.indisprimary` without grouping it and
     DataFusion rejects the reference as not in `GROUP BY` (PostgreSQL allows it
     by functional dependency on the primary key); `SQLPrimaryKeys` hits a type
     coercion the planner rejects.
   - The correct answer for a backend with no keys or indexes is an empty result
     set. Tests: the `*_are_empty_not_error` cases in `test_pyodbc_catalog`.
