"""Authored PR activity and event-backed guards for parked candidates (KO-563)."""
import json
import re

import store

COMMENT_FIELDS = 'id createdAt body author { login __typename }'
PAGE = 'pageInfo { hasPreviousPage startCursor }'
FIELDS = {
    'comments': COMMENT_FIELDS,
    'reviews': 'id submittedAt body author { login __typename }',
    'commits': ('commit { oid committedDate author { user { login } } '
                'committer { user { login } } statusCheckRollup { state } }'),
    'reviewThreads': ('id comments(last: 100) { ' + PAGE + ' nodes { '
                      + COMMENT_FIELDS + ' } }'),
}
LABELS = dict(comments='conversation comment', reviews='review',
              commits='commit', reviewThreads='inline thread')
ACTIVITY_FIELDS = '\n'.join(
    f'{name}(last: 100) {{ {PAGE} nodes {{ {fields} }} }}'
    for name, fields in FIELDS.items())
HEADER = re.compile(r'^---- Comment by [^\n]+', re.MULTILINE)


def _pages(target, pull, page, field, fields, read, *, thread=None):
    """Walk backwards, including old threads whose replies are new."""
    while True:
        yield from page.get('nodes') or ()
        info = page.get('pageInfo') or {}
        if not info.get('hasPreviousPage'):
            return
        selection = (f'{field}(last: 100, before: $before) {{ {PAGE} '
                     f'nodes {{ {fields} }} }}')
        if thread:
            query = ('query($id: ID!, $before: String) { node(id: $id) { '
                     '... on PullRequestReviewThread { ' + selection
                     + ' } } rateLimit { remaining resetAt } }')
            data = read(target, pull, query,
                        {'id': thread, 'before': info['startCursor']})
            page = data['node'][field]
        else:
            query = ('query($owner: String!, $name: String!, $number: Int!, '
                     '$before: String) { repository(owner: $owner, name: $name) { '
                     'pullRequest(number: $number) { ' + selection
                     + ' } } rateLimit { remaining resetAt } }')
            data = read(target, pull, query, dict(owner=pull.owner, name=pull.name,
                        number=pull.number, before=info['startCursor']))
            page = data['repository']['pullRequest'][field]


def _item(field, item, viewer):
    if field == 'commits':
        item = item.get('commit') or {}
        author = (item.get('author') or {}).get('user') or {}
        committer = (item.get('committer') or {}).get('user') or {}
        if viewer and viewer in (author.get('login'), committer.get('login')):
            return None
        return (LABELS[field], item.get('committedDate'), item.get('oid'))
    author = item.get('author') or {}
    if (viewer and author.get('login') == viewer
            and HEADER.search(item.get('body') or '')):
        return None
    # Creation/submission, never updatedAt: bot edits are not new comments.
    return (LABELS[field], item.get('submittedAt') if field == 'reviews'
            else item.get('createdAt'), item.get('id'))


def activities(target, pull, node, viewer, read, rate=None):
    """Collect content and update rate with the last overflow response."""
    def budgeted_read(*args):
        data = read(*args)
        if rate is not None and isinstance(data.get('rateLimit'), dict):
            rate.update(data['rateLimit'])
        return data

    result = []
    for field, fields in FIELDS.items():
        items = _pages(target, pull, node.get(field) or {},
                       field, fields, budgeted_read)
        for item in items:
            comments = (_pages(target, pull, item.get('comments') or {},
                        'comments', COMMENT_FIELDS, budgeted_read,
                        thread=item.get('id'))
                        if field == 'reviewThreads' else (item,))
            for comment in comments:
                activity = _item(field, comment, viewer)
                if activity and activity[1] and activity[2]:
                    result.append(activity)
    return tuple(result)


def latest(conn, run_id, kind):
    """Events survive a resume's new run id; scope to this ticket and PR."""
    if conn is None or run_id is None:
        return None
    row = conn.execute(
        'SELECT e.summary FROM runEvents e JOIN runs r ON r.id = e.runId '
        'JOIN runs current ON current.id = ? WHERE r.ticketId = current.ticketId '
        'AND r.prUrl IS current.prUrl AND e.kind = ? ORDER BY e.id DESC LIMIT 1',
        (run_id, kind)).fetchone()
    return row[0] if row else None


def record_pass(conn, run_id, status):
    """Count supervisor wakes whose advertised content disappeared before park."""
    previous = conn.execute(
        'SELECT r.id FROM runs r JOIN runs current ON current.id = ? '
        'WHERE r.ticketId = current.ticketId AND r.prUrl = current.prUrl '
        'AND r.id < current.id ORDER BY r.id DESC LIMIT 1', (run_id,)).fetchone()
    if not previous:
        return
    wake = conn.execute('SELECT summary FROM runEvents WHERE runId = ? '
                        "AND kind = 'pr_wake' ORDER BY id DESC LIMIT 1",
                        previous).fetchone()
    supervised = conn.execute('SELECT 1 FROM interventions WHERE runId = ? '
                              "AND action = 'babysit' AND source = 'supervisor'",
                              previous).fetchone()
    if not supervised:
        store.record_event(conn, run_id, "pr_empty_wakes", "0")
        return
    expected = json.loads(wake[0]) if wake else []
    found = any(tuple(item) in status.activity for item in expected)
    count = 0 if found else int(latest(conn, run_id, 'pr_empty_wakes') or 0) + 1
    store.record_event(conn, run_id, 'pr_empty_wakes', str(count))


def break_empty_wakes(conn, ticket):
    if int(latest(conn, ticket.runId, 'pr_empty_wakes') or 0) < 2:
        return
    reason = 'woken repeatedly with nothing new'
    with store.transaction(conn):
        parked = conn.execute(
            "SELECT 1 FROM runs r JOIN tickets t ON t.id = r.ticketId "
            "WHERE r.id = ? AND r.phase = 'awaiting_merge_approval' "
            "AND r.endedAt IS NULL AND t.status = 'blocked_on_operator' "
            "AND NOT EXISTS (SELECT 1 FROM runEvents WHERE runId = r.id "
            "AND kind = 'pr_wake_breaker')", (ticket.runId,)).fetchone()
        if not parked:
            return
        store.record_event(conn, ticket.runId, 'pr_wake_breaker', reason)
        conn.execute('UPDATE runs SET outcomeReason = ? WHERE id = ?',
                     (reason, ticket.runId))
        conn.execute('UPDATE tickets SET blockedQuestion = ? WHERE id = ?',
                     (reason, ticket.id))


def record_commits(conn, run_id, status):
    """Snapshot eligible commit identities; embedded dates are not push times."""
    store.record_event(conn, run_id, 'pr_seen_commits', json.dumps(
        [item[2] for item in status.activity if item[0] == 'commit']))


def arrived(conn, run_id, status, seen_at):
    previous = latest(conn, run_id, 'pr_seen_commits')
    seen = set(json.loads(previous)) if previous is not None else None
    result = [item for item in status.activity
              if (item[2] not in seen if item[0] == 'commit' and seen is not None
                  else item[1] > seen_at)]
    # Older parks have no identity snapshot: establish it without treating
    # their historical commits as new activity.
    if seen is None and not result:
        record_commits(conn, run_id, status)
    return result
