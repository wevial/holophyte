"""A native board's key is its own on the host (KO-752): `project add` and
the loop's start refuse a `[board] key` another registered project's native
board has, or a registered store holds as a Linear team key.

Run: python3 -m unittest discover -s tests -p 'test_native_key.py'
"""
import unittest
from unittest.mock import patch

import store
from holophyte.project import Project
from tests.host_fixture import HostFixture


class NativeKeyTest(HostFixture):

    def native(self, directory, key):
        """A repository whose `[board]` is native with `key`."""
        path = self.repo(directory)
        Project.locate(path, adopt=False).config_path.write_text(
            f'[board]\nkind = "native"\nkey = "{key}"\n')
        return path

    def holding(self, path, team, *tickets):
        """`path`'s own store holding `(board id, identifier)` tickets."""
        conn = store.open(str(Project.locate(path).store_path))
        try:
            project = store.ensure_project(conn, team, path)
            for issue, identifier in tickets:
                store.mirror_ticket(conn, project, issue, identifier, "t")
        finally:
            conn.close()

    def linear(self, directory, identifier):
        """A registered Linear project whose store holds `identifier`."""
        path = self.repo(directory)
        self.cli("project", "add", str(path))
        self.holding(path, f"team-{directory}",
                     ("4b1e0c6a-uuid-" + directory, identifier))
        return path

    def test_add_refuses_a_key_a_linear_store_holds(self):
        linear = self.linear("linear", "KO-7")
        native = self.native("native", "KO")
        registry = (self.home / "host.toml").read_bytes()
        with self.assertRaises(SystemExit) as refused:
            self.cli("project", "add", str(native))
        line = str(refused.exception.code)
        self.assertIn("KO", line.replace(str(native), ""))
        self.assertIn(str(linear), line)
        self.assertEqual((self.home / "host.toml").read_bytes(), registry)
        self.assertFalse(Project.locate(native).store_path.exists())

    def test_add_refuses_a_second_native_board_with_one_key(self):
        first = self.native("first", "HOLO")
        self.cli("project", "add", str(first))
        second = self.native("second", "HOLO")
        with self.assertRaises(SystemExit) as refused:
            self.cli("project", "add", str(second))
        self.assertIn(str(first), str(refused.exception.code))
        self.assertEqual([path for _, path in self.registered()], [first])

    def test_own_native_and_linear_tickets_under_other_keys_pass(self):
        # HOLO-1 is native (its board id is its identifier); KO-5 is an old
        # Linear ticket under another team key.
        native = self.native("native", "HOLO")
        self.holding(native, "native:HOLO", ("HOLO-1", "HOLO-1"),
                     ("9f0c2d4e-uuid", "KO-5"))
        self.cli("project", "add", str(native))
        self.assertEqual([path for _, path in self.registered()], [native])

    def test_loop_start_refuses_a_key_a_linear_store_holds(self):
        self.linear("linear", "NAT-2")
        native = self.native("native", "NAT")
        with patch("holophyte.cli.main", return_value=0) as main, \
                self.assertRaises(SystemExit) as refused:
            self.cli(str(native))
        self.assertIn("NAT", str(refused.exception.code).replace(
            str(native), ""))
        main.assert_not_called()

    def test_an_entry_whose_config_cannot_be_read_is_skipped(self):
        # As `project list` skips it: its store is not read either.
        linear = self.linear("linear", "NAT-2")
        Project.locate(linear, adopt=False).config_path.write_text("[board\n")
        native = self.native("native", "NAT")
        self.cli("project", "add", str(native))
        self.assertIn(native, [path for _, path in self.registered()])


if __name__ == "__main__":
    unittest.main()
