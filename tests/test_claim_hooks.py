"""Hook setup isolation and failure cleanup in real worktrees."""
from unittest.mock import patch

import holophyte.claim
from tests.fake_agent import _git
from tests.loop_fixture import BRANCH, LoopFixture, StubProvider, a_task


class WorktreeHooksTests(LoopFixture):
    def test_hooks_configuration_is_isolated_and_removed_with_worktree(self):
        self.git("config", "core.hooksPath", "shared-hooks")
        wt = self.target.parent / "hooks-worktree"
        self.git("worktree", "add", "--detach", str(wt))
        (wt / ".githooks").mkdir()
        self.assertTrue(holophyte.claim.run_worktree_setup(self.tgt, wt)[0])
        self.assertEqual(self.git("config", "--get", "core.hooksPath").strip(),
                         "shared-hooks")
        self.assertEqual(_git(wt, "config", "--get", "core.hooksPath").strip(),
                         ".githooks")
        self.git("worktree", "remove", "--force", str(wt))
        self.git("worktree", "add", "--detach", str(wt))
        self.assertEqual(_git(wt, "config", "--get", "core.hooksPath").strip(),
                         "shared-hooks")

    def test_hooks_config_failure_records_detail_and_discards_fresh_worktree(self):
        self.configure('[worktree]\nsetup = ["mkdir .githooks"]\n')
        provider = StubProvider(a_task())
        original = holophyte.claim.sh

        def fail_config(args, *a, **kw):
            if args[:2] == ["git", "config"]:
                raise RuntimeError("git config: cannot lock config")
            return original(args, *a, **kw)

        with patch.object(holophyte.claim, "sh", side_effect=fail_config):
            fake, _ = self.loop(provider=provider)
        self.assertEqual(fake.roles, [])
        self.assertNotIn(BRANCH, self.branches())
        self.assertFalse((self.worktrees / "ko-131-add-a-thing").exists())
        self.assertEqual(self.read("SELECT outcomeClass FROM runs"), [("infra",)])
        self.assertIn("worktree setup", provider.comments[0][1])
        self.assertIn("cannot lock config", provider.comments[0][1])
