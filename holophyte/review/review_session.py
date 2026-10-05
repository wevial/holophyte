# Session files for configured reviewers; never shares implementer state.
import json

import store
from holophyte.agents.harness import seat as harness_seat
from holophyte.agents.session_arms import select_arm
from holophyte.config.config_tables import loop_config


def record_session(scratch, conn, run_id, role, route, round_number):
    # Read before scratch cleanup; invalid or absent wrapper output is harmless.
    if (role != 'review' or round_number is None
            or conn is None or run_id is None):
        return
    try:
        with (scratch / 'session').open(encoding='utf-8') as source:
            session = source.read(201)
    except (OSError, UnicodeError):
        return
    record_review_session(conn, run_id, session, role, route, round_number)


def record_review_session(conn, run_id, session, role, route, round_number):
    if (not session or len(session) > 200 or '\x00' in session
            or any(c.isspace() for c in session)):
        return
    store.record_event(conn, run_id, 'agent_session', 'review session recorded',
                       level='detail', payload=json.dumps({
                           'session_id': session, 'role': role, 'route': route,
                           'round': round_number}))


def first_session(conn, run_id):
    rows = conn.execute(
        "SELECT payload FROM runEvents WHERE runId=? AND kind='agent_session' "
        "ORDER BY seq DESC", (run_id,))
    for (payload,) in rows:
        event = json.loads(payload)
        if (event.get('role') == 'review' and event.get('route') == 'primary'
                and event.get('round') == 1):
            return event['session_id']
    return None


def resumable(target):
    seat = harness_seat(target, 'review')
    return seat is None or seat.adapter.resumes


def prepare_environment(target, env, conn, run_id, role, route, round_number):
    env.pop('HOLOPHYTE_REVIEW_RESUME', None)
    mode = loop_config(target).review_session
    if (role != 'review' or round_number is None or round_number < 2
            or mode == 'fresh' or conn is None or run_id is None):
        return
    arm = select_arm(mode, run_id)
    session = None
    if route == 'primary' and not resumable(target):
        reason = 'harness cannot resume'
    elif arm == 'fresh':
        reason = 'fresh arm'
    elif route == 'fallback':
        reason = 'fallback reviewer route'
    else:
        session = first_session(conn, run_id)
        reason = None if session else 'no recorded session'
    if session:
        env['HOLOPHYTE_REVIEW_RESUME'] = session
    payload = {'arm': arm, 'requested': session is not None}
    if reason:
        payload['reason'] = reason
    store.record_event(conn, run_id, 'review_session', f'review session: {arm}',
                       level='detail', payload=json.dumps(payload))
