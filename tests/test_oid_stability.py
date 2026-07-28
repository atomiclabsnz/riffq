"""An object's OID does not depend on which client connected first.

Hosts that supply their own OIDs (a lazy catalog source) keep them verbatim.
Hosts that do not get an auto-increment counter, and that counter belongs to the
database's own context: each database numbers its objects from the same floor,
so two databases' first tables may share a number, exactly as PostgreSQL's OIDs
are unique only within a database.

What that has to guarantee is that the numbering depends ONLY on the host's own
registration order -- not on the order clients happen to connect in, and not on
anything left over from another database. Contexts are built lazily on first
connect, so before the counter was scoped to the context it was a process-global
static: whichever database a client reached first consumed the low numbers, and
every OID moved when the server restarted with a different connect order.

These tests drive the whole scenario over the wire: register two databases,
connect in one order, restart the server, connect in the OPPOSITE order, and
require every OID to be unchanged.
"""
import multiprocessing
import socket
import time
import unittest

import psycopg
from helpers import stop_server
from riffq.helpers import FIRST_USER_OID, stable_oid

# Two databases, each with its own schema and table, so nothing observed can be
# ambiguous about which side it came from.
DATABASES = [
    ("db1", "s1", "t1"),
    ("db2", "s2", "t2"),
]

EAGER_PORT = 55487
LAZY_PORT = 55488

# Every OID a client can see for these objects, fetched by name so the query
# itself never depends on an OID.
#
# The pg_type and pg_attribute columns are reached by JOINING rather than read
# from pg_class, so the query asserts that the references RESOLVE. Reading
# c.reltype alone would still pass if pg_type's own row had drifted away from
# it, leaving every catalog join broken but the probe green.
OID_PROBE = """
SELECT n.nspname, c.relname, n.oid, c.oid, c.reltype, t.oid, t.typrelid, a.attrelid
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_type t ON t.oid = c.reltype
JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
ORDER BY n.nspname, c.relname, a.attnum
"""

DATABASE_OID_PROBE = (
    "SELECT datname, oid FROM pg_catalog.pg_database "
    "WHERE datname IN ('db1', 'db2') ORDER BY datname"
)


def _wait_for_port(port, timeout=30):
    """Block until something is listening on `port`, or raise."""
    start = time.time()
    while time.time() - start < timeout:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"server never bound port {port}")


class LazyTwoDatabaseSource:
    """A lazy source describing the same two databases as the eager registrations.

    It supplies its own OIDs, which is the first rule: a host that knows its own
    identifiers keeps them. `riffq.helpers.stable_oid` is the helper offered for
    deriving them from names, and teleduck uses it too. These pass through riffq
    untouched, so what the test checks here is that they arrive unchanged -- not
    that they match anything the eager path would have chosen for itself.
    """

    def databases(self, callback):
        """Report both databases."""
        callback([{"oid": stable_oid("db", name), "name": name} for name, _, _ in DATABASES])

    def schemas(self, database, callback):
        """Report the one schema belonging to `database`."""
        callback(
            [
                {"oid": stable_oid("ns", database, schema), "name": schema}
                for name, schema, _ in DATABASES
                if name == database
            ]
        )

    def relations(self, database, schema, callback):
        """Report the one table in `database`.`schema`."""
        callback(
            [
                {
                    "oid": stable_oid("rel", database, schema, table),
                    "reltype_oid": stable_oid("type", database, schema, table),
                    "name": table,
                    "kind": "table",
                    "has_index": False,
                }
                for name, registered_schema, table in DATABASES
                if name == database and registered_schema == schema
            ]
        )

    def columns(self, database, schema, relation, callback):
        """Report the single id column every fixture table has."""
        callback([{"name": "id", "type_oid": 23, "nullable": False}])


def _run_server(port, lazy):
    """Serve the two databases through the chosen registration path."""
    import riffq
    from riffq.helpers import to_arrow

    def handle_query(sql, callback, **kwargs):
        callback(to_arrow([{"name": "val", "type": "int"}], [[1]]))

    server = riffq.Server(f"127.0.0.1:{port}")
    if lazy:
        server.set_lazy_catalog(LazyTwoDatabaseSource())
    else:
        for database, schema, table in DATABASES:
            server.register_database(database)
            server.register_schema(database, schema)
            server.register_table(
                database, schema, table, [{"id": {"type": "int", "nullable": False}}]
            )
    server.on_query(handle_query)
    server.start(catalog_emulation=True)


def _start_server(port, lazy):
    """Launch a server in a FRESH process and wait for it to bind.

    "spawn", not the default "fork": a forked child inherits the parent's already
    imported native module and any state initialised in it, so two forks of one
    parent are not two independent server runs. The bug this file guards against
    lived in a process-global static, and a fork-based "restart" would inherit
    that static's starting state rather than re-creating it -- which is exactly
    the thing under test. It also avoids forking a multi-threaded parent.
    """
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_run_server, args=(port, lazy), daemon=True)
    process.start()
    try:
        _wait_for_port(port, timeout=120)
    except RuntimeError:
        stop_server(process)
        raise
    return process


