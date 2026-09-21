"""Repository-backed ticket contracts, using real temporary files."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import ticket_template as tt
from tests.test_ticket_template import FILLED


class RepositoryChecksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (self.repo / 'tests').mkdir()
        (self.repo / 'tests/test_a.py').touch()
        self.body = FILLED.replace(
            '.venv/bin/python -m unittest test_orders_export',
            '.venv/bin/python -m unittest tests.test_a')

    def problems(self, body):
        return tt.blocking(tt.validate(tt.parse(body), repo=self.repo))

    def test_witness_paths_and_new_declarations(self):
        anchor = 'then 4 lines including header.'
        body = self.body.replace(anchor, 'then `tests/test_a.py` and '
                                 '`tests/test_missing.py` witness it.')
        self.assertEqual(self.problems(body), [
            'path does not exist in Acceptance criteria #1: tests/test_missing.py'])
        for declaration in ('a new test file `tests/test_missing.py`',
                            'a new directory `tests/future/` with '
                            '`tests/future/test_missing.py`',
                            'a new directory `future` with `future/test_missing.py`'):
            with self.subTest(declaration=declaration):
                self.assertEqual(self.problems(self.body.replace(anchor,
                                 f'then {declaration} witnesses it.')), [])
        body = self.body.replace(anchor, 'then new behavior works. '
                                 '`tests/test_missing.py` witnesses it.')
        self.assertIn('tests/test_missing.py', '\n'.join(self.problems(body)))
        notes = self.body.replace('Endpoint lives beside the other order routes.',
                                  'Use `tests/test_missing.py`.')
        self.assertEqual(self.problems(notes), [
            'path does not exist in Implementation notes: tests/test_missing.py'])

    def test_verify_paths_modules_and_exclusions(self):
        body = self.body.replace('tests.test_a', 'tests.test_a tests.test_gone')
        self.assertEqual(self.problems(body), [
            'unittest module does not exist in verify command: tests.test_gone'])
        declared = body.replace('Endpoint lives beside the other order routes.',
                                'Add a new test file `tests/test_gone.py`.')
        self.assertEqual(self.problems(declared), [])
        body = self.body.replace('.venv/bin/python -m unittest tests.test_a',
                                 'ruff check tests/test_a.py tests/gone.py')
        self.assertEqual(self.problems(body), [
            'path does not exist in verify command: tests/gone.py'])
        (self.repo / '.gitignore').write_text('generated/\n')
        body = self.body.replace('Endpoint lives beside the other order routes.',
                                'Examples: `tests/test_*.py`, `--flag`, '
                                '`python3 -m unittest`, `generated/result.py`.')
        self.assertFalse(any('does not exist' in p for p in self.problems(body)))

    def test_blank_template_and_cli_without_repo(self):
        body = self.body.replace('.venv/bin/python -m unittest tests.test_a',
                                 'python3 ticket_template.py ticketTemplate.md')
        for repo in (None, self.repo):
            problems = tt.blocking(tt.validate(tt.parse(body), repo=repo))
            self.assertTrue(any('blank template can never validate' in p
                                for p in problems), problems)
        body = self.body.replace('tests.test_a', 'tests.test_gone')
        self.assertEqual(tt.validate(tt.parse(body)), tt.validate(tt.parse(self.body)))
        ticket = self.repo / 'ticket.md'
        ticket.write_text(body)
        result = subprocess.run([sys.executable, str(Path(tt.__file__)), str(ticket)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count('check skipped'), 1)
        self.assertIn('repository', result.stdout)
