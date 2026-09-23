"""Failure reasons witnessed through the real loop with scripted agents."""
import json
import unittest
from unittest.mock import patch

import holophyte.gates
import holophyte.pr
from tests.fake_agent import (
    APPROVE,
    REQUEST_CHANGES,
    Commit,
    Idle,
    Reply,
    block_until_killed,
)
from tests.loop_fixture import LoopFixture, MergeModeFixture, StubProvider, a_task


class TimedOut(Idle):
    def play(self, cwd, turn):
        block_until_killed(cwd, "still fixing", timeout=0.05)


class FailureReasonTests(LoopFixture):
    def reason(self):
        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertEqual(len(reason.splitlines()), 1)
        self.assertLessEqual(len(reason), 400)
        return reason

    def fail_verify(self):
        task = a_task()
        task['verify'] = "echo first\nsh -c 'echo boom; exit 3'"
        self.loop(Commit(), REQUEST_CHANGES, Commit(), REQUEST_CHANGES,
                  Commit(), provider=StubProvider(task))
        return self.reason()

    def test_second_verify_command(self):
        reason = self.fail_verify()
        for text in ("command 2", "sh -c 'echo boom; exit 3'", "exit 3", "boom"):
            self.assertIn(text, reason)

    def test_terminal_criteria(self):
        task = a_task()
        task['criteria'] = ['First behavior', 'Second behavior',
                            'Third behavior', 'Fourth behavior']
        verdict = Reply("CRITERION 1: met — check passed\n"
                        "CRITERION 2: not met — broken\n"
                        "CRITERION 3: met — check passed\n"
                        "CRITERION 4: unwitnessed — missing test\nVERDICT: FAIL")
        self.loop(Commit(), REQUEST_CHANGES, Commit(), REQUEST_CHANGES,
                  Commit(), verdict, provider=StubProvider(task))
        reason = self.reason()
        for text in ('criterion 2', 'Second behavior',
                     'criterion 4', 'Fourth behavior'):
            self.assertIn(text, reason)
        ((payload,),) = self.read(
            "SELECT payload FROM runEvents WHERE kind = 'failure'")
        self.assertEqual(json.loads(payload)['criteria'], [
            {'number': 2, 'status': 'not met', 'text': 'Second behavior'},
            {'number': 4, 'status': 'unwitnessed', 'text': 'Fourth behavior'},
        ])

    def test_timed_out_findings(self):
        verdict = Reply('- First defect\n- Second defect\n- Third defect\n'
                        'CRITERION 1: met — check passed\nVERDICT: REQUEST_CHANGES')
        self.loop(Commit(), verdict, TimedOut())
        reason = self.reason()
        self.assertIn('3 findings open', reason)
        self.assertIn('First defect', reason)
        ((payload,),) = self.read(
            "SELECT payload FROM runEvents WHERE kind = 'failure'")
        facts = json.loads(payload)
        self.assertEqual(facts['open_count'], 3)
        self.assertEqual(facts['first_title'], '- First defect')
        self.assertTrue(facts['timed_out'])


class ComposerTests(unittest.TestCase):
    def test_long_multiline_facts_keep_command_status_and_bound(self):
        from holophyte.failure_reason import compose
        reason = compose('verify', command_index=2, command='x' * 130,
                         exit_status=3, last_output_line='boom\n' + 'y' * 500)
        self.assertLessEqual(len(reason), 400)
        self.assertNotIn('\n', reason)
        self.assertIn('x' * 120, reason)
        self.assertNotIn('x' * 121, reason)
        self.assertIn('exit 3', reason)


class BabysitterFailureTests(MergeModeFixture):
    def verify_failure(self, approve, before_review):
        self.configure(f'[merge]\nmode = "pr"\napprove = "{approve}"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT]), self.pr_state()])
        marker = self.db.parent / 'fail-verify'
        command = f"sh -c 'if test -f {marker}; then echo boom; exit 3; fi'"
        task = dict(a_task(), body=self.BODY, verify=f'echo first\n{command}')
        push = holophyte.pr.push_branch
        pushes = []

        def push_then_fail(*args, **kwargs):
            result = push(*args, **kwargs)
            pushes.append(result)
            if len(pushes) == 2:
                marker.touch()
                holophyte.gates._PASSES.clear()  # a re-exec (KO-646)
            return result

        class BreakVerify(Commit):
            def play(self, cwd, turn):
                result = super().play(cwd, turn)
                if not before_review:
                    marker.touch()
                return result

        with patch.object(holophyte.pr, 'push_branch', push_then_fail):
            self.loop(Commit(), APPROVE, Idle(''),
                      Reply('THREAD 1: ADDRESS -- a real crash'),
                      BreakVerify(), provider=StubProvider(task))
        ((summary, payload),) = self.read(
            "SELECT summary, payload FROM runEvents WHERE kind = 'failure'")
        self.assertIn('command 2', summary)
        self.assertIn('exit 3; boom', summary)
        self.assertEqual(json.loads(payload)['command'], command)
        return summary

    def test_before_human_approval_still_parks(self):
        reason = self.verify_failure('human', True)
        self.assertIn('before human approval', reason)
        self.assertEqual(self.read('SELECT phase, outcome FROM runs'),
                         [('awaiting_merge_approval', None)])

    def test_before_review_of_fix_parks(self):
        reason = self.verify_failure('auto', True)
        self.assertIn('before the review of the fix', reason)
        self.assertEqual(self.read('SELECT phase, outcome FROM runs'),
                         [('awaiting_merge_approval', None)])

    def test_after_fix_round(self):
        reason = self.verify_failure('auto', False)
        self.assertIn('after the fix round', reason)
        self.assertEqual(self.read('SELECT outcomeReason, failureKind FROM runs'),
                         [(reason, 'verify')])

    def test_timed_out_thread_fix(self):
        self.configure('[merge]\nmode = "pr"\napprove = "auto"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT] * 3)])
        self.loop(Commit(), APPROVE, Idle(''),
                  Reply('THREAD 1: ADDRESS -- broken\nTHREAD 2: ADDRESS -- broken\n'
                        'THREAD 3: ADDRESS -- broken'),
                  TimedOut(), provider=self.provider())
        ((reason,),) = self.read('SELECT outcomeReason FROM runs')
        self.assertIn('3 findings open', reason)
        self.assertIn(self.DEFECT[3].splitlines()[0], reason)


class VerifyFactsTests(unittest.TestCase):
    def test_blocks_preserve_shell_state_and_stop_on_failure(self):
        from holophyte.gates import run_verify
        for command, expected in (
                ('export ANSWER=42\n[ "$ANSWER" = 42 ]', True),
                ('false\n[ "$?" = 1 ]', False),
                ('false\ntrue', False),
                ('if true; then\necho yes\nfi', True)):
            with self.subTest(command=command):
                ok, output = run_verify(command, '.')
                self.assertEqual(ok, expected, output)
        ok, output = run_verify('echo first\necho boom && exit 3', '.')
        self.assertFalse(ok)
        self.assertEqual(output.failure, {
            'command_index': 2, 'command': 'echo boom && exit 3',
            'exit_status': 3, 'last_output_line': 'boom',
        })
