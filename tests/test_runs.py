"""`holophyte.runs.review_round_cap()`: the review-round cap a candidate
earns from its size and the `[loop]` review keys (KO-299).

Run: python3 -m unittest discover -s tests -p 'test_runs*' -v
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import holophyte.config  # noqa: E402 - after the sys.path insert above
import holophyte.config_tables  # noqa: E402 - after the sys.path insert above
import holophyte.runs  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
from tests.schema_fixture import (  # noqa: E402 - after the sys.path insert
    DOCUMENTED_COLUMNS,
    move_ahead_additively,
)


def a_config(**overrides):
    """A `LoopConfig` over the defaults, with the review keys overridden."""
    values = dict(holophyte.config_tables.LOOP_KEYS)
    values.update(overrides)
    return holophyte.config_tables.LoopConfig(**values)


class ReviewRoundCapTests(unittest.TestCase):

    def test_scales_with_lines_and_caps(self):
        """Base 2, one more round per 800 changed lines, four at most: a
        300-line candidate keeps the base, a 1,700-line one earns the two
        extra rounds that reach the ceiling, and a 9,000-line one stops at
        the ceiling. With `review_rounds_per_lines = 0` nothing scales."""
        cfg = a_config(review_rounds=2, review_rounds_per_lines=800,
                       review_rounds_max=4)

        self.assertEqual(
            [holophyte.runs.review_round_cap(n, cfg) for n in (300, 1700, 9000)],
            [2, 4, 4])

        flat = a_config(review_rounds=2, review_rounds_per_lines=0,
                        review_rounds_max=4)
        self.assertEqual(
            [holophyte.runs.review_round_cap(n, flat) for n in (0, 300, 9000)],
            [2, 2, 2])

    def test_the_default_config_keeps_the_two_round_base(self):
        """`MAX_ROUNDS` is the base's default: an unconfigured target still
        pays two rounds for a small change."""
        self.assertEqual(holophyte.config_tables.LOOP_KEYS["review_rounds"],
                         holophyte.runs.MAX_ROUNDS)
        self.assertEqual(holophyte.runs.review_round_cap(1, a_config()), 2)


class OpenStoreSchemaTests(unittest.TestCase):
    """`open_store()` leaves the schema to `store.open()` (KO-720)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def opened_and_closed(self, path):
        holophyte.runs.open_store(None, path).close()
        with closing(sqlite3.connect(path)) as conn:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            note = conn.execute(
                "SELECT note FROM interventions WHERE action = 'migrate'"
                " ORDER BY id DESC LIMIT 1").fetchone()
        return version, tables, note and json.loads(note[0])

    def test_compatible_newer_store_keeps_its_stamp_and_note(self):
        path = self.dir / 'store.sqlite3'
        store.open(path).close()
        move_ahead_additively(path, readableFrom=store.SCHEMA_VERSION)

        version, _, note = self.opened_and_closed(path)

        self.assertEqual(version, store.SCHEMA_VERSION + 1)
        self.assertEqual(note['build'], 'newer')
        self.assertEqual(note['to'], store.SCHEMA_VERSION + 1)

    def test_fresh_and_older_stores_are_migrated_to_this_build(self):
        path = self.dir / 'store.sqlite3'
        fresh_version, fresh_tables, _ = self.opened_and_closed(path)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute(f'PRAGMA user_version = {store.SCHEMA_VERSION - 1}')

        older_version, older_tables, _ = self.opened_and_closed(path)

        self.assertEqual([fresh_version, older_version],
                         [store.SCHEMA_VERSION] * 2)
        self.assertLessEqual(set(DOCUMENTED_COLUMNS), fresh_tables)
        self.assertLessEqual(set(DOCUMENTED_COLUMNS), older_tables)


if __name__ == "__main__":
    unittest.main()