def _collect_oids(port, connect_order):
    """Connect to each database in `connect_order` and gather every visible OID.

    The connect order is the input under test: it decides the order contexts are
    built in, which is exactly what a counter-derived OID depended on.
    """
    observed = {}
    for database in connect_order:
        with psycopg.connect(
            host="127.0.0.1", port=port, user="user", dbname=database
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(OID_PROBE)
                observed[f"{database}:relations"] = cur.fetchall()
                cur.execute(DATABASE_OID_PROBE)
                observed[f"{database}:databases"] = cur.fetchall()
    return observed


class OidStabilityAcrossRestartTest(unittest.TestCase):
    """The scenario in full: same objects, two server runs, opposite connect
    orders, identical OIDs."""

    def test_oids_survive_a_restart_with_the_connect_order_reversed(self):
        forwards = ["db1", "db2"]
        backwards = ["db2", "db1"]

        process = _start_server(EAGER_PORT, lazy=False)
        try:
            first_run = _collect_oids(EAGER_PORT, forwards)
        finally:
            stop_server(process)

        # A genuinely new process: the counter this replaced was a process-global
        # static, so nothing short of a restart could expose its drift.
        process = _start_server(EAGER_PORT, lazy=False)
        try:
            second_run = _collect_oids(EAGER_PORT, backwards)
        finally:
            stop_server(process)

        self.assertEqual(
            first_run,
            second_run,
            "every OID must be identical across a restart, whatever order "
            "clients connect in",
        )

    def test_each_database_keeps_its_own_objects_and_oids(self):
        process = _start_server(EAGER_PORT, lazy=False)
        try:
            observed = _collect_oids(EAGER_PORT, ["db1", "db2"])
        finally:
            stop_server(process)

        db1 = observed["db1:relations"]
        db2 = observed["db2:relations"]
        self.assertEqual([(row[0], row[1]) for row in db1], [("s1", "t1")])
        self.assertEqual([(row[0], row[1]) for row in db2], [("s2", "t2")])

        # The counter belongs to each database's own context, so the two
        # databases number their objects from the same floor and their first
        # schema and first table land on the SAME numbers. That is correct, not a
        # collision: PostgreSQL OIDs are unique within a database, and these two
        # catalogs are never joined to each other. What matters is that each
        # connection sees only its own objects, asserted above.
        self.assertEqual(db1[0][2], db2[0][2], "each database numbers from the same floor")
        self.assertEqual(db1[0][3], db2[0][3])

        # Within one database the OIDs must still be distinct from each other -
        # that is what the joins depend on.
        self.assertNotEqual(db1[0][2], db1[0][3], "schema and relation must differ")
        self.assertNotEqual(db1[0][3], db1[0][4], "relation and rowtype must differ")

        # And both connections agree on what the databases themselves are, since
        # pg_database is the one catalog table that is not per-database.
        self.assertEqual(observed["db1:databases"], observed["db2:databases"])

    def test_every_user_oid_is_clear_of_the_builtin_range(self):
        # Allocating from max(oid)+1 put the first user database at 6, inside the
        # range PostgreSQL reserves for its own catalog.
        process = _start_server(EAGER_PORT, lazy=False)
        try:
            observed = _collect_oids(EAGER_PORT, ["db1", "db2"])
        finally:
            stop_server(process)

        for database, _, _ in DATABASES:
            for row in observed[f"{database}:relations"]:
                for oid in row[2:]:
                    self.assertGreaterEqual(oid, FIRST_USER_OID)
            for _datname, oid in observed[f"{database}:databases"]:
                self.assertGreaterEqual(oid, FIRST_USER_OID)


class HostSuppliedOidsArePassedThroughTest(unittest.TestCase):
    """A host that supplies OIDs gets exactly those OIDs back.

    This is the first of the two rules, and it is the one the auto-increment
    counter must never override: the counter exists only for hosts that supply
    nothing.
    """

    def test_the_source_oids_arrive_unchanged(self):
        lazy = _start_server(LAZY_PORT, lazy=True)
        try:
            observed = _collect_oids(LAZY_PORT, ["db1", "db2"])
        finally:
            stop_server(lazy)

        # Exactly what LazyTwoDatabaseSource handed out, not what a counter
        # would have chosen. Nothing renumbers a host that knows its own OIDs.
        for database, schema, table in DATABASES:
            relation_oid = stable_oid("rel", database, schema, table)
            rowtype_oid = stable_oid("type", database, schema, table)
            self.assertEqual(
                observed[f"{database}:relations"],
                [
                    (
                        schema,
                        table,
                        stable_oid("ns", database, schema),
                        relation_oid,
                        rowtype_oid,
                        rowtype_oid,
                        relation_oid,
                        relation_oid,
                    )
                ],
            )
            self.assertIn(
                (database, stable_oid("db", database)),
                observed[f"{database}:databases"],
            )


if __name__ == "__main__":
    unittest.main()
