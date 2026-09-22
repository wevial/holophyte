"""The operator has one explicit schema restart order."""
import unittest
from pathlib import Path


class OperatingDocsTests(unittest.TestCase):
    def test_supervisor_owns_the_documented_restart_order(self):
        doc = (Path(__file__).resolve().parents[1] / 'docs/operating.md').read_text()
        self.assertEqual(doc.count(
            'Pull, then restart the supervisor, then restart everything else '
            'in any order.'), 1)
        self.assertIn('The supervisor alone migrates the store', doc)
        self.assertNotRegex(doc, r'(?i)loop (?:opens and )?migrates')
