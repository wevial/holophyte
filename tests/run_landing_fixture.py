"""End-to-end observations shared by the three landing-path witnesses."""
import io
from dataclasses import FrozenInstanceError
from unittest.mock import patch

import holophyte.claim
import holophyte.operator
import holophyte.run
from tests.fake_agent import APPROVE, Commit, Idle
from tests.loop_fixture import BRANCH


def landing_path(case, mode):
    """Capture the claim and landing values around real gates and merges."""
    claims, landed = [], []
    build, land = holophyte.claim.claimed_run, holophyte.run.land

    def claim(*args, **kwargs):
        run = build(*args, **kwargs, clock=lambda: 100.0)
        claims.append(run)
        return run

    def capture(run, verify):
        claimed = claims[-1]
        for name in ("target", "conn", "provider", "run_id", "task_id", "issue_id",
                     "task", "branch", "wt", "started", "started_at", "budget_min"):
            case.assertEqual(getattr(run, name), getattr(claimed, name), name)
        row = run.conn.execute(
            'SELECT branch, startedAt, timeBoxMs FROM runs WHERE id = ?',
            (run.run_id,)).fetchone()
        case.assertEqual(row, (BRANCH, run.started_at, run.budget_min * 60000))
        case.assertEqual(run.wt, case.worktrees / 'ko-131-add-a-thing')
        if run.pr_url is None:
            case.assertEqual(run.sha, case.git("rev-parse", BRANCH).strip())
        case.assertIsNone(claimed.sha)
        case.assertEqual(claimed.rnd, 0)
        with case.assertRaises(FrozenInstanceError):
            run.sha = 'changed'
        landed.append(run)
        return land(run, verify)

    if mode == 'pr':
        case.configure('[merge]\nmode = "pr"\npr_quiet_sec = 0\n')
        case.fake_route(states=[case.pr_state()])
    else:
        case.configure('[merge]\napprove = "' + ('human' if mode == 'approved'
                       else 'auto') + '"\n'
                       'after = ["git log -1 --format=%B > after.txt"]\n')
    with patch.object(holophyte.claim, 'claimed_run', side_effect=claim), \
            patch.object(holophyte.run, 'land', side_effect=capture):
        script = [Commit('candidate'), APPROVE]
        if mode == 'pr':
            script.append(Idle(''))
        case.loop(*script, provider=case.provider())
        if mode == 'approved':
            holophyte.operator.approve(case.tgt, 'KO-131', 'ok', out=io.StringIO())
            case.loop(provider=case.provider())
    case.assertEqual(len(landed), 1)
    run = landed[0]
    case.assertEqual(run.rnd, 0 if mode == 'approved' else 1)
    if mode == 'pr':
        case.assertEqual((run.pr_url, run.merge_sha), (case.URL, case.MERGE_SHA))
    else:
        # Exact main-era output, independently specified (not rendered by land).
        expected = (f'MERGED to main (branch {BRANCH} deleted). Verify: passed.\n'
                    f'actual: 0.0 min · estimate: 5 min · rounds: {run.rnd}')
        case.assertEqual(case.read("SELECT text FROM ledger WHERE kind = 'merge'"),
                         [(expected,)])
        case.assertEqual(case.git('log', '-1', '--format=%B').strip(),
                         f'Merge {BRANCH}: add a thing')
        case.assertEqual((case.target / 'after.txt').read_text(),
                         f'Merge {BRANCH}: add a thing\n\n')
        case.assertEqual(len(case.git('rev-list', '--parents', '-n', '1', 'HEAD')
                             .split()), 3)
