"""Witness resolution follows the class unittest discovers (KO-444)."""
import tempfile
import unittest
from pathlib import Path

from holophyte.review import missing_witnesses, test_references


class WitnessResolutionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "tests").mkdir()

    def write(self, name, source):
        (self.root / "tests" / name).write_text(source)

    def missing(self, name):
        return missing_witnesses(
            test_references(f"tests/mod.py::Klass::{name}"), self.root)

    def test_inherited_sibling_mixin_test_is_found(self):
        self.write("fixture.py", "class Cases:\n    def test_x(self):\n        pass\n")
        self.write("mod.py", "from fixture import Cases\n"
                   "import unittest\n"
                   "class Klass(Cases, unittest.TestCase):\n    pass\n")
        self.assertEqual(self.missing("test_x"), [])

    def test_absent_or_noncallable_attribute_is_missing(self):
        self.write("mod.py", "class Klass:\n"
                   "    def test_value(self):\n        pass\n"
                   "    test_value = 42\n")
        for name in ("test_nope", "test_value"):
            with self.subTest(name=name):
                (note,) = self.missing(name)
                self.assertIn(f"tests/mod.py::Klass::{name}", note)
                self.assertIn("import", note)

    def test_failed_import_uses_literal_scan_and_names_fallback(self):
        self.write("mod.py", "raise RuntimeError('cannot import')\n"
                   "class Klass:\n    def test_y(self):\n        pass\n")
        self.assertEqual(self.missing("test_y"), [])
        (note,) = self.missing("test_nope")
        self.assertIn("fallback", note)
        self.assertIn("test_nope", note)
