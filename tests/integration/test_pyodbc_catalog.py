"""ODBC catalog-metadata tests over both psqlodbc driver flavours.

pyodbc's ``tables`` / ``columns`` / ``getTypeInfo`` / ``primaryKeys`` /
``statistics`` calls make the driver issue its own catalog SQL (SQLTables,
SQLColumns, and so on) against the server -- the surface a psycopg-only suite
never touches. Each metadata assertion runs once per driver flavour (Unicode and
ANSI) via ``subTest`` because the two issue slightly different catalog SQL.

The connections are opened with ``connect_odbc``, which configures pyodbc for
narrow-char decoding so the ANSI driver (which cannot produce wide characters)
reads text results correctly.

primaryKeys and statistics remain ``expectedFailure``: riffq has no
pg_index / pg_am-backed introspection, so those driver queries error instead of
returning empty. That is tracked as pg_catalog follow-up work in
tests/integration/README.md.
"""
import unittest

import fixture_dataset
from server_case import FixtureServerCase
from toolchain import ODBC_DRIVERS, connect_odbc, require_odbc

try:
    import pyodbc
except ImportError:
    pyodbc = None


@require_odbc()
class PyodbcCatalogTest(FixtureServerCase):
    """Driver-issued catalog metadata matches the fixture, for both flavours."""

    PORT = 55541

    def test_tables_lists_the_three_fixture_tables(self):
        """SQLTables returns exactly the three tables in public, typed TABLE."""
        for driver in ODBC_DRIVERS:
            with self.subTest(driver=driver):
                conn = connect_odbc(self.PORT, driver=driver)
                try:
                    rows = conn.cursor().tables(schema=fixture_dataset.SCHEMA_NAME).fetchall()
                    fixture_rows = [
                        r for r in rows if r.table_name in fixture_dataset.TABLE_NAMES
                    ]
                    self.assertEqual(
                        sorted(r.table_name for r in fixture_rows),
                        sorted(fixture_dataset.TABLE_NAMES),
                    )
                    for row in fixture_rows:
                        self.assertEqual(row.table_type, "TABLE", row.table_name)
                finally:
                    conn.close()
                self.assertEqual(self.errored_statements(), [], self.log_tail())

    def test_columns_reports_names_order_and_nullability(self):
        """SQLColumns returns customers' columns in order with correct nullability."""
        expected = fixture_dataset.TABLE_COLUMNS["customers"]
        for driver in ODBC_DRIVERS:
            with self.subTest(driver=driver):
                conn = connect_odbc(self.PORT, driver=driver)
                try:
                    rows = conn.cursor().columns(
                        table="customers", schema=fixture_dataset.SCHEMA_NAME
                    ).fetchall()
                    self.assertEqual(
                        [row.column_name for row in rows],
                        [column.name for column in expected],
                    )
                    # ODBC nullable codes: 1 = SQL_NULLABLE, 0 = SQL_NO_NULLS.
                    self.assertEqual(
                        [bool(row.nullable) for row in rows],
                        [column.nullable for column in expected],
                    )
                finally:
                    conn.close()

    def test_get_type_info_returns_rows(self):
        """SQLGetTypeInfo returns a non-empty type catalogue."""
        for driver in ODBC_DRIVERS:
            with self.subTest(driver=driver):
                conn = connect_odbc(self.PORT, driver=driver)
                try:
                    rows = conn.cursor().getTypeInfo().fetchall()
                    self.assertGreater(len(rows), 0)
                finally:
                    conn.close()

    def test_connection_dbms_info(self):
        """Both drivers report a PostgreSQL DBMS name and a version string."""
        for driver in ODBC_DRIVERS:
            with self.subTest(driver=driver):
                conn = connect_odbc(self.PORT, driver=driver)
                try:
                    self.assertIn("PostgreSQL", conn.getinfo(pyodbc.SQL_DBMS_NAME))
                    self.assertTrue(conn.getinfo(pyodbc.SQL_DBMS_VER))
                finally:
                    conn.close()

    @unittest.expectedFailure
    def test_primary_keys_are_empty_not_error(self):
        """KNOWN GAP (deferred): SQLPrimaryKeys errors instead of returning empty.

        psqlodbc's SQLPrimaryKeys uses a different (information_schema
        key-column-usage) query than JDBC getPrimaryKeys -- the JDBC path is
        fixed, but the ODBC query fails on a separate pg_catalog gap (a type
        coercion the planner rejects). The correct result for a backend with no
        constraints is an empty set; a driver that errors here breaks table
        introspection.
        """
        conn = connect_odbc(self.PORT)
        try:
            rows = conn.cursor().primaryKeys(
                "customers", schema=fixture_dataset.SCHEMA_NAME
            ).fetchall()
            self.assertEqual(rows, [])
        finally:
            conn.close()

    @unittest.expectedFailure
    def test_statistics_are_empty_not_error(self):
        """KNOWN GAP (deferred): SQLStatistics errors instead of returning empty.

        psqlodbc's SQLStatistics query selects i.indisprimary without grouping
        it; DataFusion rejects the reference as not in GROUP BY (Postgres allows
        it via functional dependency). A separate pg_catalog gap from the JDBC
        _pg_expandarray one.
        """
        conn = connect_odbc(self.PORT)
        try:
            rows = conn.cursor().statistics(
                "customers", schema=fixture_dataset.SCHEMA_NAME
            ).fetchall()
            self.assertEqual(rows, [])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
