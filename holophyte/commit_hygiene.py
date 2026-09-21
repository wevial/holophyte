"""Reword unpublished commits without checking out or modifying their trees."""
import re
import subprocess

from holophyte.config import merge_config
from holophyte.gates import InfraFailure


def _git(wt, *args, data=None):
    try:
        result = subprocess.run(
            ['git', '-c', 'core.hooksPath=/dev/null', *args], cwd=wt,
            input=data, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InfraFailure(f'commit attribution cleanup: git {args[0]}: {exc}') from exc
    if result.returncode:
        detail = result.stderr.decode(errors='replace').strip()
        raise InfraFailure(f'commit attribution cleanup: git {args[0]}: {detail}')
    return result.stdout


def _message(message, patterns):
    # Keep bytes (including the encoding) intact for every retained line.
    lines = message.splitlines(keepends=True)
    kept = []
    removed = False
    for line in lines:
        text = line.decode('utf-8', errors='surrogateescape')
        if any(p.search(text) for p in patterns):
            removed = True
            if kept and not kept[-1].strip():
                kept.pop()
        else:
            kept.append(line)
    if not removed:
        return message
    while kept and not kept[-1].strip():
        kept.pop()
    return b''.join(kept)


def _reword(wt, sha, rewritten, patterns):
    original = _git(wt, 'cat-file', 'commit', sha)
    headers, message = original.split(b'\n\n', 1)
    lines = headers.split(b'\n')
    updated = []
    for line in lines:
        if line.startswith(b'parent '):
            parent = line[7:].decode('ascii')
            line = b'parent ' + rewritten.get(parent, parent).encode('ascii')
        updated.append(line)
    cleaned = _message(message, patterns)
    if updated == lines and cleaned == message:
        return sha
    # A signature covers the original object and cannot survive a reword.
    valid = []
    signature = False
    for line in updated:
        if not line.startswith(b' '):
            signature = line.startswith((b'gpgsig ', b'gpgsig-sha256 '))
        if not signature:
            valid.append(line)
    data = b'\n'.join(valid) + b'\n\n' + cleaned
    return _git(wt, 'hash-object', '-t', 'commit', '-w', '--stdin',
                data=data).decode().strip()


def _unpublished(wt, tip):
    published = _git(wt, 'for-each-ref', '--format=%(objectname)',
                     'refs/remotes/origin/').decode().splitlines()
    return _git(wt, 'rev-list', '--reverse', '--topo-order', tip,
                '--not', 'refs/heads/main', *published).decode().splitlines()


def _has_attribution(wt, commits, patterns):
    for sha in commits:
        message = _git(wt, 'cat-file', 'commit', sha).split(b'\n\n', 1)[1]
        if _message(message, patterns) != message:
            return True
    return False


def strip_attribution(target, wt, branch):
    """Atomically replace unpublished messages, preserving authors and dates.

    This is a message-only rebase using Git objects: build the entire graph,
    verify its tip tree, then compare-and-swap the branch. No checkout, index,
    hooks or intermediate branch moves; any failure leaves the old tip intact.
    """
    patterns = [re.compile(p) for p in merge_config(target).strip_attribution]
    if not patterns:
        return
    ref = f'refs/heads/{branch}'
    tip = _git(wt, 'rev-parse', '--verify', ref).decode().strip()
    commits = _unpublished(wt, tip)
    if not _has_attribution(wt, commits, patterns):
        return
    remotes = _git(wt, 'remote').decode().splitlines()
    if 'origin' in remotes:
        # Explicit refspec also covers remotes configured to fetch only main.
        _git(wt, 'fetch', '--prune', '--no-tags', 'origin',
             '+refs/heads/*:refs/remotes/origin/*')
    commits = _unpublished(wt, tip)
    rewritten = {}
    for sha in commits:
        rewritten[sha] = _reword(wt, sha, rewritten, patterns)
    new_tip = rewritten.get(tip, tip)
    if new_tip == tip:
        return
    old_tree = _git(wt, 'rev-parse', tip + '^{tree}')
    if _git(wt, 'rev-parse', new_tip + '^{tree}') != old_tree:
        raise InfraFailure('commit attribution cleanup changed the tip tree; '
                           'branch preserved')
    _git(wt, 'update-ref', '-m', 'Strip commit attribution', ref, new_tip, tip)
