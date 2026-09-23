"""Fallback configuration is held to the same command contract as each seat."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_fixture import ConfigTestCase  # noqa: E402

from holophyte.config import check_config  # noqa: E402


class FallbackConfigTests(ConfigTestCase):
    def test_fallback_must_differ_from_primary(self):
        self.locate('[agents]\nimplementer = "echo ready"\n'
                    'implementer_fallback = "echo  ready"\n')
        with self.assertRaises(SystemExit) as error:
            check_config(self.project)
        self.assertIn('implementer_fallback', str(error.exception))
        self.assertIn('equal implementer', str(error.exception))

    def test_fallback_validation_and_review_model_exclusivity(self):
        for key in ('implementer_fallback', 'reviewer_fallback',
                    'adjudicator_fallback'):
            for value in ('42', '" "', '"./relative"'):
                with self.subTest(key=key, value=value):
                    self.locate(f'[agents]\n{key} = {value}\n')
                    with self.assertRaisesRegex(SystemExit, key):
                        check_config(self.project)
            self.locate(f'[agents]\n{key} = "echo ready"\n'
                        'review_model = "codex"\n')
            with self.assertRaisesRegex(SystemExit, 'review_model'):
                check_config(self.project)
