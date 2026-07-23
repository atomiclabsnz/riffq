"""JDBC catalog-metadata tests, asserting on the Java harness's JSON dump.

RiffqMetadataHarness opens one pgjdbc connection and prints the results of the
DatabaseMetaData calls a JDBC application relies on. This module runs it once and
asserts the working results against fixture_dataset, and records the gaps the
driver surfaced as expectedFailure tests, each naming its cause. The gaps are
also listed in tests/integration/README.md as follow-up work in riffq /
pg_catalog. An expectedFailure that starts passing is reported as an unexpected
success, so each turns back into a plain assertion once the gap is fixed.

Gaps found (all are pgjdbc-issued catalog queries riffq's emulation cannot yet
answer):

- getTables errors (an optimizer rule fails casting 'pg_class' to Int32 in the
  driver's table query), so no table list comes back.
- getPrimaryKeys / getImportedKeys / getIndexInfo error instead of returning
  empty; riffq has no index/constraint introspection and pg_get_indexdef lacks
  the multi-argument overload the driver calls.
- getTypeInfo errors: its pg_catalog query uses a correlated scalar subquery
  DataFusion rejects.
"""
import unittest

import fixture_dataset
from jdbc import run_harness
from server_case import FixtureServerCase
from toolchain import require_jdbc


@require_jdbc()
class JdbcMetadataTest(FixtureServerCase):
    """DatabaseMetaData results from pgjdbc, working assertions and known gaps."""

    PORT = 55551

    @classmethod
    def setUpClass(cls):
        """Start the fixture server and run the metadata harness once."""
        super().setUpClass()
        cls.meta = run_harness("RiffqMetadataHarness", cls.PORT)

    def test_product_and_driver_identity(self):
        """The driver reports PostgreSQL, the pinned server version, and pgjdbc."""
        self.assertEqual(self.meta["productName"], "PostgreSQL")
        self.assertEqual(self.meta["productVersion"], "17.0")
        self.assertTrue(self.meta["driverVersion"])

    def test_catalogs_include_the_database(self):
        """getCatalogs lists the fixture database."""
        self.assertIn(fixture_dataset.DATABASE_NAME, self.meta["catalogs"])

    def test_schemas_include_public(self):
        """getSchemas lists the fixture schema."""
        self.assertIn(fixture_dataset.SCHEMA_NAME, self.meta["schemas"])

    def test_columns_match_dataset(self):
        """getColumns matches the dataset's names, order, JDBC types, nullability."""
        for table, columns in fixture_dataset.TABLE_COLUMNS.items():
            reported = self.meta["columns"][table]
            self.assertEqual(
                [c["name"] for c in reported], [c.name for c in columns], table
            )
            self.assertEqual(
                [c["ordinal"] for c in reported],
                list(range(1, len(columns) + 1)),
                table,
            )
            self.assertEqual(
                [c["dataType"] for c in reported],
                [c.jdbc_type for c in columns],
                table,
            )
            self.assertEqual(
                [c["nullable"] for c in reported],
                [c.jdbc_nullable for c in columns],
                table,
            )

    def test_row_data_matches(self):
        """SELECT per table returns the fixed rows, including NULLs and booleans."""
        customers = self.meta["rows"]["customers"]
        self.assertEqual(len(customers), 3)
        self.assertEqual([row[0] for row in customers], ["1", "2", "3"])
        self.assertEqual([row[1] for row in customers], ["Ada", "Bo", "Cy"])
        self.assertIsNone(customers[1][2])  # Bo's email is NULL

        products = self.meta["rows"]["products"]
        self.assertEqual([row[3] for row in products], ["true", "false", "true"])
        self.assertIsNone(products[2][2])  # SKU-3 price is NULL

    def test_tables_list_is_returned(self):
        """getTables lists exactly the three fixture tables.

        pgjdbc's table query casts the literal 'pg_class' to regclass; pg_catalog
        resolves that to the relation's OID so the comparison against an oid
        column plans and executes.
        """
        names = sorted(table["name"] for table in self.meta["tables"])
        self.assertEqual(names, sorted(fixture_dataset.TABLE_NAMES))

    @unittest.expectedFailure
    def test_primary_keys_are_empty_not_error(self):
        """KNOWN GAP (deferred): getPrimaryKeys errors instead of returning empty.

        Its query accesses an inline (_pg_expandarray(i.indkey)).n set-returning
        function field; pg_catalog's SRF-to-unnest rewrite handles the bare
        aliased form but not this inline field access in the same SELECT, so the
        access reaches DataFusion on a List(Struct) value and errors. Deferred:
        the fix is in a delicate multi-pass rewrite. The harness records -1 for a
        failed call and 0 for an empty one.
        """
        self.assertEqual(
            [self.meta["primaryKeys"][table] for table in fixture_dataset.TABLE_NAMES],
            [0, 0, 0],
        )

    def test_imported_keys_are_empty_not_error(self):
        """getImportedKeys returns empty (the fixture has no foreign keys).

        Its query uses pg_catalog.generate_series as a table function, which
        pg_catalog now strips to the registered unqualified name.
        """
        self.assertEqual(
            [self.meta["importedKeys"][table] for table in fixture_dataset.TABLE_NAMES],
            [0, 0, 0],
        )

    def test_index_info_is_empty_not_error(self):
        """getIndexInfo returns empty (the fixture has no indexes).

        Its query calls the three-argument pg_get_indexdef(oid, column, pretty),
        for which pg_catalog now has a matching signature.
        """
        self.assertEqual(
            [self.meta["indexInfo"][table] for table in fixture_dataset.TABLE_NAMES],
            [0, 0, 0],
        )

    @unittest.expectedFailure
    def test_type_info_is_non_empty(self):
        """KNOWN GAP: getTypeInfo errors on a correlated scalar subquery
        DataFusion rejects; should return the server's type catalogue."""
        self.assertGreater(len(self.meta["typeInfo"]), 0)


if __name__ == "__main__":
    unittest.main()
