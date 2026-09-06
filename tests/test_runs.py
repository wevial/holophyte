"""`holophyte.runs.review_round_cap()`: the review-round cap a candidate
earns from its size and the `[loop]` review keys (KO-299).

Run: python3 -m unittest discover -s tests -p 'test_runs*' -v
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import holophyte.config  # noqa: E402 - after the sys.path insert above
import holophyte.runs  # noqa: E402 - after the sys.path insert above


def a_config(**overrides):
    """A `LoopConfig` over the defaults, with the review keys overridden."""
    values = dict(holophyte.config.LOOP_KEYS)
    values.update(overrides)
    return holophyte.config.LoopConfig(**values)


class ReviewRoundCapTests(unittest.TestCase):

    def test_scales_with_lines_and_caps(self):
        """Base 2, one more round per 800 changed lines, four at most: a
        300-line candidate keeps the base, a 1,700-line one earns the two
        extra rounds that reach the ceiling, and a 9,000-line one stops at
        the ceiling. With `review_rounds_per_lines = 0` nothing scales."""
        cfg = a_config(review_rounds=2, review_rounds_per_lines=800,
                       review_rounds_max=4)

        self.assertEqual(
            [holophyte.runs.review_round_cap(n, cfg) for n in (300, 1700, 9000)],
            [2, 4, 4])

        flat = a_config(review_rounds=2, review_rounds_per_lines=0,
                        review_rounds_max=4)
        self.assertEqual(
            [holophyte.runs.review_round_cap(n, flat) for n in (0, 300, 9000)],
            [2, 2, 2])

    def test_the_default_config_keeps_the_two_round_base(self):
        """`MAX_ROUNDS` is the base's default: an unconfigured target still
        pays two rounds for a small change."""
        self.assertEqual(holophyte.config.LOOP_KEYS["review_rounds"],
                         holophyte.runs.MAX_ROUNDS)
        self.assertEqual(holophyte.runs.review_round_cap(1, a_config()), 2)


if __name__ == "__main__":
    unittest.main()
