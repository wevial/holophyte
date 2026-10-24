"""A scout's own process, `factory.py --scout ID PROJECT`, and the host
sweep's scout step, over real git repositories registered in a temporary
host registry.

Run: python3 -m unittest discover -s tests -p 'test_scout_process.py' -v
"""
import io
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from host_fixture import HostFixture, git  # noqa: E402

import holophyte.scout.scout_process as scout_process  # noqa: E402
import holophyte.scout.scout_sweep as scout_sweep  # noqa: E402
import store  # noqa: E402
from holophyte.config.project import Project  # noqa: E402
from holophyte.host.registry import Host, HostError, settings  # noqa: E402
from holophyte.host.sweep_host import supervise_host  # noqa: E402
from store.scouts import (  # noqa: E402
    beat_scout,
    create_scout,
    end_scout,
    move_scout,
    scout,
)

MINUTE = 60 * 1000


def now_ms():
    return int(time.time() * 1000)


class ScoutFixture(HostFixture):
    def project(self, name, supervisor=""):
        """A registered native-board repository with two commits on main."""
        path = self.repo(name)
        target = Project.locate(path, adopt=False)
        target.config_path.write_text(
            f'[board]\nkind = "native"\nprefix = "{name.upper()}"\n'
            + supervisor)
        git(path, "checkout", "-q", "-b", "main")
        for message in ("first", "second"):
            (path / "notes.txt").write_text(message)
            git(path, "add", "notes.txt")
            git(path, "commit", "-q", "-m", message)
        code, printed = self.cli("project", "add", str(path))
        self.assertIsNone(code, printed)
        return path

    def conn(self, path):
        conn = store.open(str(Project.locate(path, adopt=False).store_path))
        self.addCleanup(conn.close)
        return conn

    def a_scout(self, path, phase="queued", base=None, created=None):
        conn = self.conn(path)
        (project,) = conn.execute("SELECT id FROM projects").fetchone()
        scout_id = create_scout(conn, project, "where is the bug?", "research",
                                30, now=created)
        if phase != "queued":
            move_scout(conn, scout_id, "planning",
                       base_sha=base or git(path, "rev-parse", "main"))
        return scout_id

    def lead(self, path, scout_id):
        return scout_process.lead_path(Project.locate(path, adopt=False),
                                       scout_id)

    def listed(self, path, lead):
        listing = git(path, "worktree", "list", "--porcelain")
        return [line for line in listing.splitlines()
                if line == f"worktree {lead}"]

    def events(self, path, scout_id):
        return [kind for (kind,) in self.conn(path).execute(
            "SELECT kind FROM runEvents WHERE scoutId = ? ORDER BY seq",
            (scout_id,))]

    def sweep_pass(self):
        out = io.StringIO()
        supervise_host(Host.locate(), once=True, out=out)
        return out.getvalue()


