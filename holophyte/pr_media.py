"""Capture user-facing candidates and publish their review artifacts."""
import fnmatch
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote

from holophyte import pr
from holophyte.config_tables import merge_config
from holophyte.gates import InfraFailure, sh

CAPTURE_TIMEOUT = 300


def media_url(repo, branch, name, private):
    """Images use the raw host only when public; videos always link to blobs."""
    path = f'{repo}/{quote(branch)}/{quote(name)}'
    if private or Path(name).suffix != '.png':
        return f'https://github.com/{repo}/blob/{quote(branch)}/{quote(name)}?raw=true'
    return f'https://raw.githubusercontent.com/{path}'


def repo_is_private(target):
    """Read origin's visibility once, using the configured GitHub transport."""
    pull = pr._origin_pull(target)
    if pull is None:
        raise ValueError('origin does not name a GitHub repository')
    if shutil.which(pr.GH) is None:
        answer = pr.rest(target, pull, 'GET', f'repos/{pull.repo}')
        field = 'private'
    else:
        result = subprocess.run(
            [pr.GH, 'repo', 'view', pull.repo, '--json', 'isPrivate'],
            cwd=target.path, capture_output=True, text=True, timeout=pr.PR_TIMEOUT)
        if result.returncode:
            raise InfraFailure('gh repo view failed')
        answer = json.loads(result.stdout)
        field = 'isPrivate'
    value = answer.get(field) if isinstance(answer, dict) else None
    if not isinstance(value, bool):
        raise ValueError('repository visibility is missing or invalid')
    return value


def matches(wt, patterns):
    paths = sh(['git', 'diff', '--name-only', '-z', 'main...HEAD'], cwd=wt)
    return any(fnmatch.fnmatchcase(path, pattern)
               for path in paths.split('\0') for pattern in patterns)


def _capture(command, wt, output):
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(shlex.split(command) + [str(output)],
                                   cwd=wt, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log, start_new_session=True)
        try:
            code = process.wait(timeout=CAPTURE_TIMEOUT)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            return f'Capture command `{command}` failed: timed out after 300 seconds.'
    return f'Capture command `{command}` failed (exit {code}).' if code else ''


def _push(wt, output, files, task_id):
    """An empty index and commit-tree make one root commit, without a local branch."""
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / 'media'
        sh(['git', 'worktree', 'add', '--detach', str(stage)], cwd=wt)
        try:
            sh(['git', 'read-tree', '--empty'], cwd=stage)
            sh(['git', 'clean', '-fdx'], cwd=stage)
            for file in files:
                dest = stage / file.relative_to(output)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file, dest)
                sh(['git', 'add', '-f', '--', str(dest.relative_to(stage))], cwd=stage)
            tree = sh(['git', 'write-tree'], cwd=stage)
            commit = sh(['git', 'commit-tree', tree, '-m',
                         f'Evidence for {task_id}'], cwd=stage)
            sh(['git', 'push', '--force', pr.REMOTE,
                f'{commit}:refs/heads/pr-media/{task_id}'], cwd=wt)
        finally:
            sh(['git', 'worktree', 'remove', '--force', str(stage)], cwd=wt)


def _produce(target, wt, task_id, command, note):
    with tempfile.TemporaryDirectory(prefix='pr-media-') as tmp:
        output = Path(tmp)
        error = _capture(command, wt, output)
        if error:
            return '## Evidence\n\n' + error
        files = sorted(file for file in output.rglob('*')
                       if file.suffix in ('.png', '.webm', '.mp4')
                       and file.is_file() and not file.is_symlink()
                       and file.resolve().is_relative_to(output))
        if not files:
            return (f'## Evidence\n\nCapture command `{command}`'
                    ' produced no media files.')
        repo = pr.repo_of(pr.origin_url(target))
        if repo is None:
            raise ValueError('origin does not name a GitHub repository')
        try:
            private = repo_is_private(target)
            visibility = 'private' if private else 'public'
            form = 'blob-with-raw' if private else 'raw host'
            text = (f'Evidence visibility read: {repo} is {visibility}; '
                    f'images use {form}.')
        except (InfraFailure, OSError, RuntimeError, ValueError,
                subprocess.TimeoutExpired) as error:
            private = True
            text = (f'Evidence visibility read failed for {repo} '
                    f'({type(error).__name__}); images use blob-with-raw fallback.')
        note.write_text(text + ' Videos use blob-with-raw links.')
        _push(wt, output, files, task_id)
        lines = ['## Evidence', '', f'Captured with `{command}`.']
        for file in files:
            name = file.relative_to(output).as_posix()
            url = media_url(repo, f'pr-media/{task_id}', name, private)
            label = name.replace('[', r'\[').replace(']', r'\]')
            lines.append(f'{"!" if file.suffix == ".png" else ""}[{label}]({url})')
        return '\n\n'.join(lines)


def prepare(target, wt, task_id, record_note=None):
    """Reuse evidence only for this exact candidate, base, and configuration.

    Keep the receipt in the worktree's git directory, outside candidate files.
    Both the pre-PR review and PR creation call this entry point.
    """
    cfg = merge_config(target)
    if not cfg.ui_paths or not matches(wt, cfg.ui_paths):
        return ''
    identity = [sh(['git', 'rev-parse', 'HEAD', 'main'], cwd=wt), task_id,
                cfg.ui_paths, cfg.ui_capture, pr.origin_url(target)]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    git_dir = Path(sh(['git', 'rev-parse', '--absolute-git-dir'], cwd=wt))
    receipt = git_dir / f'pr-media-{key}.txt'
    note = receipt.with_suffix('.note')
    if not receipt.exists():
        try:
            section = _produce(target, wt, task_id, cfg.ui_capture, note)
        except (OSError, RuntimeError, ValueError) as error:
            section = (f'## Evidence\n\nCapture command `{cfg.ui_capture}` failed to'
                       f' publish evidence ({type(error).__name__}).')
        receipt.write_text(section)
    if record_note is not None and note.exists():
        record_note(note.read_text())
    return receipt.read_text()


def append(body, section):
    if not section:
        return body
    text, separator, linear = body.rpartition('\n\nLinear:')
    return (f'{text}\n\n{section}{separator}{linear}' if separator
            else f'{body}\n\n{section}')
