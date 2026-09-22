"""Deferred imported-name ceilings after the Run extraction (KO-591)."""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CEILINGS = {"loop": 1, "babysitter": 26, "claim": 6, "merge_gate": 2, "run": 2}


class DeferredImportTests(unittest.TestCase):
    def test_deferred_imports_stay_below_the_candidate_ceiling(self):
        for module, ceiling in CEILINGS.items():
            with self.subTest(module=module):
                tree = ast.parse((ROOT / "holophyte" / f"{module}.py").read_text())
                top = {id(node) for node in tree.body}
                count = sum(len(node.names) for node in ast.walk(tree)
                            if isinstance(node, (ast.Import, ast.ImportFrom))
                            and id(node) not in top)
                self.assertLessEqual(count, ceiling)
