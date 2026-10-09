#!/usr/bin/env python3
"""Run an exact-SHA local code review inside one hardened Docker container."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parent
IMAGE = "holophyte-reviewer:ubuntu24.04-v12"
MODEL = "gpt-6-astra"
EFFORT = "high"
EFFORTS = ("low", "medium", "high", "xhigh")


def profile_for(model: str, effort: str) -> str:
    return f"codex-{model.rsplit('-', 1)[-1]}-{effort}"


PROFILE = profile_for(MODEL, EFFORT)
SCRATCH_ROOT = Path.home() / ".cache" / "holophyte" / "reviews"
SCRATCH_PREFIX = "review."
CONTAINER_PREFIX = "holophyte-review-"
# SIGKILL cannot be caught; the sweep answers that case.
REMOVAL_SIGNALS = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
CODEX_AUTH = Path.home() / ".codex" / "auth.json"
DOCKERFILE = ROOT / "docker" / "reviewer.Dockerfile"
DOCKERFILE_PATH = "docker/reviewer.Dockerfile"
RUNNER_PATH = "review_runner.py"
IMAGE_LINE = re.compile(r'^IMAGE = "([^"\s]+)"$', re.MULTILINE)
CODEX_FILES = ("codex", "codex-code-mode-host")
CREDENTIAL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

REVIEW_VERDICTS = ("APPROVE", "REQUEST_CHANGES")
ADJUDICATION_VERDICTS = ("PASS", "FAIL")
SESSION_EVENTS = {"thread.started": "thread_id", "session.created": "session_id"}


EVIDENCE_LINE = 300
EVIDENCE_TAIL = 2048


class ReviewBoundaryError(RuntimeError):
    line = None
    tail = None
    exit_status = None


@dataclass(frozen=True)
class StagedCandidate:
    path: Path
    base_sha: str
    candidate_sha: str
    fingerprint: str


def _run(
    args: Sequence[str], *, cwd: Path | None = None, timeout: int = 300,
    check: bool = True, on_start=None,
) -> subprocess.CompletedProcess[str]:
    if on_start is None:
        result = subprocess.run(
            list(args), cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
    else:
        with subprocess.Popen(list(args), cwd=cwd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) as proc:
            on_start(proc)
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as expired:
                proc.kill()
                expired.output, expired.stderr = proc.communicate()
                raise
        result = subprocess.CompletedProcess(list(args), proc.returncode,
                                             stdout, stderr)
    if check and result.returncode:
        error = ReviewBoundaryError(
            f"command failed ({result.returncode}): {' '.join(args)}\n"
            f"{result.stdout}{result.stderr}".strip()
        )
        error.returncode = result.returncode
        error.output = f"{result.stdout}{result.stderr}"
        raise error
    return result


def _git(repo: Path, *args: str) -> str:
    return _run(["git", *args], cwd=repo).stdout.strip()


def _commit(repo: Path, revision: str) -> str:
    sha = _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}")
    if len(sha) != 40 or any(ch not in "0123456789abcdef" for ch in sha):
        raise ReviewBoundaryError(f"not a full commit SHA: {revision}")
    return sha


def _fingerprint(repo: Path, run_id=None) -> str:
    from holophyte.agents.review_workspace import review_refs

    base_ref, candidate_ref = review_refs(run_id)
    facts = [
        _git(repo, "rev-parse", "HEAD"),
        _git(repo, "rev-parse", "HEAD^{tree}"),
        _git(repo, "rev-parse", base_ref),
        _git(repo, "rev-parse", candidate_ref),
        _git(repo, "status", "--porcelain=v1", "--untracked-files=all"),
        _git(repo, "remote"),
    ]
    return hashlib.sha256("\n".join(facts).encode()).hexdigest()


def _check_clean(stage: Path) -> None:
    """`--ignored=no` is spelled so a carried directory git did not ignore is caught."""
    if _git(stage, "remote") or _git(
        stage, "status", "--porcelain=v1", "--untracked-files=all", "--ignored=no"
    ):
        raise ReviewBoundaryError("staged candidate is not clean and zero-remote")


def check_carry(source: Path, entry: str) -> Path:
    relative = Path(entry)
    if (relative.is_absolute() or not entry
            or any(part in ("..", "") for part in relative.parts)):
        raise ReviewBoundaryError(
            f"[worktree] carry: {entry!r} escapes the repository")
    if _run(["git", "ls-files", "--error-unmatch", "--", entry],
            cwd=source, check=False).returncode == 0:
        raise ReviewBoundaryError(
            f"[worktree] carry: {entry!r} is tracked in git; only an ignored "
            "install directory can be carried")
    if _run(["git", "check-ignore", "-q", "--", f"{relative}/"],
            cwd=source, check=False).returncode:
        raise ReviewBoundaryError(
            f"[worktree] carry: {entry!r} is not ignored by git in the worktree")
    return source / relative


def _carry_into(source: Path, stage: Path, carry: Sequence[str]) -> None:
    """Each copy has every write bit cleared, so the reviewer reads it like the tree."""
    for entry in carry:
        origin = check_carry(source, entry)
        relative = Path(entry)
        if not origin.is_dir() or origin.is_symlink():
            raise ReviewBoundaryError(
                f"[worktree] carry: {entry!r} is not a directory in the worktree "
                f"{source}")
        shutil.copytree(origin, stage / relative, symlinks=True)
        for root, dirs, files in os.walk(stage / relative, topdown=False):
            for name in files + dirs:
                path = Path(root) / name
                if not path.is_symlink():
                    path.chmod(path.stat().st_mode & ~0o222)
        (stage / relative).chmod((stage / relative).stat().st_mode & ~0o222)


def stage_candidate(
    source: Path,
    stage: Path,
    base_revision: str,
    candidate_revision: str,
    carry: Sequence[str] = (),
    run_id: int | None = None,
) -> StagedCandidate:
    """The fingerprint covers the tracked tree alone, carried directories or not."""
    source = source.expanduser().resolve(strict=True)
    if _git(source, "rev-parse", "--is-inside-work-tree") != "true":
        raise ReviewBoundaryError(f"not a Git worktree: {source}")
    base = _commit(source, base_revision)
    candidate = _commit(source, candidate_revision)
    if subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, candidate], cwd=source
    ).returncode:
        raise ReviewBoundaryError(f"base {base} is not an ancestor of {candidate}")

    stage = stage.expanduser().resolve()
    if stage.exists():
        raise ReviewBoundaryError(f"review stage already exists: {stage}")
    stage.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _run(["git", "init", "-q", str(stage)])
    _git(stage, "fetch", "--quiet", "--no-tags", str(source), base, candidate)
    from holophyte.agents.review_workspace import review_refs

    base_ref, candidate_ref = review_refs(run_id)
    _git(stage, "update-ref", base_ref, base)
    _git(stage, "update-ref", candidate_ref, candidate)
    _git(stage, "checkout", "--quiet", "--detach", candidate)
    _check_clean(stage)
    _carry_into(source, stage, carry)
    _check_clean(stage)
    return StagedCandidate(stage, base, candidate, _fingerprint(stage, run_id))


def _prepare_runtime(root: Path, auth: Path,
                     codex: Path | None) -> tuple[Path, Path]:
    home = root / "home"
    codex_home = home / ".codex"
    toolchain = root / "toolchain"
    home.mkdir()
    toolchain.mkdir(mode=0o700)
    if codex is None:
        return home, toolchain
    codex_home.mkdir(mode=0o700)

    auth = auth.expanduser().resolve(strict=True)
    shutil.copyfile(auth, codex_home / "auth.json")
    (codex_home / "auth.json").chmod(0o600)

    release = codex.expanduser().resolve(strict=True).parent
    for name in CODEX_FILES:
        source = release / name
        if not source.is_file() or not os.access(source, os.X_OK):
            raise ReviewBoundaryError(f"Codex release is missing executable: {source}")
        shutil.copy2(source, toolchain / name)
    return home, toolchain


CODEX_EXEC = r'''exec /opt/codex/bin/codex exec --json -C /home/reviewer/candidate \
  -m "$2" -c "$3" ${4+-c "$4"} \
  -s danger-full-access --SWITCH multi_agent "$1"'''
CLAUDE_EXEC = '''cd /home/reviewer/candidate
exec /opt/claude/bin/claude -p --model "$2" --effort "$3" --output-format json "$1"'''


def hardening_flags(uid: int, gid: int, memory: str = "2g") -> list[str]:
    return ["--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--pids-limit=256",
            f"--memory={memory}", "--cpus=2", "--network=bridge",
            f"--user={uid}:{gid}", "--workdir=/workspace"]


def container_command(
    *,
    image: str,
    workspace: Path,
    reviewer_home: Path,
    toolchain: Path,
    name: str,
    prompt: str,
    uid: int,
    gid: int,
    model: str = MODEL,
    effort: str = EFFORT,
    run_id: int | None = None,
    service_tier: str | None = None,
    multi_agent: bool = False,
    harness: str = "codex",
    credential: str | None = None,
) -> list[str]:
    """Prompt, model, effort and tier are positional arguments, never interpolated."""
    from holophyte.agents.review_workspace import review_refs

    mounts = [
        f"{workspace.expanduser().resolve(strict=True)}:/workspace:ro",
        f"{reviewer_home.expanduser().resolve(strict=True)}:/home/reviewer:rw",
        f"{toolchain.expanduser().resolve(strict=True)}:/opt/codex/bin:ro",
    ]
    if any(":" in mount.split(":", 1)[0] for mount in mounts):
        raise ReviewBoundaryError("bind source paths may not contain ':'")

    preflight = r'''
mkdir -p -m 0700 "$TMPDIR"
actual=$(git rev-parse HEAD)
test "$actual" = "$(git rev-parse "$HOLOPHYTE_REVIEW_CANDIDATE")"
test -z "$(git remote)"
test -z "$(git status --porcelain=v1 --untracked-files=all)"
if touch /workspace/.holophyte-write-probe 2>/tmp/write-probe.err; then
  rm -f /workspace/.holophyte-write-probe
  exit 41
fi
test ! -e /var/run/docker.sock
echo "PREFLIGHT_OK candidate=$actual" >&2
cp -a /workspace /home/reviewer/candidate
'''.strip() + "\n" + (CLAUDE_EXEC if harness == "claude" else CODEX_EXEC.replace(
        "SWITCH", "enable" if multi_agent else "disable"))

    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        *hardening_flags(uid, gid),
        "--env=HOME=/home/reviewer",
        f"--env=HOLOPHYTE_REVIEW_CANDIDATE={review_refs(run_id)[1]}",
        *([f"--env={credential}"] if harness == "claude" else []),
        "--tmpfs",
        f"/tmp:rw,nosuid,nodev,noexec,size=256m,uid={uid},gid={gid},mode=1777",
    ]
    for mount in mounts:
        command.extend(["--volume", mount])
    tier = [] if service_tier is None else [
        f"service_tier={json.dumps(service_tier, ensure_ascii=False)}"]
    turn = ([model, effort] if harness == "claude" else
            [model, f'model_reasoning_effort="{effort}"', *tier])
    return command + [image, "/bin/sh", "-eu", "-c", preflight, "review", prompt,
                      *turn]


def terminal_verdict(message: str, verdicts: Sequence[str] = REVIEW_VERDICTS) -> str:
    """Raise unless one allowed verdict line ends it: a bad reply is never approval."""
    allowed = [f"VERDICT: {verdict}" for verdict in verdicts]
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    found = [line for line in lines if line in allowed]
    if len(found) != 1 or lines[-1] != found[0]:
        raise ReviewBoundaryError(
            "review must end with exactly one of: " + ", ".join(allowed)
        )
    return found[0].removeprefix("VERDICT: ")


def codex_events(output: str):
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError as exc:
            error = ReviewBoundaryError("Codex emitted invalid JSONL")
            error.line = "".join(ch for ch in line if ch.isprintable())[
                :EVIDENCE_LINE]
            raise error from exc


def parse_codex_output(
    output: str, verdicts: Sequence[str] | None = REVIEW_VERDICTS
) -> tuple[str, str | None]:
    """Read trusted CLI JSONL events, not model-controlled transcript strings."""
    command_succeeded = False
    messages: list[str] = []
    for event in codex_events(output):
        if event.get("type") != "item.completed":
            continue
        item = event.get("item", {})
        if item.get("type") == "command_execution" and item.get("exit_code") == 0:
            command_succeeded = True
        elif item.get("type") == "agent_message":
            messages.append(item.get("text", ""))
    if not command_succeeded:
        raise ReviewBoundaryError("reviewer produced no successful command event")
    if not messages:
        raise ReviewBoundaryError("reviewer produced no final message event")
    message = messages[-1]
    if verdicts is None:
        return message, None
    return message, terminal_verdict(message, verdicts)


def parse_claude_output(
    output: str, verdicts: Sequence[str] | None = REVIEW_VERDICTS
) -> tuple[str, str | None]:
    try:
        reply = json.loads(output)
    except json.JSONDecodeError:
        reply = None
    if not (isinstance(reply, dict) and reply.get("is_error") is False
            and isinstance(reply.get("result"), str)):
        detail = reply.get("result") if isinstance(reply, dict) else None
        error = ReviewBoundaryError(
            f"Claude turn failed: {detail}" if detail
            else "Claude printed no JSON result")
        raise error
    message = reply["result"]
    if verdicts is None:
        return message, None
    return message, terminal_verdict(message, verdicts)


PARSERS = {"codex": parse_codex_output, "claude": parse_claude_output}


def codex_session(output: str) -> str | None:
    from holophyte.agents.transcripts import SESSION_ID

    for event in codex_events(output):
        key = SESSION_EVENTS.get(event.get("type"))
        value = event.get(key) if key else None
        if isinstance(value, str) and SESSION_ID.fullmatch(value):
            return value
    return None


def keep_transcript(home: Path, transcripts: Path, session: str) -> bool:
    sessions = home / ".codex" / "sessions"
    try:
        if (sessions.parent.is_symlink() or sessions.is_symlink()
                or not sessions.is_dir()):
            return False
        found = [Path(top) / name for top, _, names in os.walk(sessions)
                 for name in names if name.endswith(f"-{session}.jsonl")]
        found = [path for path in found if stat.S_ISREG(path.lstat().st_mode)]
        if not found:
            return False
        newest = max(found, key=lambda path: path.lstat().st_mtime_ns)
        transcripts.mkdir(parents=True, exist_ok=True, mode=0o700)
        transcripts.chmod(0o700)
        target = transcripts / newest.name
        source = os.open(newest, os.O_RDONLY | os.O_NOFOLLOW)
        with open(source, "rb") as reader:
            created = os.open(
                target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with open(created, "wb") as writer:
                    shutil.copyfileobj(reader, writer)
            except OSError:
                target.unlink(missing_ok=True)
                raise
    except OSError:
        return False
    return True


def image_for(candidate: StagedCandidate) -> tuple[str, str]:
    """Both come from the candidate commit, so a new tool is reviewed in its image."""
    dockerfile = _run(
        ["git", "show", f"{candidate.candidate_sha}:{DOCKERFILE_PATH}"],
        cwd=candidate.path, check=False)
    runner = _run(
        ["git", "show", f"{candidate.candidate_sha}:{RUNNER_PATH}"],
        cwd=candidate.path, check=False)
    tag = IMAGE_LINE.search(runner.stdout) if runner.returncode == 0 else None
    if dockerfile.returncode or tag is None:
        return IMAGE, DOCKERFILE.read_text()
    return tag.group(1), dockerfile.stdout


def _ensure_image(image: str, dockerfile: str, *, candidate: str) -> None:
    """The build context holds that Dockerfile alone: the candidate's is what builds."""
    if subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, text=True
    ).returncode == 0:
        return
    with tempfile.TemporaryDirectory(prefix="reviewer-image.") as context:
        path = Path(context) / Path(DOCKERFILE_PATH).name
        path.write_text(dockerfile)
        try:
            _run(
                ["docker", "build", "--pull=false", "--tag", image,
                 "--file", str(path), context],
                cwd=ROOT,
                timeout=900,
            )
        except ReviewBoundaryError as exc:
            raise ReviewBoundaryError(
                f"reviewer image {image} failed to build from candidate "
                f"{candidate}:{DOCKERFILE_PATH}: {exc}"
            ) from exc