class ScoutProcessTests(ScoutFixture):
    def test_a_sweep_pass_starts_a_queued_scout_that_ends_done(self):
        path = self.project("alpha")
        scout_id = self.a_scout(path)
        main = git(path, "rev-parse", "main")
        self.sweep_pass()
        deadline = time.monotonic() + 60
        conn = self.conn(path)
        while scout(conn, scout_id).phase not in ("done", "failed") \
                and time.monotonic() < deadline:
            time.sleep(0.2)
        row = scout(conn, scout_id)
        self.assertEqual(row.phase, "done", row.reason)
        self.assertEqual(row.base_sha, main)
        self.assertTrue(row.host)
        self.assertIsNotNone(row.pid)
        events = self.events(path, scout_id)
        self.assertLess(events.index("scout_started"), events.index("scout_done"))
        lead = self.lead(path, scout_id)
        self.assertEqual(git(lead, "rev-parse", "HEAD"), main)
        self.assertEqual(git(lead, "rev-parse", "--abbrev-ref", "HEAD"),
                         "HEAD")
        self.assertEqual(len(self.listed(path, lead)), 1)

    def test_the_lead_path_is_replaced_whether_a_worktree_or_a_directory(self):
        path = self.project("alpha")
        older = git(path, "rev-parse", "main~1")
        main = git(path, "rev-parse", "main")
        for holder in ("worktree", "directory"):
            with self.subTest(holder=holder):
                scout_id = self.a_scout(path, "planning")
                lead = self.lead(path, scout_id)
                lead.parent.mkdir(parents=True)
                if holder == "worktree":
                    git(path, "worktree", "add", "--detach", "-q", str(lead),
                        older)
                else:
                    lead.mkdir()
                (lead / "stray.txt").write_text("left over")
                code, printed = self.cli("--scout", str(scout_id), str(path))
                self.assertEqual(code, 0, printed)
                self.assertEqual(git(lead, "rev-parse", "HEAD"), main)
                self.assertFalse((lead / "stray.txt").exists())
                self.assertEqual(git(lead, "status", "--porcelain"), "")
                self.assertEqual(len(self.listed(path, lead)), 1)

    def test_a_push_from_the_lead_is_refused_and_the_checkout_still_pushes(self):
        path = self.project("alpha")
        scout_id = self.a_scout(path, "planning")
        code, printed = self.cli("--scout", str(scout_id), str(path))
        self.assertEqual(code, 0, printed)
        bare = self.root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        git(path, "remote", "add", "origin", str(bare))

        def push(checkout):
            return subprocess.run(
                ["git", "push", "origin", "HEAD:refs/heads/scout-push"],
                cwd=checkout, capture_output=True, text=True).returncode

        def branch():
            return subprocess.run(
                ["git", "--git-dir", str(bare), "rev-parse", "--verify", "-q",
                 "refs/heads/scout-push"], capture_output=True).returncode == 0

        self.assertNotEqual(push(self.lead(path, scout_id)), 0)
        self.assertFalse(branch())
        self.assertEqual(push(path), 0)
        self.assertTrue(branch())

    def test_a_base_naming_no_commit_ends_the_scout_failed(self):
        path = self.project("alpha")
        missing = "0123456789abcdef" * 2 + "01234567"
        scout_id = self.a_scout(path, "planning", base=missing)
        code, printed = self.cli("--scout", str(scout_id), str(path))
        self.assertEqual(code, 1, printed)
        row = scout(self.conn(path), scout_id)
        self.assertEqual(row.phase, "failed")
        self.assertIn(missing, row.reason)
        self.assertIn("scout_failed", self.events(path, scout_id))
        lead = self.lead(path, scout_id)
        self.assertFalse(lead.exists() or lead.is_symlink())

    def test_a_scout_not_ready_to_run_is_refused_unchanged(self):
        path = self.project("alpha")
        queued = self.a_scout(path)
        ended = self.a_scout(path, "planning")
        end_scout(self.conn(path), ended, "done", "answered")
        for scout_id, phase in ((queued, "queued"), (ended, "done"),
                                (999, None)):
            with self.subTest(phase=phase):
                before = scout(self.conn(path), scout_id)
                code, printed = self.cli("--scout", str(scout_id), str(path))
                self.assertEqual(code, 1)
                self.assertIn(f"scout {scout_id}", printed)
                if phase is not None:
                    self.assertIn(phase, printed)
                self.assertEqual(scout(self.conn(path), scout_id), before)
                self.assertEqual(self.events(path, scout_id), [])

    def test_the_heartbeat_beats_and_stops_once_the_scout_has_ended(self):
        path = self.project("alpha")
        scout_id = self.a_scout(path, "planning")
        conn, other = self.conn(path), self.conn(path)
        seen = set()
        with scout_process.scout_heartbeat(conn, scout_id, 0.2):
            for _ in range(10):
                seen.add(scout(other, scout_id).heartbeat_at)
                time.sleep(0.1)
        self.assertGreaterEqual(len(seen), 3)

        with self.assertRaises(scout_process.ScoutEnded):
            with scout_process.scout_heartbeat(conn, scout_id, 0.2):
                time.sleep(0.3)
                end_scout(other, scout_id, "failed", "ended by another hand")
                ended_beat = scout(other, scout_id).heartbeat_at
                time.sleep(0.7)
        row = scout(other, scout_id)
        self.assertEqual(row.reason, "ended by another hand")
        self.assertEqual(row.heartbeat_at, ended_beat)

    def test_the_mode_exits_1_when_the_scout_ends_elsewhere_mid_run(self):
        path = self.project("alpha",
                            "[supervisor]\nheartbeat_stale_min = 0.01\n")
        scout_id = self.a_scout(path, "planning")
        other = self.conn(path)

        def ended_elsewhere(_project, ended_id, _base):
            end_scout(other, ended_id, "failed", "ended by another hand")
            time.sleep(1)

        with patch.object(scout_process, "make_lead", ended_elsewhere):
            code, printed = self.cli("--scout", str(scout_id), str(path))
        self.assertEqual(code, 1, printed)
        row = scout(other, scout_id)
        self.assertEqual((row.phase, row.reason),
                         ("failed", "ended by another hand"))
        self.assertNotIn("scout_done", self.events(path, scout_id))


