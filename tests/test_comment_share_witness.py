"""Witness: the in-scope comment and docstring share is at or under the target, citing no ticket."""
import unittest

from tests.test_comment_budget import TARGET, measure


class CommentShareWitness(unittest.TestCase):

    def test_the_in_scope_share_is_at_or_under_the_target(self):
        modules = measure().values()
        count = sum(m.count for m in modules)
        size = sum(m.size for m in modules)
        self.assertLessEqual(count / size, TARGET,
                             f"{count} comment and docstring lines in {size}")

    def test_no_comment_or_docstring_line_cites_a_ticket(self):
        cited = {name: len(m.cited) for name, m in measure().items() if m.cited}
        self.assertEqual(cited, {})


if __name__ == "__main__":
    unittest.main()