def _remove_container(name: str, *, env=None) -> None:
    environment = {} if env is None else {"env": env}
    subprocess.run(
        ["docker", "rm", "--force", name], capture_output=True, text=True, timeout=30,
        **environment
    )
    if subprocess.run(
        ["docker", "inspect", name], capture_output=True, text=True, timeout=30,
        **environment
    ).returncode == 0:
        raise ReviewBoundaryError(
            f"review container still exists after cleanup: {name}"
        )


@contextlib.contextmanager
def _removing_on_signal(name: str):
    """Signals install only from the main thread; elsewhere `finally` alone removes."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum, frame):
        _remove_container(name)
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    previous = {sig: signal.signal(sig, handler) for sig in REMOVAL_SIGNALS}
    try:
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


def _docker() -> str:
    docker = shutil.which("docker")
    if not docker:
        raise ReviewBoundaryError("no docker on PATH")
    return docker


def stray_containers() -> list[str]:
    """Raise when docker cannot be asked, so no caller reports an unseen host clean."""
    result = subprocess.run(
        [_docker(), "ps", "--filter", f"name={CONTAINER_PREFIX}",
         "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise ReviewBoundaryError(
            f"docker ps failed ({result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}")
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return [
        name for name in names
        if name.startswith(CONTAINER_PREFIX)
        and not (SCRATCH_ROOT / (SCRATCH_PREFIX + name[len(CONTAINER_PREFIX):])
                 ).is_dir()
    ]


def _harness_tools(harness: str, credential: str | None) -> Path | None:
    if harness not in PARSERS:
        raise ReviewBoundaryError(f"unknown review harness {harness!r}")
    if harness == "claude":
        if not (credential and CREDENTIAL_NAME.fullmatch(credential)
                and os.environ.get(credential)):
            raise ReviewBoundaryError(
                f"Claude credential variable {credential} is unset or empty")
        return None
    codex = shutil.which("codex")
    if not codex:
        raise ReviewBoundaryError("Codex CLI is not installed")
    return Path(codex)


def run_review(
    *,
    repo: Path,
    base_sha: str,
    candidate_sha: str,
    prompt: str,
    model: str = MODEL,
    effort: str = EFFORT,
    profile: str | None = None,
    timeout: int = 1800,
    verdicts: Sequence[str] | None = REVIEW_VERDICTS,
    carry: Sequence[str] = (),
    run_id: int | None = None,
    on_start=None,
    service_tier: str | None = None,
    transcripts: Path | None = None,
    on_session=None,
    multi_agent: bool = False,
    harness: str = "codex",
    credential: str | None = None,
) -> str:
    """`profile` must be what the model and effort compute to; another is refused."""
    if not model:
        raise ReviewBoundaryError("empty reviewer model")
    if effort not in EFFORTS:
        raise ReviewBoundaryError(
            f"unknown reasoning effort {effort!r}; one of {', '.join(EFFORTS)}")
    if profile is not None and profile != profile_for(model, effort):
        raise ReviewBoundaryError(
            f"reviewer profile {profile} does not name the route "
            f"{model} at {effort} ({profile_for(model, effort)})")
    codex = _harness_tools(harness, credential)

    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    scratch = tempfile.TemporaryDirectory(prefix=SCRATCH_PREFIX, dir=SCRATCH_ROOT)
    with scratch as temporary:
        root = Path(temporary)
        staged = stage_candidate(
            repo, root / "candidate", base_sha, candidate_sha,
            carry=carry, run_id=run_id)
        image, dockerfile = image_for(staged)
        _ensure_image(image, dockerfile, candidate=staged.candidate_sha)
        home, toolchain = _prepare_runtime(root, CODEX_AUTH, codex)
        name = "holophyte-" + root.name.replace(".", "-")
        command = container_command(
            image=image,
            workspace=staged.path,
            reviewer_home=home,
            toolchain=toolchain,
            name=name,
            prompt=prompt,
            uid=os.getuid(),
            gid=os.getgid(),
            model=model,
            effort=effort,
            run_id=run_id,
            service_tier=service_tier,
            multi_agent=multi_agent,
            harness=harness,
            credential=credential,
        )
        try:
            with _removing_on_signal(name):
                result = _run(command, timeout=timeout, on_start=on_start,
                              check=codex is not None)
        finally:
            _remove_container(name)
            if _fingerprint(staged.path, run_id) != staged.fingerprint:
                raise ReviewBoundaryError("staged candidate changed during review")
        if "PREFLIGHT_OK" not in result.stderr:
            raise ReviewBoundaryError(
                "review preflight did not complete: "
                f"{result.stderr.strip()[-EVIDENCE_LINE:]}")
        try:
            message, _ = PARSERS[harness](result.stdout, verdicts)
        except ReviewBoundaryError as error:
            error.tail = (result.stdout or result.stderr)[-EVIDENCE_TAIL:]
            error.exit_status = result.returncode
            if codex is None:
                error.output = f"{result.stdout}{result.stderr}"
            raise
        if (transcripts is not None and on_session is not None
                and (session := codex_session(result.stdout))
                and keep_transcript(home, transcripts, session)):
            on_session(session)
        return message


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--base", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    args = parser.parse_args()
    print(
        run_review(
            repo=args.repo,
            run_id=args.run_id,
            base_sha=args.base,
            candidate_sha=args.candidate,
            prompt=args.prompt_file.read_text(),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
