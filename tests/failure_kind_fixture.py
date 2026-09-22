"""Failure categories witnessed through actual loop close-out."""
from unittest.mock import patch

from fake_agent import MALFORMED, REQUEST_CHANGES, Commit, Idle
from loop_fixture import CommitThenTimeout, StubProvider, a_task

from holophyte.gates import MergeLockHeld


class FailureKindCases:
    def test_failure_kinds_from_loop_paths(self):
        cases = [
            ((Commit(), MALFORMED, MALFORMED), 'review_route',
             'reviewer returned no verdict line twice'),
            ((Commit(), REQUEST_CHANGES, Idle()), 'fix_no_progress',
             'fix round made no progress'),
            ((Idle(),), 'no_commits', 'implementer made no commits'),
            ((CommitThenTimeout(),), 'budget', 'implementer exceeded the '),
        ]
        for index, (turns, kind, prefix) in enumerate(cases):
            with self.subTest(kind=kind):
                if index:
                    self.tearDown()
                    self.doCleanups()
                    self.setUp()
                self.loop(*turns)
                ((actual, reason),) = self.read(
                    'SELECT failureKind, outcomeReason FROM runs')
                self.assertEqual(actual, kind)
                self.assertTrue(reason.startswith(prefix), reason)

    def test_merge_lock_kind_survives_dispatch(self):
        reason = 'merge lock held by run 9; waited 240s'
        with patch('holophyte.loop.run_task', side_effect=MergeLockHeld(reason)):
            self.loop()
        self.assertEqual(self.read('SELECT failureKind, outcomeReason FROM runs'),
                         [('merge_lock', reason)])

    def test_verify_kind_survives_builder_and_dispatch(self):
        task = dict(a_task(), verify="echo first\nsh -c 'echo boom; exit 3'")
        self.loop(Commit(), REQUEST_CHANGES, Commit(), REQUEST_CHANGES, Commit(),
                  provider=StubProvider(task))
        ((kind, reason),) = self.read('SELECT failureKind, outcomeReason FROM runs')
        self.assertEqual(kind, 'verify')
        self.assertIn("verify failed: command 2 [sh -c 'echo boom; exit 3'], exit 3",
                      reason)
