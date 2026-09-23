"""Optional experiment for retaining implementer context between review rounds."""
import json
import shlex

import store
from holophyte.agents import effective_role, routes
from holophyte.config import check_command_path, config_table, loop_config
from holophyte.gates import sh
from holophyte.harness import seat as harness_seat
from holophyte.session_arms import select_arm


def resume_template(target):
    """Validate once at startup; substitute only after splitting shell words."""
    value = config_table(target, 'agents').get('implementer_resume')
    if value is None:
        return None
    key = f'[holo2] {target.config_path}: [agents] implementer_resume'
    if not isinstance(value, str) or '{session}' not in value:
        raise SystemExit(f'{key} must be a command string containing {{session}}')
    try:
        argv = shlex.split(value)
    except ValueError as exc:
        raise SystemExit(f'{key}: {exc}') from exc
    check_command_path(target, 'implementer_resume', argv[0])
    return argv


def resume_argv(target, conn, run_id):
    """Return safe resume argv or the reason this turn must start fresh.

    A table-form implementer's adapter builds the argv; a command string
    goes through its `implementer_resume` template."""
    role = effective_role(target, 'implement')
    if role in routes(target).commands:
        return None, 'fallback implementer route'
    row = (conn.execute('SELECT providerSessionId FROM runs WHERE id = ?',
                        (run_id,)).fetchone() if conn is not None else None)
    if row is None or not row[0]:
        return None, 'no recorded session'
    seat = harness_seat(target, 'implement')
    if seat is not None:
        return seat.resume(row[0]), None
    template = resume_template(target)
    if template is None:
        return None, 'no implementer_resume template'
    return [arg.replace('{session}', row[0]) for arg in template], None


def fix_turn(target, conn, run_id, beat_s, wt, budget_min, ticket, verdict, sha,
             *, timed, check_cap):
    """Use the common timed dispatcher, retrying a failed resume at most once."""
    findings = (f'Reviewer findings:\n\n{verdict}\n\n'
        'For EACH finding, adjudicate it first: ADDRESS (concrete '
        'blocker — fix now), FOLLOW_UP (valid but out of scope — name '
        'it in the commit message), or DECLINE (invalid/out-of-scope — '
        'state the rationale in the commit message). Then fix only the '
        'ADDRESS items and commit.')
    fresh = ('A reviewer left findings on your work. The ticket you '
             'are held to, acceptance criteria included:\n\n'
             f'{ticket}\n\n' + findings)
    arm = select_arm(loop_config(target).fix_session, run_id)
    argv, reason = (resume_argv(target, conn, run_id)
                    if arm == 'resume' else (None, None))
    args = (target, conn, run_id, beat_s, wt, budget_min)
    retry = False
    if argv is None:
        output, timed_out = timed(*args, fresh)
    else:
        try:
            output, timed_out = timed(*args, findings, argv=argv)
            code = getattr(output, 'exit_code', 0)
            if timed_out or code:
                reason = 'resume timed out' if timed_out else f'resume exited {code}'
                retry = (not timed_out
                         and sh(['git', 'rev-parse', 'HEAD'], cwd=wt) == sha)
        except OSError:
            output, timed_out = '', False
            reason, retry = 'resume launch failed', True
    if loop_config(target).fix_session != 'fresh' and conn is not None:
        payload = {'arm': arm, 'resumed': argv is not None and reason is None}
        if reason:
            payload['reason'] = reason
        store.record_event(conn, run_id, 'fix_session', f'fix session: {arm}',
                           level='detail', payload=json.dumps(payload))
    if retry:
        check_cap(target, conn, run_id, budget_min, sha)
        output, timed_out = timed(*args, fresh)
    return output, timed_out
