"""A worker keeps its startup modules when its checkout moves."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class EagerImportTests(unittest.TestCase):
    def test_worker_imports_whole_build_before_checkout_moves(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            for package in ('holophyte', 'store'):
                shutil.copytree(root / package, checkout / package,
                                ignore=shutil.ignore_patterns('__pycache__'))
            for module in root.glob('*.py'):
                shutil.copy2(module, checkout / module.name)
            babysitter = checkout / 'holophyte' / 'babysitter.py'
            linear = checkout / 'linear_provider.py'
            for module in (babysitter, linear):
                with module.open('a') as stream:
                    stream.write('\ndef build_witness():\n    return "original"\n')
            expected = {'.'.join(path.relative_to(checkout).with_suffix('').parts)
                        for package in ('holophyte', 'store')
                        for path in (checkout / package).rglob('*.py')
                        if path.name != '__init__.py'}
            expected.update(path.stem for path in checkout.glob('*.py'))
            script = '''
import json, sys
from unittest.mock import patch
from holophyte.cli import cli

def worker(*args):
    print(json.dumps(sorted(sys.modules)), flush=True)
    sys.stdin.readline()
    from holophyte import babysitter
    import linear_provider
    print(babysitter.build_witness(), flush=True)
    print(linear_provider.build_witness(), flush=True)

with patch('holophyte.startup.build_sha', return_value='original'), \
     patch('holophyte.cli.Project'), patch('holophyte.cli.check_config'), \\
     patch('holophyte.cli.board_config', return_value=('team', 'project')), \\
     patch('holophyte.cli.worker', worker):
    cli(['.', '--worker'])
'''
            with subprocess.Popen(
                    [sys.executable, '-c', script], cwd=checkout,
                    env=dict(os.environ, PYTHONPATH=str(checkout)),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True) as child:
                try:
                    modules = json.loads(child.stdout.readline())
                    for module in (babysitter, linear):
                        module.write_text(
                            'def build_witness():\n    return "replacement"\n')
                    output, errors = child.communicate('\n', timeout=30)
                    self.assertEqual(child.returncode, 0, errors)
                    self.assertTrue(expected <= set(modules),
                                    sorted(expected - set(modules)))
                    self.assertEqual(output.splitlines(), ['original', 'original'])
                finally:
                    if child.poll() is None:
                        child.kill()