class ScoutSweepTests(ScoutFixture):
    def setUp(self):
        super().setUp()
        self.spawned = []

        def recorder(argv, **options):
            self.spawned.append((argv, options))
            return SimpleNamespace(pid=4242)

        patcher = patch.object(scout_sweep, "SPAWN", recorder)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_stale_scout_ends_failed_and_a_fresh_one_is_left(self):
        path = self.project("alpha",
                            "[supervisor]\nheartbeat_stale_min = 1\n")
        stale = self.a_scout(path, "planning")
        conn = self.conn(path)
        move_scout(conn, stale, "swarming")
        beat_scout(conn, stale, now=now_ms() - 2 * MINUTE)
        fresh = self.a_scout(path, "planning")
        before = scout(conn, fresh)
        self.sweep_pass()
        row = scout(conn, stale)
        self.assertEqual(row.phase, "failed")
        self.assertTrue(row.reason.startswith("heartbeat stale"), row.reason)
        self.assertEqual(scout(conn, fresh), before)

    def test_a_project_with_a_config_error_still_counts_its_live_scouts(self):
        alpha, beta = self.project("alpha"), self.project("beta")
        with open(Host.locate().path, "a") as registry:
            registry.write("\n[scout]\nmax_parallel = 1\n")
        live = self.a_scout(alpha, "planning")
        queued = self.a_scout(beta)
        config = Project.locate(alpha, adopt=False).config_path
        config.write_text(config.read_text() + '[serve]\nname = ""\n')
        self.sweep_pass()
        self.assertEqual(self.spawned, [])
        self.assertEqual(scout(self.conn(alpha), live).phase, "planning")
        self.assertEqual(scout(self.conn(beta), queued).phase, "queued")

    def test_the_host_starts_the_oldest_queued_scout_under_its_limit(self):
        alpha, beta = self.project("alpha"), self.project("beta")
        with open(Host.locate().path, "a") as registry:
            registry.write("\n[scout]\nmax_parallel = 2\n")
        live = self.a_scout(alpha, "planning", created=1000)
        first = self.a_scout(alpha, created=2000)
        second = self.a_scout(alpha, created=3000)
        other = self.a_scout(beta, created=4000)
        self.sweep_pass()
        self.assertEqual(len(self.spawned), 1)
        argv, options = self.spawned[0]
        self.assertEqual(argv[-3:], ["--scout", str(first), str(alpha)])
        self.assertTrue(options["start_new_session"])
        row = scout(self.conn(alpha), first)
        self.assertEqual((row.phase, row.base_sha),
                         ("planning", git(alpha, "rev-parse", "main")))
        self.assertEqual(scout(self.conn(alpha), second).phase, "queued")
        self.assertEqual(scout(self.conn(beta), other).phase, "queued")

        self.sweep_pass()
        self.assertEqual(len(self.spawned), 1)

        end_scout(self.conn(alpha), live, "done", "answered")
        self.sweep_pass()
        self.assertEqual([argv[-2] for argv, _ in self.spawned],
                         [str(first), str(second)])
        self.assertEqual(scout(self.conn(beta), other).phase, "queued")

    def test_the_host_scout_limit_is_a_positive_integer(self):
        registry = Host.locate().path
        registry.parent.mkdir(parents=True, exist_ok=True)
        for table, key in (("max_parallel = 0", "max_parallel"),
                           ('max_parallel = "two"', "max_parallel"),
                           ("slots = 1", "slots")):
            with self.subTest(table=table):
                registry.write_text(f"[scout]\n{table}\n")
                with self.assertRaises(HostError) as raised:
                    settings(Host.locate())
                self.assertIn(key, str(raised.exception))
        registry.write_text("")
        self.assertEqual(settings(Host.locate()).scout_max_parallel, 2)
