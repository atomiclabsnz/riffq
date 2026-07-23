"""Unit tests for riffq.helpers.stable_oid.

stable_oid derives the OIDs a lazy catalog source hands back for databases,
schemas, relations, and their row types. Those OIDs must be deterministic (so
pg_class.oid and the pg_attribute.attrelid that references it agree across
independent scans), class-separated (so a database and a table of the same name
do not collide), and above PostgreSQL's built-in OID floor. These tests pin all
three properties.
"""
import unittest

from riffq.helpers import FIRST_USER_OID, stable_oid


class StableOidTest(unittest.TestCase):
    """Determinism, class separation, and the built-in OID floor."""

    def test_same_inputs_yield_same_oid(self):
        """Repeated calls with identical arguments return the identical OID, so
        an OID computed during one scan matches the one computed during another."""
        first = stable_oid("rel", "appdb", "public", "customers")
        second = stable_oid("rel", "appdb", "public", "customers")
        self.assertEqual(first, second)

    def test_different_salts_separate_object_classes(self):
        """The same name under different class salts maps to different OIDs, so a
        database, schema, and relation that share a name do not collide."""
        as_database = stable_oid("db", "orders")
        as_relation = stable_oid("rel", "orders")
        self.assertNotEqual(as_database, as_relation)

    def test_different_names_yield_different_oids(self):
        """Distinct relations under one salt map to distinct OIDs."""
        customers = stable_oid("rel", "appdb", "public", "customers")
        orders = stable_oid("rel", "appdb", "public", "orders")
        self.assertNotEqual(customers, orders)

    def test_result_stays_above_builtin_floor(self):
        """Every derived OID is at or above FIRST_USER_OID (16384), keeping it
        clear of PostgreSQL's reserved built-in OID range."""
        for parts in [("db", "a"), ("rel", "a", "b", "c"), ("type", "x")]:
            self.assertGreaterEqual(stable_oid(*parts), FIRST_USER_OID)


if __name__ == "__main__":
    unittest.main()
