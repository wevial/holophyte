"""The SwiftBar drawer against a real host daemon (consolidation stage 3).

One `[[daemon]]` entry names a host daemon -- a `HostServer` over two
projects registered with `project add`, each with a real store -- and the
machine token as its `token_file`. The drawer polls the root, then each
project under `/projects/NAME` with that one token, and renders a block per
project under one line for the host's last sweep. The daemon binds beyond
loopback, so every read it answers demanded the token.

Run: python3 -m unittest discover -s tests -p 'test_drawer_host*' -v
"""
import json
import sqlite3
from time import time

from holophyte.project import Project
from tests.test_drawer import drawer
from tests.test_serve_host import HostServeCase


class DrawerHostDaemonTests(HostServeCase):
    def setUp(self):
        super().setUp()
        self.host_config(machine_token_file=self.machine())
        self.start(bind="0.0.0.0")
        now = int(time() * 1000)
        (self.home / "sweep.json").write_text(json.dumps({
            "started": now - 25_000, "ended": now - 20_000,
            "revision": "abc1234", "pid": 777, "exit": 0,
            "projects": {"alpha": "ok", "beta": "ok"}}))
        config = self.root / "drawer.toml"
        config.write_text(
            '[[daemon]]\nname = "writer"\n'
            f'url = "http://127.0.0.1:{self.port}"\n'
            f'token_file = "{self.home / "machine.token"}"\n')
        self.daemon = drawer.load_config(config)["daemons"][0]

    def test_one_daemon_entry_is_a_block_per_project_under_the_sweep_line(self):
        entries = drawer.poll_daemon(self.daemon)
        self.assertEqual([entry["name"] for entry in entries],
                         ["alpha", "beta"])
        self.assertEqual([entry["status"].get("project") for entry in entries],
                         [str(self.paths["alpha"]), str(self.paths["beta"])])
        lines = drawer.render(entries, drawer.reference_now(entries))
        sweep = [line for line in lines if line.startswith("writer · sweep")]
        # A fresh sweep is one grey line above the first block, not a
        # "needs you" row.
        self.assertEqual(len(sweep), 1)
        self.assertRegex(sweep[0], r"^writer · sweep fresh · 2\ds ago \| size=11$")
        self.assertTrue(lines[lines.index(sweep[0]) + 1].startswith("alpha · "))
        text = "\n".join(lines)
        self.assertIn("KO-7 · working", text)
        self.assertIn("2 targets · 1 host", text)

    def test_without_the_machine_token_the_host_is_one_refused_entry(self):
        entries = drawer.poll_daemon({**self.daemon, "token": None})
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"].get("http_status"), 401)

    def test_a_locked_store_is_its_project_block_and_the_other_reads_whole(self):
        holder = sqlite3.connect(Project.locate(self.paths["beta"]).store_path,
                                 isolation_level=None)
        holder.execute("PRAGMA locking_mode = EXCLUSIVE")
        holder.execute("BEGIN EXCLUSIVE")
        self.addCleanup(holder.close)
        self.addCleanup(holder.execute, "ROLLBACK")
        alpha, beta = drawer.poll_daemon(self.daemon)
        self.assertEqual(alpha["status"]["project"], str(self.paths["alpha"]))
        self.assertIn("locked", beta["status"]["error"])
        self.assertIsNone(beta["attention"])
        text = "\n".join(drawer.render([alpha, beta], drawer.reference_now([alpha])))
        self.assertRegex(
            text, r"\nbeta · \? \| size=11\n[^\n]*database is locked \| color=")

    def test_a_sweep_that_is_not_fresh_needs_you(self):
        (self.home / "sweep.json").write_text(json.dumps({
            "started": int(time() * 1000) - 400_000, "ended": None}))
        entries = drawer.poll_daemon(self.daemon)
        lines = drawer.render(entries, drawer.reference_now(entries))
        needs = lines.index("NEEDS YOU | size=11")
        self.assertRegex(lines[needs + 1], r"^writer · sweep killed \| color=#F0B13A$")
