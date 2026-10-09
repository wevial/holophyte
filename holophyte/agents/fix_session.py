import json
import shlex

import store
from holophyte.agents.agent_routes import routes
from holophyte.agents.harness import seat as harness_seat
from holophyte.agents.roles import effective_role
from holophyte.agents.session_arms import select_arm
from holophyte.config.checks import check_command_path
from holophyte.config.config_tables import loop_config
from holophyte.config.reader import config_table
from holophyte.loop.follow_ups import capture as capture_follow_ups
from holophyte.loop.gates import sh


def resume_template(target):
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
    # Substituted after splitting, so a session id cannot add shell words.
    return [arg.replace('{session}', row[0]) for arg in template], None


def fix_turn(target, conn, run_id, beat_s, wt, budget_min, ticket, verdict, sha,
             *, timed, check_cap):
    findings = (f'Reviewer findings:\n\n{verdict}\n\n'
        'For EACH finding, adjudicate it first: ADDRESS (concrete '
        'blocker — fix now), FOLLOW_UP (valid but out of scope — name '
        'it in the commit message), or DECLINE (invalid/out-of-scope — '
        'state the rationale in the commit message). Then fix only the '
        'ADDRESS items and commit. Never amend, rebase or squash commits '
        'already on the branch: the factory only fast-forwards it. A '
        'finding that asks for that is DECLINE for that reason; fix '
        'anything still wrong at HEAD in a new commit.\n\n'
        'Write each FOLLOW_UP as one unwrapped line of the commit message, '
        'in one of two forms:\n'
        'FOLLOW_UP(feature): TEXT @ PATH:LINE\n'
        'FOLLOW_UP(guardrail): TEXT @ PATH:LINE\n'
        'feature is a capability or behavior change for a later ticket; '
        'guardrail is a check, test, prompt rule or review rule that would '
        'catch this class of problem next time. The " @ PATH:LINE" tail is '
        'optional, and so is ":LINE" within it.')
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
    if conn is not None:
        capture_follow_ups(conn, run_id, wt, sha)
    return output, timed_out
