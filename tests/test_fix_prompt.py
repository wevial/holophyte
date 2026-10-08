"""A fix turn's prompt keeps the branch fast-forward only.

Run: python3 -m unittest discover -s tests -p 'test_fix_prompt.py' -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import holophyte.config.project  # noqa: E402
from holophyte.agents.fix_session import fix_turn  # noqa: E402

VERDICT = ("VERDICT: REQUEST_CHANGES\n"
           "1. Squash trim commit d5cb8a8d with its restoration.")


class FixPromptTests(unittest.TestCase):
    def test_the_address_prompt_forbids_history_rewrites_and_declines_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = holophyte.config.project.Project(
                path=root / "repo", holo_dir=root, store_path=root / "store.db",
                config_path=root / "config.toml", worktrees=root / "worktrees")
            prompts = []

            def timed(*args, argv=None):
                prompts.append(args[-1])
                return "done", False

            fix_turn(target, None, 1, 0, root, 10, "TICKET BODY", VERDICT,
                     "abc123", timed=timed, check_cap=None)
        [prompt] = prompts
        prompt = " ".join(prompt.split())
        self.assertIn(VERDICT.split("\n")[1], prompt)
        self.assertIn("Never amend, rebase or squash commits already on the"
                      " branch: the factory only fast-forwards it.", prompt)
        self.assertIn("A finding that asks for that is DECLINE for that reason;"
                      " fix anything still wrong at HEAD in a new commit.",
                      prompt)


if __name__ == "__main__":
    unittest.main()
