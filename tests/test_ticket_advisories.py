"""Advisories on hard-coded counts, wide verify selections and a failing main lint."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from holophyte.tickets import ticket_advisories
from holophyte.tickets import ticket_template as tt
from tests.test_ticket_template import FILLED

ROOT = Path(__file__).resolve().parent.parent
VERIFY = '.venv/bin/python -m unittest test_orders_export'
IN_SCOPE = '- CSV serialization of the orders list'
CRITERION_2 = 'then only the header row.'
SAME_COUNT = 'the same count as main'
WIDE = 'In scope names'
LINT = 'lint fails on main'
NO_RUFF = 'ruff is not on PATH'
OUTPUT = "and that run's output is in the implementer output."
ONLY_ON_MAIN = 'the factory makes only on-main runs'
NO_TEST_FILE = 'names no test file for the factory to run'


def ticket(verify=VERIFY, in_scope=IN_SCOPE, criterion=CRITERION_2):
    return tt.parse(FILLED.replace(VERIFY, verify)
                    .replace(IN_SCOPE, in_scope)
                    .replace(CRITERION_2, criterion))


def matching(problems, phrase):
    return [p for p in problems
            if p.startswith(tt.ADVISORY_PREFIX) and phrase in p]


def git(repo, *args):
    return subprocess.run(
        ['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t',
         '-c', 'commit.gpgsign=false', *args],
        check=True, capture_output=True, text=True).stdout.strip()


def make_repo(test, files):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    repo = Path(tmp.name)
    git(repo, 'init', '-q', '-b', 'main')
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, 'add', '.')
    git(repo, 'commit', '-q', '-m', 'init')
    return repo


class HardCodedCountTests(unittest.TestCase):
    def test_stated_counts_in_criteria_and_verify_are_advised(self):
        for criterion, verify, shown_criterion, shown_verify in (
                ('then all 93 tests pass.', 'Ran 93 tests', '93 tests', 'Ran 93 tests'),
                ('then 42 fail.', '42 fail', '42 fail', '42 fail'),
                ('then 93 passed.', '93 passed', '93 passed', '93 passed'),
                ('then FAILED (failures=2).', 'FAILED (failures=2)',
                 'failures=2', 'failures=2')):
            with self.subTest(text=verify):
                t = ticket(verify="python3 tests/run_modules.py tests/test_a.py"
                                  f" | grep '{verify}'", criterion=criterion)
                found = matching(tt.validate(t), SAME_COUNT)
                self.assertEqual(len(found), 2, found)
                by_criterion = [a for a in found if 'criterion 2' in a]
                by_verify = [a for a in found if 'verify command' in a]
                self.assertEqual(len(by_criterion), 1, found)
                self.assertEqual(len(by_verify), 1, found)
                self.assertIn(shown_criterion, by_criterion[0])
                self.assertIn(shown_verify, by_verify[0])

    def test_numbers_that_are_not_run_results_are_not_advised(self):
        for criterion in ('then criterion 3 fails on main.',
                          'then C4 fails until the fix.',
                          'then round 2 failed.',
                          'then HOLO-42 tests stay green.',
                          'then test_42.py passes.',
                          'then the branch adds 3 tests.',
                          'then the branch adds 3 passing tests.'):
            with self.subTest(criterion=criterion):
                problems = tt.validate(ticket(criterion=criterion))
                self.assertEqual(matching(problems, SAME_COUNT), [])

    def test_a_count_after_a_conjunction_is_advised(self):
        found = matching(tt.validate(ticket(
            criterion='then stdout reports success and 93 passed.')), SAME_COUNT)
        self.assertEqual(len(found), 1, found)
        self.assertIn("'93 passed'", found[0])


class WideVerifyTests(unittest.TestCase):
    def test_directory_wider_than_in_scope_is_advised(self):
        repo = make_repo(self, {'tests/console/test_a.py': '',
                                'tests/console/test_b.py': '',
                                'tests/other.py': ''})
        verify = 'python3 -m pytest tests/console'
        found = matching(tt.validate(ticket(
            verify, '- `tests/console/test_a.py`'), repo=repo), WIDE)
        self.assertEqual(len(found), 1, found)
        for text in ('tests/console', '2 files', 'names 1 of them'):
            self.assertIn(text, found[0])
        for in_scope in ('- `tests/console/`', '- `tests/other.py`'):
            with self.subTest(in_scope=in_scope):
                problems = tt.validate(ticket(verify, in_scope), repo=repo)
                self.assertEqual(matching(problems, WIDE), [])

    def test_discover_pattern_wider_than_in_scope_is_advised(self):
        repo = make_repo(self, {'tests/test_serve_a.py': '',
                                'tests/test_serve_b.py': ''})
        in_scope = '- `tests/test_serve_a.py`'
        found = matching(tt.validate(ticket(
            "python3 -m unittest discover -s tests -p 'test_serve*.py'",
            in_scope), repo=repo), WIDE)
        self.assertEqual(len(found), 1, found)
        self.assertIn('tests/test_serve*.py', found[0])
        self.assertIn('2 files', found[0])
        narrow = tt.validate(ticket(
            "python3 -m unittest discover -s tests -p 'test_serve_a.py'",
            in_scope), repo=repo)
        self.assertEqual(matching(narrow, WIDE), [])

    def test_bare_glob_wider_than_in_scope_is_advised(self):
        repo = make_repo(self, {'test_a.py': '', 'test_b.py': ''})
        found = matching(tt.validate(ticket(
            'python3 -m pytest test_*', '- `test_a.py`'), repo=repo), WIDE)
        self.assertEqual(len(found), 1, found)
        self.assertIn('selects test_*', found[0])
        self.assertIn('2 files', found[0])

    def test_glob_under_a_named_parent_is_not_advised(self):
        repo = make_repo(self, {'tests/console/test_a.py': '',
                                'tests/console/test_b.py': ''})
        verify = "python3 -m pytest 'tests/*/test_*.py'"
        named = '- `tests/console/test_a.py`'
        self.assertEqual(len(matching(tt.validate(
            ticket(verify, named), repo=repo), WIDE)), 1)
        problems = tt.validate(ticket(verify, named + '\n- `tests/`'), repo=repo)
        self.assertEqual(matching(problems, WIDE), [])

    def test_discover_character_class_wider_than_in_scope_is_advised(self):
        repo = make_repo(self, {'tests/test_a.py': '', 'tests/test_b.py': ''})
        found = matching(tt.validate(ticket(
            "python3 -m unittest discover -s tests -p 'test_[ab].py'",
            '- `tests/test_a.py`'), repo=repo), WIDE)
        self.assertEqual(len(found), 1, found)
        self.assertIn('tests/test_[ab].py', found[0])
        self.assertIn('2 files', found[0])


class MainLintTests(unittest.TestCase):
    def failing_main(self):
        return make_repo(self, {'a.py': 'import os\n'})

    @unittest.skipUnless(shutil.which('ruff'), NO_RUFF)
    def test_failing_main_is_advised_and_working_tree_is_ignored(self):
        repo = self.failing_main()
        found = matching(tt.validate(ticket('ruff check .'), repo=repo), LINT)
        self.assertEqual(len(found), 1, found)
        self.assertIn('ruff check .', found[0])
        self.assertIn(git(repo, 'rev-parse', 'main')[:12], found[0])
        clean = make_repo(self, {'a.py': 'x = 1\n'})
        (clean / 'a.py').write_text('import os\n')
        problems = tt.validate(ticket('ruff check .'), repo=clean)
        self.assertEqual(matching(problems, LINT), [])

    @unittest.skipUnless(shutil.which('ruff'), NO_RUFF)
    def test_lint_that_does_not_run_ruff_on_the_whole_repository_is_not_advised(self):
        repo = self.failing_main()
        for verify in ('ruff check a.py', 'echo ruff check .', 'ruff check -',
                       'ruff check --stdin-filename a.py -'):
            with self.subTest(verify=verify):
                problems = tt.validate(ticket(verify), repo=repo)
                self.assertEqual(matching(problems, LINT), [])

    def test_missing_ruff_gives_no_advice(self):
        repo = self.failing_main()
        t = ticket('ruff check .')
        if shutil.which('ruff'):
            self.assertEqual(len(matching(tt.validate(t, repo=repo), LINT)), 1)
        with tempfile.TemporaryDirectory() as bin_dir:
            os.symlink(shutil.which('git'), Path(bin_dir) / 'git')
            with mock.patch.dict(os.environ, {'PATH': bin_dir}):
                problems = tt.validate(t, repo=repo)
        self.assertEqual(matching(problems, LINT), [])


class BaselineRunTests(unittest.TestCase):
    def advised(self, criterion, phrase):
        return matching(ticket_advisories.advisories(ticket(criterion=criterion)),
                        phrase)

    def test_a_run_on_another_branch_is_left_to_the_implementer(self):
        (advice,) = self.advised(
            f"then it stays. On the first ticket's branch the panel stays, "
            f"{OUTPUT}", ONLY_ON_MAIN)
        self.assertIn('criterion 2 ', advice)
        self.assertIn('the implementer must supply this one', advice)

    def test_an_on_main_clause_naming_no_test_file_is_advised(self):
        (advice,) = self.advised(f"then it stays. On main it fails, {OUTPUT}",
                                 NO_TEST_FILE)
        self.assertIn('criterion 2 ', advice)

    def test_an_on_main_clause_naming_a_test_file_is_not_advised(self):
        criterion = (f"`tests/test_export.py` passes. On main it fails, "
                     f"{OUTPUT}")
        self.assertEqual(self.advised(criterion, ONLY_ON_MAIN), [])
        self.assertEqual(self.advised(criterion, NO_TEST_FILE), [])


class ScriptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ruff'), NO_RUFF)
    def test_script_prints_all_three_advisories_and_files_ok(self):
        repo = make_repo(self, {'tests/console/test_a.py': '',
                                'tests/console/test_b.py': '',
                                'a.py': 'import os\n'})
        body = (FILLED.replace(VERIFY, 'python3 -m pytest tests/console\n'
                                       'ruff check .')
                .replace(IN_SCOPE, '- `tests/console/test_a.py`')
                .replace(CRITERION_2, 'then all 93 tests pass.'))
        path = Path(tempfile.mkdtemp()) / 'TICKET.md'
        self.addCleanup(shutil.rmtree, path.parent)
        path.write_text(body)
        r = subprocess.run([sys.executable, 'ticket_template.py', '--repo',
                            str(repo), str(path)], cwd=ROOT,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f'{path}: OK', r.stdout)
        for phrase in (SAME_COUNT, WIDE, LINT):
            self.assertEqual(len(matching(
                [line.strip().removeprefix('- ') for line in r.stdout.splitlines()],
                phrase)), 1, r.stdout)
        self.assertEqual(tt.filing_refusals(tt.validate(tt.parse(body), repo=repo)), [])


if __name__ == '__main__':
    unittest.main()
