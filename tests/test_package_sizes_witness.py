"""Witness: no tracked Python file is over its line ceiling, and the size table pins nothing."""
import unittest

from tests.test_file_sizes import CEILING, PINNED, tracked_counts


class PackageSizesWitness(unittest.TestCase):

    def test_no_tracked_python_file_is_over_its_ceiling(self):
        over = {name: lines for name, lines in tracked_counts().items()
                if lines > CEILING["test" if name.startswith("tests/") else "source"]}
        self.assertEqual(over, {})

    def test_the_size_table_pins_no_file(self):
        self.assertEqual(PINNED, {})


if __name__ == "__main__":
    unittest.main()
