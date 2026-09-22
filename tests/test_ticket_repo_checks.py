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
        return tt.blocking(self.everything(body))

    def everything(self, body):
        return tt.validate(tt.parse(body), repo=self.repo)

    def missing(self, body):
        return [p for p in self.everything(body) if 'does not exist' in p]

    def test_missing_paths_are_advisories_in_all_three_places(self):
        advisory = tt.ADVISORY_PREFIX + 'path does not exist in '
        for label, anchor, replacement in (
            ('Acceptance criteria #1', 'then 4 lines including header.',
             'then `tests/test_a.py` and `tests/test_missing.py` witness it.'),
            ('Implementation notes',
             'Endpoint lives beside the other order routes.',
             'Use `tests/test_missing.py`.'),
            ('verify command', '.venv/bin/python -m unittest tests.test_a',
             'ruff check tests/test_a.py tests/test_missing.py'),
        ):
            with self.subTest(label=label):
                body = self.body.replace(anchor, replacement)
                self.assertEqual(self.missing(body), [
                    f'{advisory}{label}: tests/test_missing.py'])
                self.assertEqual(self.problems(body), [])

    def test_new_declarations_silence_the_advisory(self):
        anchor = 'then 4 lines including header.'
        for declaration in ('a new test file `tests/test_missing.py`',
                            'a new directory `tests/future/` with '
                            '`tests/future/test_missing.py`',
                            'a new directory `future` with `future/test_missing.py`'):
            with self.subTest(declaration=declaration):
                self.assertEqual(self.missing(self.body.replace(anchor,
                                 f'then {declaration} witnesses it.')), [])
        body = self.body.replace(anchor, 'then new behavior works. '
                                 '`tests/test_missing.py` witnesses it.')
        self.assertIn('tests/test_missing.py', '\n'.join(self.missing(body)))

    def test_verify_modules_and_exclusions(self):
        body = self.body.replace('tests.test_a', 'tests.test_a tests.test_gone')
        self.assertEqual(self.missing(body), [
            f'{tt.ADVISORY_PREFIX}unittest module does not exist in verify '
            'command: tests.test_gone'])
        self.assertEqual(self.problems(body), [])
        declared = body.replace('Endpoint lives beside the other order routes.',
                                'Add a new test file `tests/test_gone.py`.')
        self.assertEqual(self.missing(declared), [])
        (self.repo / '.gitignore').write_text('generated/\n')
        body = self.body.replace('Endpoint lives beside the other order routes.',
                                'Examples: `tests/test_*.py`, `--flag`, '
                                '`python3 -m unittest`, `generated/result.py`.')
        self.assertEqual(self.missing(body), [])

    def test_paths_must_resolve_inside_repository(self):
        with tempfile.TemporaryDirectory(dir=self.repo.parent) as outside:
            external = Path(outside)
            (external / 'test_helper.py').touch()
            (self.repo / 'shared').symlink_to(external, target_is_directory=True)
            for path in (f'../{external.name}/test_helper.py',
                         'shared/test_helper.py', 'shared/new_helper.py'):
                for label, anchor, replacement in (
                    ('Acceptance criteria #1', 'then 4 lines including header.',
                     f'then `{path}` witnesses it.'),
                    ('Implementation notes',
                     'Endpoint lives beside the other order routes.',
                     f'Add a new test file `{path}`.'),
                    ('verify command',
                     '.venv/bin/python -m unittest tests.test_a',
                     f'python3 {path}'),
                ):
                    with self.subTest(path=path, label=label):
                        body = self.body.replace(anchor, replacement)
                        self.assertIn(
                            f'path is outside the repository in {label}: {path}',
                            self.problems(body))
                        self.assertEqual(self.missing(body), [])
        body = self.body.replace('then 4 lines including header.',
                                 'then `tests/../tests/test_a.py` witnesses it.')
        self.assertEqual(self.everything(body), self.everything(self.body))

    def test_blank_template_and_cli_without_repo(self):
        for interpreter in ("python3", "python3 -B", "python3 -u",
                            ".venv/bin/python -X dev -W error"):
            for validator in ('ticket_template.py', '-m ticket_template'):
                body = self.body.replace(
                    '.venv/bin/python -m unittest tests.test_a',
                    f'{interpreter} {validator} ticketTemplate.md')
                for repo in (None, self.repo):
                    with self.subTest(interpreter=interpreter, validator=validator,
                                      repo=repo):
                        problems = tt.blocking(tt.validate(tt.parse(body), repo=repo))
                        self.assertTrue(any('blank template can never validate' in p
                                            for p in problems), problems)
        for command in ('echo ticket_template.py ticketTemplate.md',
                        'python3 -m other_module ticket_template.py ticketTemplate.md',
                        'python3 -c "print(1)" ticket_template.py ticketTemplate.md'):
            body = self.body.replace(
                '.venv/bin/python -m unittest tests.test_a', command)
            self.assertEqual(tt.blocking(tt.validate(tt.parse(body))), [])
        body = self.body.replace('tests.test_a', 'tests.test_gone')
        self.assertEqual(tt.validate(tt.parse(body)), tt.validate(tt.parse(self.body)))
        ticket = self.repo / 'ticket.md'
        ticket.write_text(body)
        result = subprocess.run([sys.executable, str(Path(tt.__file__)), str(ticket)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count('check skipped'), 1)
        self.assertIn('repository', result.stdout)
