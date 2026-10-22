import re
import subprocess

from holophyte.config.config_tables import merge_config
from holophyte.loop.gates import InfraFailure


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
    lines = message.splitlines(keepends=True)
    kept = []
    removed = False
    for line in lines:
        text = line.decode('utf-8', errors='surrogateescape')
        if any(p.search(text) for p in patterns):
            removed = True
        else:
            kept.append(line)
    if not removed:
        return message
    while kept and not kept[-1].strip():
        kept.pop()
    cleaned = re.sub(rb'\n(?:[ \t]*\r?\n)+', b'\n\n', b''.join(kept))
    trailers = re.search(rb'(?m)(?:^[A-Za-z][A-Za-z-]*: .*\n?)+\Z', cleaned)
    if trailers and trailers.start():
        # Git needs a separate final paragraph to recognize surviving trailers.
        body = cleaned[:trailers.start()].rstrip(b'\r\n')
        cleaned = body + b'\n\n' + trailers.group()
    return cleaned


def _reword(wt, sha, rewritten, transform):
    original = _git(wt, 'cat-file', 'commit', sha)
    headers, message = original.split(b'\n\n', 1)
    lines = headers.split(b'\n')
    updated = []
    for line in lines:
        if line.startswith(b'parent '):
            parent = line[7:].decode('ascii')
            line = b'parent ' + rewritten.get(parent, parent).encode('ascii')
        updated.append(line)
    cleaned = transform(message)
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


def _changes(wt, commits, transform):
    for sha in commits:
        message = _git(wt, 'cat-file', 'commit', sha).split(b'\n\n', 1)[1]
        if transform(message) != message:
            return True
    return False


def rewrite_unpublished(wt, branch, transform, what):
    ref = f'refs/heads/{branch}'
    tip = _git(wt, 'rev-parse', '--verify', ref).decode().strip()
    if not _changes(wt, _unpublished(wt, tip), transform):
        return
    remotes = _git(wt, 'remote').decode().splitlines()
    if 'origin' in remotes:
        # Explicit refspec also covers remotes configured to fetch only main.
        _git(wt, 'fetch', '--prune', '--no-tags', 'origin',
             '+refs/heads/*:refs/remotes/origin/*')
    rewritten = {}
    for sha in _unpublished(wt, tip):
        rewritten[sha] = _reword(wt, sha, rewritten, transform)
    new_tip = rewritten.get(tip, tip)
    if new_tip == tip:
        return
    old_tree = _git(wt, 'rev-parse', tip + '^{tree}')
    if _git(wt, 'rev-parse', new_tip + '^{tree}') != old_tree:
        raise InfraFailure(f'{what} changed the tip tree; branch preserved')
    _git(wt, 'update-ref', '-m', what.capitalize(), ref, new_tip, tip)


def strip_attribution(target, wt, branch):
    """Reword with no checkout, index or hooks; a failure leaves the old tip intact."""
    patterns = [re.compile(p) for p in merge_config(target).strip_attribution]
    if patterns:
        rewrite_unpublished(wt, branch, lambda m: _message(m, patterns),
                            'commit attribution cleanup')
