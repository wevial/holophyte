"""Persisted work clock: migration, work-only boundaries and sweep ownership."""
import inspect
import subprocess
from contextlib import ExitStack, nullcontext
from unittest.mock import patch

import store
import store.read
from holophyte import (
    agents,
    babysitter,
    claim,
    gates,
    loop,
    merge_gate,
    pr,
    pullrequest,
    supervisor,
)
from tests.sweep_fixture import MINUTE, T0, SweepTestCase, no_network


class WorkingTimeTests(SweepTestCase):
    def snapshot(self, run):
        return store.read.run_snapshot(self.conn, run)

    def test_working_clock_migration(self):
        old = self.a_run()
        store.record_review_round(self.conn, old, 1, 'pass', 'test',
                                  started_at=T0 + 100, ended_at=T0 + 200)
        rounds = store.read.rounds_of(self.conn, old)
        store.release(self.conn, old, 'failed', now=T0 + 900)
        before = store.read.run_detail(self.conn, old)
        # A version-18 database must migrate through the public open path.
        self.conn.execute('ALTER TABLE runs DROP COLUMN workingMs')
        self.conn.execute('ALTER TABLE runs DROP COLUMN workStartedAt')
        self.conn.execute('PRAGMA user_version = 18')
        self.conn.commit()
        self.conn.close()
        self.conn = store.open(str(self.db), migrate="owner")
        self.addCleanup(self.conn.close)
        historical = self.snapshot(old)
        self.assertEqual(self.conn.execute('PRAGMA user_version').fetchone()[0],
                         store.SCHEMA_VERSION)
        self.assertIsNone(historical.workingMs)
        self.assertIsNone(historical.workStartedAt)
        self.assertEqual(historical.startedAt, T0)
        self.assertEqual(historical.endedAt, T0 + 900)
        self.assertEqual(store.read.run_detail(self.conn, old).attempt, before.attempt)
        fresh = self.a_run()
        for row in (self.snapshot(fresh), store.read.run_detail(self.conn, fresh),
                    store.read.live_runs(self.conn, ['working'])[0]):
            self.assertEqual(row.workingMs, 0)
            self.assertIsNone(row.workStartedAt)
        ended = store.read.ended_runs(self.conn)[0]
        self.assertIsNone(ended.workingMs)
        self.assertEqual(ended.reviewRoundCount, 1)
        self.assertEqual(store.read.rounds_of(self.conn, old), rounds)
        from store.working import effective_work
        self.assertIsNone(effective_work(historical, T0 + 1000))

    def test_work_boundaries_exclude_waits(self):
        from store.working import effective_work
        self.exercise_controller_paths()
        run = self.a_run()
        now = [T0]
        total = 0

        def route(*args, **kwargs):
            start = now[0]
            self.assertEqual(self.snapshot(run).workStartedAt, start)
            now[0] += 37
            self.assertEqual(effective_work(self.snapshot(run), now[0]), total + 37)
            store.heartbeat(self.conn, run, now=now[0])
            if failure:
                raise failure
            return 0, 'done'

        with patch('store.working.time', lambda: now[0] / 1000), \
                patch.object(agents, 'run_capped', route), \
                patch.object(gates, 'run_capped', route), \
                patch.object(agents, 'agent_command', return_value=['script']), \
                patch.object(agents, 'publish_review_refs'), \
                patch.object(agents, 'check_review_refs'), \
                patch.object(agents, 'review_scratch',
                             lambda _: nullcontext(self.target)), \
                patch('holophyte.runs.heartbeat_while',
                      lambda *a, **k: nullcontext()), \
                patch.object(loop, 'heartbeat_while', lambda *a, **k: nullcontext()):
            for failure in (None, subprocess.TimeoutExpired('script', 1),
                            RuntimeError('route failed')):
                for path in ('initial', 'fix', 'conflict', 'PR-text',
                             'verify-initial', 'verify-final', 'verify-PR-fix',
                             'review', 'adjudicate', 'PR-thread-adjudicate'):
                    with self.subTest(path=path, failure=type(failure).__name__):
                        try:
                            if path.startswith('verify'):
                                gates.run_verify('echo done', self.target,
                                                 conn=self.conn, run_id=run)
                            elif path in ('review', 'adjudicate',
                                          'PR-thread-adjudicate'):
                                role = 'review' if path == 'review' else 'adjudicate'
                                agents.agent(self.tgt, role, 'goal', self.target,
                                             base_sha='base', candidate_sha='sha',
                                             conn=self.conn, run_id=run)
                            else:
                                loop._timed(self.tgt, self.conn, run, 100,
                                            self.target, 1, path)
                        except (RuntimeError, subprocess.TimeoutExpired):
                            if failure is None:
                                raise
                        total += 37
                        self.assertEqual(self.snapshot(run).workingMs, total)
                        self.assertIsNone(self.snapshot(run).workStartedAt)
                        # Polling/quiet/retry gaps and their heartbeats are idle.
                        now[0] += 100_000
                        store.heartbeat(self.conn, run, now=now[0])
                        self.assertEqual(
                            effective_work(self.snapshot(run), now[0]), total)

    def test_verify_time_is_split_from_agent_work(self):
        from store.working import agent_work, verify_work
        run = self.a_run()
        now = [T0]
        step = {'minutes': 0, 'open': None}

        def route(*args, **kwargs):
            now[0] += step['minutes'] * MINUTE
            if step['open'] is not None:
                snap = self.snapshot(run)
                step['open'].append((agent_work(snap, now[0]),
                                     verify_work(snap, now[0])))
            return 0, 'done'

        def role():
            agents.agent(self.tgt, 'review', 'goal', self.target,
                         base_sha='base', candidate_sha='sha',
                         conn=self.conn, run_id=run)

        def verify():
            gates.run_verify('echo done', self.target,
                             conn=self.conn, run_id=run)

        with patch('store.working.time', lambda: now[0] / 1000), \
                patch.object(agents, 'run_capped', route), \
                patch.object(gates, 'run_capped', route), \
                patch.object(agents, 'agent_command', return_value=['script']), \
                patch.object(agents, 'publish_review_refs'), \
                patch.object(agents, 'check_review_refs'), \
                patch.object(agents, 'review_scratch',
                             lambda _: nullcontext(self.target)):
            for call, minutes in ((role, 2), (verify, 3)):
                step['minutes'] = minutes
                call()
            snap = self.snapshot(run)
            self.assertEqual((snap.workingMs, snap.verifyMs),
                             (5 * MINUTE, 3 * MINUTE))
            self.assertEqual((agent_work(snap, now[0]), verify_work(snap, now[0])),
                             (2 * MINUTE, 3 * MINUTE))
            # One more open minute counts only to the open span's kind.
            step['minutes'] = 1
            for call, expected in ((verify, (2 * MINUTE, 4 * MINUTE)),
                                   (role, (3 * MINUTE, 4 * MINUTE))):
                step['open'] = []
                call()
                self.assertEqual(step['open'], [expected])

    def test_verify_split_migration(self):
        from store.working import agent_work, effective_work, verify_work
        live = self.a_run(active_work=True)
        self.conn.execute('UPDATE runs SET workingMs = ? WHERE id = ?',
                          (4 * MINUTE, live))
        # A store from the version before the split, opened by this build.
        self.conn.execute('ALTER TABLE runs DROP COLUMN verifyMs')
        self.conn.execute('ALTER TABLE runs DROP COLUMN verifyStartedAt')
        self.conn.execute(f'PRAGMA user_version = {store.SCHEMA_VERSION - 1}')
        self.conn.commit()
        self.conn.close()
        self.conn = store.open(str(self.db), migrate="owner")
        self.addCleanup(self.conn.close)
        self.assertEqual(self.conn.execute('PRAGMA user_version').fetchone()[0],
                         store.SCHEMA_VERSION)
        old = self.snapshot(live)
        now = T0 + 3 * MINUTE
        self.assertIsNone(old.verifyMs)
        self.assertIsNone(verify_work(old, now))
        self.assertEqual(agent_work(old, now), effective_work(old, now))
        self.assertEqual(agent_work(old, now), 7 * MINUTE)
        store.release(self.conn, live, 'failed', now=now)
        fresh = self.snapshot(self.a_run())
        self.assertEqual(fresh.verifyMs, 0)
        self.assertEqual(verify_work(fresh, now), 0)

    def exercise_controller_paths(self):
        """Drive the named callers through real role/verify boundaries."""
        run = self.a_run()
        now = [T0]
        responses = []
        calls = []
        pull = pr.PullRequest('example.invalid', 'owner', 'repo', 1, 'pull-url')
        thread = pr.Thread('thread', 'code.py', 1, 'bot', 'fix this', 'url',
                           author_kind='bot')
        values = dict(target=self.tgt, conn=self.conn, run_id=run, provider=None,
                      task_id='KO-1', issue_id='issue-1', task='task', branch='task',
                      wt=self.target, fresh=True, beat_s=100, start_sha='base',
                      base_sha='base', sha='before', ticket='ticket',
                      verify_cmd='echo done', budget_min=25, contracts=[],
                      criteria=[], cap=1, conflicts=['code.py'], body='body',
                      started=0, issue_url=None, sync_main=False, pull=pull,
                      state=pr.PrState((thread,), 'success', 'before'), rnd=1,
                      pass_no=1, model='test', addressed=[], reviewed='before',
                      review_follows=True)

        def route(*args, **kwargs):
            self.assertIsNotNone(self.snapshot(run).workStartedAt)
            now[0] += 10
            calls.append('work')
            return 0, responses.pop(0) if responses else 'done'

        def nap(seconds):
            self.assertIsNone(self.snapshot(run).workStartedAt)
            now[0] += int(seconds * 1000)
            store.heartbeat(self.conn, run, now=now[0])

        with ExitStack() as stack:
            stack.enter_context(patch('store.working.time', lambda: now[0] / 1000))
            stack.enter_context(patch.object(agents, 'run_capped', route))
            stack.enter_context(patch.object(gates, 'run_capped', route))
            stack.enter_context(patch.object(agents, 'review_scratch',
                                             lambda _: nullcontext(self.target)))
            stack.enter_context(patch.object(agents, 'agent_command',
                                             return_value=['script']))
            stack.enter_context(patch.object(agents, 'publish_review_refs'))
            stack.enter_context(patch.object(agents, 'check_review_refs'))
            for module in (loop, babysitter, merge_gate, claim, pullrequest):
                for name, result in (('sh', 'after'),
                                     ('main_merge_base', 'after'),
                                     ('ledger', None),
                                     ('record_round', None),
                                     ('merge_conflicts', []),
                                     ('scope_files', []),
                                     ('scope_brief', '')):
                    if hasattr(module, name):
                        stack.enter_context(patch.object(module, name,
                                                         return_value=result))
                if hasattr(module, 'heartbeat_while'):
                    stack.enter_context(patch.object(
                        module, 'heartbeat_while', lambda *a, **k: nullcontext()))
            stack.enter_context(patch('holophyte.runs.heartbeat_while',
                                     lambda *a, **k: nullcontext()))
            for module, name, result in (
                    (loop, '_check_run_cap', None),
                    (loop, '_candidate_drift', ''),
                    (merge_gate, '_is_ancestor', True),
                    (merge_gate, 'merge_drift', []),
                    (babysitter, '_decline_threads', ()),
                    (pr, 'push_branch', None)):
                stack.enter_context(patch.object(module, name, return_value=result))
            stack.enter_context(patch.object(pullrequest, 'monotonic', return_value=0))
            scenarios = (
                (loop._implement, 'working', ['done'], 1),
                (loop._review_rounds, 'working',
                 ['done', 'VERDICT: REQUEST_CHANGES', 'fixed'], 3),
                (claim._resolve_merge_conflict, 'merge_gate', ['fixed'], 1),
                (pullrequest._written_pr_text, 'merge_gate',
                 ['TITLE: change\nDescription'], 1),
                (merge_gate._merge_gate, 'merge_gate', ['done'], 1),
                (loop._terminal_adjudication, 'addressing',
                 ['done', 'VERDICT: PASS'], 2),
                (babysitter._answer_threads, 'merge_gate',
                 ['THREAD 1: DECLINE: not a blocker'], 1),
                (babysitter._fix_threads, 'merge_gate', ['fixed', 'done'], 2),
            )
            for function, phase, outputs, expected_calls in scenarios:
                with self.subTest(controller=function.__name__):
                    # These are independent controller calls, not one loop path.
                    run = self.a_run(phase=phase)
                    values['run_id'] = run
                    responses[:] = outputs
                    before = len(calls)
                    kwargs = {name: values[name] for name in
                              inspect.signature(function).parameters
                              if name in values}
                    function(**kwargs)
                    self.assertEqual(len(calls) - before, expected_calls)
                    self.assertEqual(
                        self.snapshot(run).workingMs, (len(calls) - before) * 10)
                    self.assertFalse(responses)
            # The real retry loop and PR pending/quiet loops advance wall time.
            stack.enter_context(patch.object(loop, 'sleep', nap))
            stack.enter_context(patch.object(loop, 'retry_clock',
                                             lambda: now[0] / 1000))
            with patch.object(agents, 'transport_failure', return_value=None):
                # loop imported this function; script just its diagnosis.
                with patch.object(loop, 'transport_failure',
                                  side_effect=['ECONNRESET', None]):
                    loop._transport_timed(self.tgt, self.conn, run, 100,
                                          self.target, 25, 'retry')
            stack.enter_context(patch.object(pr, 'SLEEP', nap))
            pending = pr.PrState((), 'pending', 'after')
            quiet = pr.PrState((), 'success', 'after', updated_at=now[0])
            with patch.object(babysitter.pr_status, 'pr_state',
                              side_effect=[pending, quiet, quiet]), \
                    patch.object(babysitter, '_quiet_left', side_effect=[1000, 0]):
                babysitter._settled_state(self.tgt, self.conn, run, 100, pull)
            self.assertEqual(self.snapshot(run).workingMs, (len(calls) - before) * 10)
            self.assertGreater(now[0] - T0, 30_000)

    def test_work_accounting_ownership_race(self):
        from store.working import effective_work, settle_work, working
        run = self.a_run()
        now = [T0]
        with patch('store.working.time', lambda: now[0] / 1000):
            with working(self.conn, run):
                now[0] += 20
                with working(self.conn, run):
                    now[0] += 30
                self.assertEqual(self.snapshot(run).workingMs, 0)
                self.assertEqual(effective_work(self.snapshot(run), now[0]), 50)
            settle_work(self.conn, run, now=now[0] + 500)
            self.assertEqual(self.snapshot(run).workingMs, 50)
            foreign = store.open(str(self.db), migrate="owner")
            try:
                with working(self.conn, run):
                    with self.assertRaises(store.ClaimConflict):
                        with working(foreign, run):
                            self.fail('another owner took an active interval')
            finally:
                foreign.close()
            with working(self.conn, run):
                now[0] = T0 + 6 * MINUTE
                supervisor.sweep(self.tgt, self.conn, now[0])
                now[0] = T0 + 12 * MINUTE
                other = store.open(str(self.db), migrate="owner")
                try:
                    with no_network():
                        result = supervisor.sweep(self.tgt, other, now[0], act=True)
                    self.assertTrue(result.outcomes[0].acted)
                    swept = store.read.run_detail(other, run)
                finally:
                    other.close()
                now[0] += 999
            self.assertEqual(store.read.run_detail(self.conn, run), swept)
            self.assertEqual(swept.workingMs, 12 * MINUTE)
            self.assertIsNone(swept.workStartedAt)
            with self.assertRaises(store.RunEnded):
                with working(self.conn, run):
                    self.fail('ended run dispatched work')
            self.assertIsNone(store.read.ticket_by_id(
                self.conn, self.ticket_of[run]).activeRunId)
