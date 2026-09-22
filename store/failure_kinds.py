"""Conservative prefix-only classification of reasons written before schema 29."""

# Deliberately no substring matching or outcomeClass inference: unknown prose
# is evidence we cannot classify. Live writers supply their own kind.
PREFIXES = {
    'verify': ('verify failed',),
    'review_route': ('reviewer returned no verdict', 'reviewer route failed',
                     'terminal adjudication: MALFORMED;'),
    'fix_no_progress': ('fix round made no progress',),
    'no_commits': ('implementer made no commits', 'implementer made no new commits'),
    'budget': ('out of time:', 'implementer exceeded the ', 'fix round timed out'),
    'merge_lock': ('merge lock ',),
    'infra': ('implementer transport failure', 'worktree setup failed;',
              'git fetch origin failed before the cut:',
              'main diverged from origin/main:',
              'the board did not take the lease label', 'the board showed ',
              'ticket was not ready when the run', 'GitHub refused',
              'GitHub did not answer', 'GitHub answered', 'GitHub GraphQL ',
              'GitHub omitted the status-context ',
              'GitHub repeated the status-context ',
              'git push origin ', 'git fetch origin did not deliver ',
              'gh pr edit ', 'gh pr create ', 'gh api ', "no 'gh' on PATH and no ",
              'no `origin` remote to open the pull request',
              'cannot read OWNER/REPO off the origin URL;',
              'cannot read remote head for ', 'review ref changed during turn:',
              'worktree environment .env is tracked;', 'candidate contains .env ',
              'refusing working files containing ', 'cannot restore working files;',
              'cannot prepare working files:', 'cannot replace working files:',
              'container history is not a fast-forward;',
              'task branch changed during container turn;',
              'task worktree index is locked;', 'commit attribution cleanup',
              'media repository ', 'gh repo view failed',
              'ask adjudicator failed or returned an empty answer'),
    'swept': ('swept by the supervisor ',),
}


def backfill(conn):
    """Run once during migration, preserving reasons verbatim."""
    for kind, prefixes in PREFIXES.items():
        for prefix in prefixes:
            conn.execute(
                "UPDATE runs SET failureKind = ? WHERE outcome = 'failed'"
                " AND failureKind IS NULL AND substr(outcomeReason, 1, ?) = ?",
                (kind, len(prefix), prefix))
    conn.execute("UPDATE runs SET failureKind = 'unclassified'"
                 " WHERE outcome = 'failed' AND failureKind IS NULL")
