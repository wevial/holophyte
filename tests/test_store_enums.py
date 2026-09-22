"""The SQL boundary and public vocabulary agree, preserving existing constraints."""
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import store
from store import enums

PREVIOUS_SCHEMA = Path(__file__).with_name('store_v26.sql')
CHECK = re.compile(r'CHECK \((?:\S+ IS NULL\s+OR )?(\S+) IN \([^)]*\)\)')


def enum_checks(conn):
    return {(table, match[1].strip('"')): match[0]
            for table, sql in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table'")
            for match in CHECK.finditer(sql)}


class StoreEnumTests(unittest.TestCase):
    def test_fresh_constraints_equal_enums_and_previous_schema_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = store.open(Path(tmp) / 'store.db', migrate="owner")
            self.addCleanup(conn.close)
            actual = enum_checks(conn)
        old = sqlite3.connect(':memory:')
        self.addCleanup(old.close)
        old.executescript(PREVIOUS_SCHEMA.read_text())
        previous = enum_checks(old)
        unchanged = set(previous) - {('interventions', 'action')}
        self.assertEqual({key: actual[key] for key in unchanged},
                         {key: previous[key] for key in unchanged})
        self.assertEqual(actual['interventions', 'action'],
                         previous['interventions', 'action'][:-2]
                         + ", 'hold', 'release_hold', 'register_project', 'disable'))")
        self.assertEqual(set(actual), set(enums.CONSTRAINED_COLUMNS))
        for key, enum in enums.CONSTRAINED_COLUMNS.items():
            self.assertEqual(actual[key], enums.check_clause(key[1], enum), key)

    def test_version_29_projects_migrate_without_losing_history(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        schema = (store.schema.SCHEMA + ";" + store.schema._INTERVENTIONS_DDL)
        schema = schema.replace(", 'disabled'", "")
        schema = schema.replace(", 'register_project', 'disable'", "")
        conn.executescript(schema)
        project = store.ensure_project(conn, "team", "/repo")
        store.hold(conn, project, "maintenance")
        conn.execute("PRAGMA user_version = 29")
        statements = []
        conn.set_trace_callback(statements.append)
        store.init(conn)
        conn.set_trace_callback(None)
        # Each INSERT ... SELECT copies the entire intervention history.
        copies = [sql for sql in statements
                  if sql.upper().startswith("INSERT INTO INTERVENTIONS")
                  and "SELECT" in sql.upper()]
        self.assertEqual(len(copies), 1, copies)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone(), (30,))
        self.assertEqual(conn.execute(
            "SELECT admission, holdNote FROM projects").fetchone(),
            ("held", "maintenance"))
        store.set_admission(conn, project, "disabled", "retired")
        self.assertEqual(conn.execute(
            "SELECT action, note FROM interventions WHERE projectId = ? ORDER BY id",
            (project,)).fetchall(), [("hold", "maintenance"), ("disable", "retired")])
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        store.init(conn)
        self.assertEqual(conn.execute(
            "SELECT admission, holdNote FROM projects").fetchone(),
            ("disabled", "retired"))

    def test_public_vocabulary_and_graph_membership(self):
        self.assertEqual(store.PHASES, tuple(e.value for e in enums.RunPhase))
        self.assertEqual(store.tickets.TICKET_STATUSES,
                         tuple(e.value for e in enums.TicketStatus))
        self.assertEqual(store.INTERVENTION_ACTIONS,
                         tuple(e.value for e in enums.InterventionAction))
        for graph, enum in ((store.TICKET_TRANSITIONS, enums.TicketStatus),
                            (store.RUN_PHASE_TRANSITIONS, enums.RunPhase)):
            self.assertEqual(set(graph), {e.value for e in enum})
            for targets in graph.values():
                self.assertLessEqual(set(targets), {e.value for e in enum})
        self.assertLessEqual(store.PARKED_PHASES, set(store.PHASES))

    def test_module_loads_without_store_package(self):
        # Load the standalone file: Python's dotted import would necessarily
        # execute store/__init__.py before it could reach store.enums.
        result = subprocess.run([sys.executable, '-I', '-c', '''
import importlib.util
import sys
spec = importlib.util.spec_from_file_location('standalone_enums', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.RunPhase.CLAIMED.value == 'claimed'
assert not any(n == 'store' or n.startswith('store.') for n in sys.modules)
''', str(Path(enums.__file__).resolve())], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
