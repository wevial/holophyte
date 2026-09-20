"""Capture user-facing candidates and publish their review artifacts."""
import base64
import fnmatch
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import tempfile
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote

import ticket_template
from holophyte import isolation, media_store, pr
from holophyte.config_tables import merge_config
from holophyte.gates import InfraFailure, sh

CAPTURE_TIMEOUT = 300
RECEIPT_VERSION = 4  # KO-530: receipts include capture execution inputs.


def implementer_brief(target, ticket):
    states = ticket_template.parse(ticket).evidence_states
    cfg = merge_config(target)
    if not states or not cfg.ui_capture:
        return ""
    return (f"\n\nAdd or update a capture script under `{cfg.ui_capture_dir}` "
            f"for this ticket, runnable by `{cfg.ui_capture}`. Produce one image "
            "per state, named NN-slug.png in state order (01, 02, ...), plus "
            "a recording when the states describe a flow. The harness receives "
            "HOLOPHYTE_TICKET and newline-joined HOLOPHYTE_EVIDENCE_STATES.\n"
            + "\n".join(f"{i:02d}: {state}" for i, state in enumerate(states, 1)))


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


def _capture(command, wt, output, task_id, states, *, target=None):
    route = isolation.route_for(target) if target is not None else isolation.Route()
    if route.backend == 'container':
        env = dict(isolation.environment(target) or {})
    else:
        env = dict(os.environ)
    env['HOLOPHYTE_TICKET'] = task_id
    env.pop("HOLOPHYTE_EVIDENCE_STATES", None)
    if states:
        env["HOLOPHYTE_EVIDENCE_STATES"] = "\n".join(states)
    if route.backend == 'container':
        destination = Path('/workspace') / output.relative_to(Path(wt).resolve())
        argv = ['/bin/sh', '-c', shlex.join(shlex.split(command) + [str(destination)])]
        try:
            code, _ = isolation.launch(route, wt, env, argv, timeout=CAPTURE_TIMEOUT)
        except subprocess.TimeoutExpired:
            return f'Capture command `{command}` failed: timed out after 300 seconds.'
        return f'Capture command `{command}` failed (exit {code}).' if code else ''
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(shlex.split(command) + [str(output)],
                                   cwd=wt, env=env, stdin=subprocess.DEVNULL,
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


def _publish_git(target, wt, output, files, task_id, note, media_repo):
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
    urls = {}
    for file in files:
        name = file.relative_to(output).as_posix()
        path = f'{task_id}/{name}' if media_repo else name
        urls[file] = media_url(repo, branch, path, private)
    return f'Media lives in `{repo}` on `{branch}`.', urls


def _publish_bucket(target, output, files, task_id, config):
    prefix = f'{target.path.name}/{task_id}/{secrets.token_urlsafe(16)}'
    urls = {file: media_store.upload(
        config, f'{prefix}/{file.relative_to(output).as_posix()}', file)
        for file in files}
    days = config.get("retention_days")
    retention = (f'{days} days' if days else 'not specified')
    return (f'Media lives in an object bucket. Retention: {retention}; '
            'evidence expires under the bucket lifecycle set by the operator.'), urls


def _cap_files(output, files, cfg):
    kept, dropped = [], []
    for file in sorted(files, key=lambda file: (file.suffix != ".png", file)):
        if file.stat().st_size > cfg.media_max_file_mb * 1024 * 1024:
            dropped.append(f'Dropped `{file.relative_to(output)}`: exceeds '
                           f'media_max_file_mb ({cfg.media_max_file_mb} MB).')
        else:
            kept.append(file)
    total = sum(file.stat().st_size for file in kept)
    while total > cfg.media_max_total_mb * 1024 * 1024:
        file = kept.pop()
        total -= file.stat().st_size
        dropped.append(f'Dropped `{file.relative_to(output)}`: exceeds '
                       f'media_max_total_mb ({cfg.media_max_total_mb} MB).')
    return kept, dropped


def _state_media(states, urls):
    """Map published PNGs by their NN- prefix; videos never satisfy a state."""
    images = {}
    for file, url in urls.items():
        match = re.match(r"^(\d{2})-.+\.png$", file.name)
        if match:
            images.setdefault(int(match[1]), (file, url))
    lines, matched = [], set()
    for number, state in enumerate(states, 1):
        label = state.replace("[", r"\[").replace("]", r"\]")
        image = images.get(number)
        lines.append(f"- {label} — {'captured' if image else 'not captured'}")
        if image:
            file, url = image
            matched.add(file)
            lines.append(f"![{label}]({url})")
    return lines, matched


def _missing(section, states):
    lines, _ = _state_media(states, {})
    return "\n\n".join([section, *lines])


def _produce(target, wt, task_id, command, note, cfg, states):
    isolated = isolation.route_for(target).backend == 'container'
    directory = ({'dir': Path(wt).resolve(), 'prefix': '.holophyte-capture-'}
                 if isolated else {'prefix': 'pr-media-'})
    with tempfile.TemporaryDirectory(**directory) as tmp:
        output = Path(tmp)
        if isolated:
            (output / '.gitignore').write_text('*\n')
        error = _capture(command, wt, output, task_id, states, target=target)
        if error:
            return _missing('## Evidence\n\n' + error, states)
        files = sorted(file for file in output.rglob('*')
                       if file.suffix in ('.png', '.webm', '.mp4')
                       and file.is_file() and not file.is_symlink()
                       and file.resolve().is_relative_to(output))
        if not files:
            return _missing(f'## Evidence\n\nCapture command `{command}`'
                            ' produced no media files.', states)
        files, dropped = _cap_files(output, files, cfg)
        lines = ['## Evidence', f'Captured with `{command}`.']
        lines.extend(dropped)
        if not files:
            lines.append('No media remains within the evidence size limits.')
            return _missing('\n\n'.join(lines), states)
        if cfg.media_bucket:
            description, urls = _publish_bucket(
                target, output, files, task_id, cfg.media_bucket)
        else:
            description, urls = _publish_git(
                target, wt, output, files, task_id, note, cfg.media_repo)
        lines.append(description)
        state_lines, matched = _state_media(states, urls)
        lines.extend(state_lines)
        for file, url in urls.items():
            if file in matched:
                continue
            name = file.relative_to(output).as_posix()
            label = name.replace('[', r'\[').replace(']', r'\]')
            lines.append(f'{"!" if file.suffix == ".png" else ""}[{label}]({url})')
        return '\n\n'.join(lines)


def _execution_fingerprint(target):
    """Hash execution inputs without storing raw environment values in receipts."""
    route = isolation.route_for(target)
    env = (dict(isolation.environment(target) or {}) if route.backend == 'container'
           else dict(os.environ))
    credential_digest = None
    if route.backend == 'container':
        if 'env' in route.credential:
            name = route.credential['env']
            env[name] = os.environ.get(name)
        if 'file' in route.credential:
            try:
                data = Path(route.credential['file']).expanduser().read_bytes()
                credential_digest = hashlib.sha256(data).hexdigest()
            except OSError:
                credential_digest = 'unreadable'
    inputs = [asdict(route), env, credential_digest, CAPTURE_TIMEOUT]
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


def prepare(target, wt, task_id, record_note=None, evidence_states=()):
    """Reuse evidence only for this exact candidate, base, and configuration.

    Keep the receipt in the worktree's git directory, outside candidate files.
    Both the pre-PR review and PR creation call this entry point.
    """
    cfg = merge_config(target)
    if not cfg.ui_paths or not matches(wt, cfg.ui_paths):
        return ''
    identity = [RECEIPT_VERSION, sh(['git', 'rev-parse', 'HEAD', 'main'], cwd=wt),
                task_id, cfg.ui_paths, cfg.ui_capture, pr.origin_url(target),
                cfg.media_repo, cfg.media_bucket, cfg.media_max_file_mb,
                cfg.media_max_total_mb, list(evidence_states),
                _execution_fingerprint(target)]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    git_dir = Path(sh(['git', 'rev-parse', '--absolute-git-dir'], cwd=wt))
    receipt = git_dir / f'pr-media-{key}.txt'
    note = receipt.with_suffix('.note')
    if not receipt.exists():
        try:
            section = _produce(target, wt, task_id, cfg.ui_capture, note,
                               cfg, evidence_states)
        except (InfraFailure, OSError, RuntimeError, ValueError,
                subprocess.TimeoutExpired) as error:
            destination = (" to media bucket" if cfg.media_bucket else
                           f" to {cfg.media_repo}" if cfg.media_repo else "")
            detail = (f": {error}" if isinstance(error, media_store.MissingCredentials)
                      else "")
            section = (f'## Evidence\n\nCapture command `{cfg.ui_capture}` failed to'
                       f' publish evidence{destination}'
                       f' ({type(error).__name__}{detail}).')
            section = _missing(section, evidence_states)
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
