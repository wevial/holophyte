"""The blast-radius tier each review round records, and its `--report` line."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import tomllib
import unittest
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import APPROVE, IMPLEMENT, REQUEST_CHANGES, Commit  # noqa: E402
from loop_fixture import LoopFixture  # noqa: E402

import holophyte.cli.report as report  # noqa: E402
import store  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.config.checks import check_config  # noqa: E402
from holophyte.config.review_settings import review_config  # noqa: E402
from holophyte.review.blast_radius import assess  # noqa: E402

TICKET = ("# Touch the readme\n\n## What / Why / How\n\n"
          "**What:** Edit the readme.\n\n**Blast radius:** high\n\n"
          "**How:** By hand.\n")


class Settings:
    """A project whose config is the given TOML text."""

    config_path = "config.toml"

    def __init__(self, toml=""):
        self.table = tomllib.loads(toml)

    def config(self):
        return self.table


class Repository:
    def __init__(self, test, files):
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "factory@example.invalid")
        self.git("config", "user.name", "Factory Test")
        self.base = self.commit({"README.md": "base\n", **files})

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, files):
        for path, text in files.items():
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "change")
        return self.git("rev-parse", "HEAD")

    def candidate(self, *paths):
        self.git("checkout", "-q", "--detach", self.base)
        return self.commit({path: f"changed {path}\n" for path in paths})

    def round(self, *paths, toml="", **raising):
        return assess(self.root, self.base, self.candidate(*paths),
                      review_config(Settings(toml)), **raising)


def sources(found):
    return [reason.split(":", 1)[0] for reason in found.reasons]


class DiffTierTests(unittest.TestCase):
    def test_base_high_paths_gate_each_kind_and_a_readme_is_low(self):
        repo = Repository(self, {})
        for path in (".github/workflows/ci.yml", "poetry.lock",
                     "web/package.json", "db/migrations/0001_init.sql",
                     "deploy/app.service", "docker/reviewer.Dockerfile"):
            with self.subTest(path=path):
                found = repo.round(path)
                self.assertEqual(found.tier, "high")
                self.assertEqual(found.gated, [path])
                self.assertTrue(any(reason.startswith("base path") and path in reason
                                    for reason in found.reasons), found.reasons)
        found = repo.round("README.md")
        self.assertEqual((found.tier, found.reasons, found.gated), ("low", [], []))

    def test_project_high_paths_add_to_the_base_list_and_never_remove(self):
        repo = Repository(self, {})
        found = repo.round("app/auth/session.ts",
                           toml='[review]\nhigh_paths = ["app/auth/*"]\n')
        self.assertEqual((found.tier, sources(found)), ("high", ["project path"]))
        found = repo.round("poetry.lock", toml="[review]\nhigh_paths = []\n")
        self.assertEqual((found.tier, sources(found)), ("high", ["base path"]))

    def test_medium_paths_and_package_spread_raise_and_tests_and_docs_do_not(self):
        repo = Repository(self, {})
        found = repo.round("src/parse/csv.py",
                           toml='[review]\nmedium_paths = ["src/parse/*"]\n')
        self.assertEqual((found.tier, sources(found)), ("medium", ["medium path"]))
        found = repo.round("api/a.py", "web/b.ts", "worker/c.go")
        self.assertEqual((found.tier, sources(found)), ("medium", ["packages"]))
        found = repo.round("tests/test_a.py", "docs/b.md", "src/c.py")
        self.assertEqual(found.tier, "low")


IMPORT_FORMS = ("import pkg.core\n", "import pkg.core as core\n",
                "from pkg.core import VALUE\n", "from pkg import core\n")


def importers(count):
    return {f"users/u{n}.py": IMPORT_FORMS[n % len(IMPORT_FORMS)]
            for n in range(count)}


class FanInTests(unittest.TestCase):
    def changed_core(self, count, toml=""):
        repo = Repository(self, {"pkg/__init__.py": "", "pkg/core.py": "VALUE = 1\n",
                                 **importers(count)})
        return repo.round("pkg/core.py", toml=toml)

    def test_ten_importers_across_all_four_forms_make_a_change_medium(self):
        found = self.changed_core(10)
        self.assertEqual((found.tier, sources(found)), ("medium", ["fan-in"]))
        self.assertIn("pkg/core.py has 10 importers", found.reasons[0])

    def test_nine_importers_are_below_the_default_threshold(self):
        self.assertEqual(self.changed_core(9).tier, "low")

    def test_fan_in_zero_turns_the_signal_off(self):
        self.assertEqual(self.changed_core(10, "[review]\nfan_in = 0\n").tier, "low")


class RaisingTests(unittest.TestCase):
    def test_the_ticket_field_raises_a_readme_change_to_high(self):
        found = Repository(self, {}).round("README.md", ticket=TICKET)
        self.assertEqual((found.tier, sources(found)), ("high", ["ticket"]))


@dataclass
class CommitSaying(Commit):
    reply: str = ""

    def play(self, cwd, turn):
        return f"{super().play(cwd, turn)}\n{self.reply}"


@dataclass
class Remove:
    path: str

    role = IMPLEMENT

    def play(self, cwd, turn):
        for args in (("rm", "-q", self.path), ("commit", "-q", "-m", "revert")):
            subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)
        return f"removed {self.path}"


class RecordedRoundTests(LoopFixture):
    def payloads(self, kind):
        return [json.loads(payload) for (payload,) in self.read(
            f"SELECT payload FROM runEvents WHERE kind = '{kind}' ORDER BY seq")]

    def test_the_implementer_line_raises_round_one_and_is_recorded(self):
        fake, _ = self.loop(CommitSaying(
            path="README.md", body="changed\n",
            reply="BLAST RADIUS: high — loosens the permission rule"), APPROVE)
        self.assertIn("BLAST RADIUS: high|medium|low", fake.turns[0].goal)
        self.assertEqual(self.payloads("blast_radius_declared"),
                         [{"tier": "high", "reason": "loosens the permission rule"}])
        [first] = self.payloads("blast_radius")
        self.assertEqual(first["tier"], "high")
        self.assertIn("implementer: high — loosens the permission rule",
                      first["reasons"])

    def test_a_low_declaration_cannot_lower_a_lockfile_change(self):
        self.loop(CommitSaying(path="poetry.lock", body="lock\n",
                               reply="BLAST RADIUS: low — docs"), APPROVE)
        self.assertEqual(self.payloads("blast_radius_declared"),
                         [{"tier": "low", "reason": "docs"}])
        [first] = self.payloads("blast_radius")
        self.assertEqual((first["tier"], first["gated"]), ("high", ["poetry.lock"]))

    def test_an_echoed_brief_template_is_no_declaration(self):
        self.loop(CommitSaying(path="README.md", body="changed\n",
                               reply="BLAST RADIUS: high|medium|low — reason"),
                  APPROVE)
        self.assertEqual(self.payloads("blast_radius_declared"), [])
        [first] = self.payloads("blast_radius")
        self.assertEqual(first["tier"], "low")

    def test_a_reverted_lockfile_keeps_the_second_round_high(self):
        fake, _ = self.loop(Commit(path="poetry.lock", body="lock\n"),
                            REQUEST_CHANGES, Remove("poetry.lock"), APPROVE)
        reviewed = [turn.candidate_sha for turn in fake.turns if turn.role == "review"]
        first, second = self.payloads("blast_radius")
        self.assertEqual([(e["round"], e["sha"]) for e in (first, second)],
                         list(zip((1, 2), reviewed)))
        self.assertEqual((first["tier"], first["gated"]), ("high", ["poetry.lock"]))
        self.assertEqual((second["tier"], second["gated"]), ("high", []))
        self.assertEqual([reason.split(":")[0] for reason in second["reasons"]],
                         ["earlier round"])

    def test_startup_refuses_each_malformed_review_key_by_name(self):
        for line, key in (('high_paths = "app/*"', "high_paths"),
                          ('high_paths = ["/etc/app"]', "high_paths"),
                          ('medium_paths = ["src/../x"]', "medium_paths"),
                          ("fan_in = -1", "fan_in"),
                          ("packages = true", "packages"),
                          ("typo = 1", "typo")):
            with self.subTest(line=line):
                self.configure(f"[review]\n{line}\n")
                with self.assertRaisesRegex(SystemExit, rf"\[review\] {key}"):
                    check_config(self.project)
        self.configure('[review]\nhigh_paths = ["app/*"]\nfan_in = 0\npackages = 4\n')
        check_config(self.project)


class ReportLineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.open(str(Path(tmp.name) / "store.db"))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project_id = store.tickets.ensure_project(self.conn, "team", tmp.name)

    def run_with_tiers(self, number, *tiers):
        ticket = store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id=f"issue-{number}",
            linear_identifier=f"KO-{number}", title="ticket")
        run = store.claim(self.conn, self.project_id, ticket)
        for rnd, tier in enumerate(tiers, 1):
            store.record_event(self.conn, run, "blast_radius", tier, level="detail",
                               payload=json.dumps({"round": rnd, "tier": tier}))

    def blast_lines(self):
        return [line for line in report.report_lines(self.conn)
                if line.startswith("blast radius:")]

    def test_each_runs_newest_tier_is_counted(self):
        self.run_with_tiers(1, "medium", "high")
        self.run_with_tiers(2, "low")
        self.assertEqual(self.blast_lines(),
                         ["blast radius: high 1 · medium 0 · low 1"])

    def test_no_line_without_a_recorded_tier(self):
        self.run_with_tiers(1)
        self.assertEqual(self.blast_lines(), [])


if __name__ == "__main__":
    unittest.main()
