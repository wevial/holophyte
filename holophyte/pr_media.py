"""Capture user-facing candidates and publish their review artifacts."""
import base64
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
RECEIPT_VERSION = 2  # KO-512: invalidate receipts without visibility-aware URLs.


def media_url(repo, branch, name, private):
    """Images use the raw host only when public; videos always link to blobs."""
    path = f'{repo}/{quote(branch)}/{quote(name)}'
    if private or Path(name).suffix != '.png':
        return f'https://github.com/{repo}/blob/{quote(branch)}/{quote(name)}?raw=true'
    return f'https://raw.githubusercontent.com/{path}'


def repo_is_private(target, repo=None):
    """Read the evidence repository visibility using the PR transport."""
    if repo:
        owner, name = repo.split("/")
        pull = pr.PullRequest("github.com", owner, name, 0, "")
    else:
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


def _media_git_env():
    """Use the PR token without putting credentials in argv or disk config."""
    if shutil.which(pr.GH) is not None:
        result = subprocess.run([pr.GH, "auth", "token", "--hostname", "github.com"],
                                capture_output=True, text=True, timeout=pr.PR_TIMEOUT)
        if result.returncode:
            raise InfraFailure("media repository authentication failed")
        token = result.stdout.strip()
    else:
        token = pr.token_from_env()
    if not token:
        raise InfraFailure("media repository authentication is missing")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    index = int(env.get("GIT_CONFIG_COUNT", "0"))
    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env.update({f"GIT_CONFIG_KEY_{index}": "http.https://github.com/.extraheader",
                f"GIT_CONFIG_VALUE_{index}": f"AUTHORIZATION: basic {auth}",
                "GIT_CONFIG_COUNT": str(index + 1)})
    return env


def _push_repo(wt, output, files, task_id, repo):
    env = _media_git_env()

    def git(*args, cwd):
        result = subprocess.run(["git", *args], cwd=cwd, env=env,
                                capture_output=True, text=True, timeout=pr.PR_TIMEOUT)
        if result.returncode:
            # Transport output can contain credentials; never propagate it.
            raise InfraFailure(f"media repository git {args[0]} failed")
        return result.stdout.strip()

    with tempfile.TemporaryDirectory(prefix="pr-media-repo-") as tmp:
        stage = Path(tmp) / "media"
        git("clone", "--depth", "1", f"https://github.com/{repo}.git",
            str(stage), cwd=wt)
        branch = git("symbolic-ref", "--short", "HEAD", cwd=stage)
        for key in ("user.name", "user.email"):
            git("config", key, sh(["git", "config", key], cwd=wt), cwd=stage)
        for attempt in range(2):
            directory = stage / task_id
            if directory.exists():
                shutil.rmtree(directory)
            directory.mkdir()
            for file in files:
                dest = directory / file.relative_to(output)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file, dest)
            git("add", "-f", "--", task_id, cwd=stage)
            if git("diff", "--cached", "--name-only", cwd=stage):
                git("commit", "-m", f"Evidence for {task_id}", cwd=stage)
            result = subprocess.run(
                ["git", "push", "--porcelain", "origin", f"HEAD:refs/heads/{branch}"],
                cwd=stage, env=env, capture_output=True, text=True,
                timeout=pr.PR_TIMEOUT)
            if not result.returncode:
                return branch
            if attempt or not any(reason in result.stdout for reason in
                                  ("[rejected] (fetch first)",
                                   "[rejected] (non-fast-forward)")):
                raise InfraFailure("media repository push failed")
            git("fetch", "--depth", "1", "origin", branch, cwd=stage)
            git("reset", "--hard", "FETCH_HEAD", cwd=stage)


def _produce(target, wt, task_id, command, note, media_repo):
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
        repo = media_repo or pr.repo_of(pr.origin_url(target))
        if repo is None:
            raise ValueError('origin does not name a GitHub repository')
        try:
            private = (repo_is_private(target, media_repo) if media_repo
                       else repo_is_private(target))
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
        branch = f'pr-media/{task_id}'
        if media_repo:
            branch = _push_repo(wt, output, files, task_id, media_repo)
        else:
            _push(wt, output, files, task_id)
        lines = ['## Evidence', '', f'Captured with `{command}`. '
                 f'Media lives in `{repo}` on `{branch}`.']
        for file in files:
            name = file.relative_to(output).as_posix()
            path = f'{task_id}/{name}' if media_repo else name
            url = media_url(repo, branch, path, private)
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
    identity = [RECEIPT_VERSION, sh(['git', 'rev-parse', 'HEAD', 'main'], cwd=wt),
                task_id, cfg.ui_paths, cfg.ui_capture, pr.origin_url(target),
                cfg.media_repo]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    git_dir = Path(sh(['git', 'rev-parse', '--absolute-git-dir'], cwd=wt))
    receipt = git_dir / f'pr-media-{key}.txt'
    note = receipt.with_suffix('.note')
    if not receipt.exists():
        try:
            section = _produce(target, wt, task_id, cfg.ui_capture, note,
                               cfg.media_repo)
        except (InfraFailure, OSError, RuntimeError, ValueError,
                subprocess.TimeoutExpired) as error:
            destination = f" to {cfg.media_repo}" if cfg.media_repo else ""
            section = (f'## Evidence\n\nCapture command `{cfg.ui_capture}` failed to'
                       f' publish evidence{destination}'
                       f' ({type(error).__name__}).')
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
