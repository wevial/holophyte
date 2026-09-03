#!/usr/bin/env python3
"""Run an exact-SHA local code review inside one hardened Docker container."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parent
IMAGE = "holophyte-reviewer:ubuntu24.04-v1"
PROFILE = "codex-sol-medium"
SCRATCH_ROOT = Path.home() / ".cache" / "holophyte" / "reviews"
SCRATCH_PREFIX = "review."
CONTAINER_PREFIX = "holophyte-review-"
# The signals a loop stops on that still let a handler run. A container the
# `finally` below would have removed must not outlive a process that never
# reached it; SIGKILL cannot be caught, and the sweep answers that case.
REMOVAL_SIGNALS = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
CODEX_AUTH = Path.home() / ".codex" / "auth.json"
DOCKERFILE = ROOT / "docker" / "reviewer.Dockerfile"
CODEX_FILES = ("codex", "codex-code-mode-host")

# Verdict vocabularies. A review round argues for or against the candidate; the
# loop's terminal adjudication round issues a bare pass/fail on the final state.
REVIEW_VERDICTS = ("APPROVE", "REQUEST_CHANGES")
ADJUDICATION_VERDICTS = ("PASS", "FAIL")


class ReviewBoundaryError(RuntimeError):
    pass


@dataclass(frozen=True)
class StagedCandidate:
    path: Path
    base_sha: str
    candidate_sha: str
    fingerprint: str


def _run(
    args: Sequence[str], *, cwd: Path | None = None, timeout: int = 300
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(args), cwd=cwd, capture_output=True, text=True, timeout=timeout
    )
    if result.returncode:
        raise ReviewBoundaryError(
            f"command failed ({result.returncode}): {' '.join(args)}\n"
            f"{result.stdout}{result.stderr}".strip()
        )
    return result


def _git(repo: Path, *args: str) -> str:
    return _run(["git", *args], cwd=repo).stdout.strip()


def _commit(repo: Path, revision: str) -> str:
    sha = _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}")
    if len(sha) != 40 or any(ch not in "0123456789abcdef" for ch in sha):
        raise ReviewBoundaryError(f"not a full commit SHA: {revision}")
    return sha


def _fingerprint(repo: Path) -> str:
    facts = [
        _git(repo, "rev-parse", "HEAD"),
        _git(repo, "rev-parse", "HEAD^{tree}"),
        _git(repo, "rev-parse", "refs/review/base"),
        _git(repo, "rev-parse", "refs/review/candidate"),
        _git(repo, "status", "--porcelain=v1", "--untracked-files=all"),
        _git(repo, "remote"),
    ]
    return hashlib.sha256("\n".join(facts).encode()).hexdigest()


def stage_candidate(
    source: Path, stage: Path, base_revision: str, candidate_revision: str
) -> StagedCandidate:
    """Create a self-contained detached, clean, zero-remote review checkout."""
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
    _git(stage, "update-ref", "refs/review/base", base)
    _git(stage, "update-ref", "refs/review/candidate", candidate)
    _git(stage, "checkout", "--quiet", "--detach", candidate)
    if _git(stage, "remote") or _git(stage, "status", "--porcelain"):
        raise ReviewBoundaryError("staged candidate is not clean and zero-remote")
    return StagedCandidate(stage, base, candidate, _fingerprint(stage))


def _prepare_runtime(root: Path, auth: Path, codex: Path) -> tuple[Path, Path]:
    """Copy only Codex auth and its two required executables into scratch."""
    home = root / "home"
    codex_home = home / ".codex"
    toolchain = root / "toolchain"
    codex_home.mkdir(parents=True, mode=0o700)
    toolchain.mkdir(mode=0o700)

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
) -> list[str]:
    """Build one fixed Docker invocation; the prompt is a positional argument."""
    mounts = [
        f"{workspace.expanduser().resolve(strict=True)}:/workspace:ro",
        f"{reviewer_home.expanduser().resolve(strict=True)}:/home/reviewer:rw",
        f"{toolchain.expanduser().resolve(strict=True)}:/opt/codex/bin:ro",
    ]
    if any(":" in mount.split(":", 1)[0] for mount in mounts):
        raise ReviewBoundaryError("bind source paths may not contain ':'")

    preflight = r'''
actual=$(git rev-parse HEAD)
test "$actual" = "$(git rev-parse refs/review/candidate)"
test -z "$(git remote)"
test -z "$(git status --porcelain=v1 --untracked-files=all)"
if touch /workspace/.holophyte-write-probe 2>/tmp/write-probe.err; then
  rm -f /workspace/.holophyte-write-probe
  exit 41
fi
test ! -e /var/run/docker.sock
echo "PREFLIGHT_OK candidate=$actual" >&2
exec /opt/codex/bin/codex exec --json -C /workspace \
  -m gpt-5.6-sol -c 'model_reasoning_effort="medium"' \
  -s danger-full-access --ephemeral "$1"
'''.strip()

    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=256",
        "--memory=2g",
        "--cpus=2",
        "--network=bridge",
        f"--user={uid}:{gid}",
        "--workdir=/workspace",
        "--env=HOME=/home/reviewer",
        "--tmpfs",
        f"/tmp:rw,nosuid,nodev,noexec,size=256m,uid={uid},gid={gid},mode=1777",
    ]
    for mount in mounts:
        command.extend(["--volume", mount])
    return command + [image, "/bin/sh", "-eu", "-c", preflight, "review", prompt]


def terminal_verdict(message: str, verdicts: Sequence[str] = REVIEW_VERDICTS) -> str:
    """The single allowed verdict `message` ends with.

    Raises when the message carries anything other than exactly one allowed
    verdict line, in final position, so an unparseable reply is never read as
    an approval. A caller that must turn a malformed reply into a decision of
    its own — the loop's terminal adjudication reads one as FAIL — catches the
    error and keeps the message it already holds.
    """
    allowed = [f"VERDICT: {verdict}" for verdict in verdicts]
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    found = [line for line in lines if line in allowed]
    if len(found) != 1 or lines[-1] != found[0]:
        raise ReviewBoundaryError(
            "review must end with exactly one of: " + ", ".join(allowed)
        )
    return found[0].removeprefix("VERDICT: ")


def parse_codex_output(
    output: str, verdicts: Sequence[str] | None = REVIEW_VERDICTS
) -> tuple[str, str | None]:
    """Read trusted CLI JSONL events, not model-controlled transcript strings.

    `verdicts=None` returns the final message unadjudicated, for a caller that
    has to see a malformed reply rather than have it raised at the boundary.
    """
    command_succeeded = False
    messages: list[str] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReviewBoundaryError("Codex emitted invalid JSONL") from exc
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


def _ensure_image() -> None:
    if subprocess.run(
        ["docker", "image", "inspect", IMAGE], capture_output=True, text=True
    ).returncode:
        _run(
            [
                "docker",
                "build",
                "--pull=false",
                "--tag",
                IMAGE,
                "--file",
                str(DOCKERFILE),
                str(DOCKERFILE.parent),
            ],
            cwd=ROOT,
            timeout=900,
        )


def _remove_container(name: str) -> None:
    subprocess.run(
        ["docker", "rm", "--force", name], capture_output=True, text=True, timeout=30
    )
    if subprocess.run(
        ["docker", "inspect", name], capture_output=True, text=True, timeout=30
    ).returncode == 0:
        raise ReviewBoundaryError(
            f"review container still exists after cleanup: {name}"
        )


@contextlib.contextmanager
def _removing_on_signal(name: str):
    """Remove container `name` if a stop signal arrives inside the block.

    Each handler removes the container, then restores the default disposition
    and re-sends the signal to this process, so the loop still ends by the
    signal it was sent. The handlers it displaced come back on exit, so
    nothing changes outside the review window. Signals can only be installed
    from the main thread; a review run anywhere else keeps the `finally` path
    alone.
    """
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
    """Running review containers whose scratch directory no longer exists.

    A review's container is named after its scratch directory, which the
    process removes on its way out; a running container without one belongs
    to a loop that is gone. Raises when there is no `docker` to ask or it
    does not answer, so a caller can say the check was skipped rather than
    report a clean host it never looked at.
    """
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


def run_review(
    *,
    repo: Path,
    base_sha: str,
    candidate_sha: str,
    prompt: str,
    profile: str = PROFILE,
    timeout: int = 1800,
    verdicts: Sequence[str] | None = REVIEW_VERDICTS,
) -> str:
    if profile != PROFILE:
        raise ReviewBoundaryError(f"unknown reviewer profile: {profile}")
    _ensure_image()
    codex = shutil.which("codex")
    if not codex:
        raise ReviewBoundaryError("Codex CLI is not installed")

    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    scratch = tempfile.TemporaryDirectory(prefix=SCRATCH_PREFIX, dir=SCRATCH_ROOT)
    with scratch as temporary:
        root = Path(temporary)
        staged = stage_candidate(repo, root / "candidate", base_sha, candidate_sha)
        home, toolchain = _prepare_runtime(root, CODEX_AUTH, Path(codex))
        name = "holophyte-" + root.name.replace(".", "-")
        command = container_command(
            image=IMAGE,
            workspace=staged.path,
            reviewer_home=home,
            toolchain=toolchain,
            name=name,
            prompt=prompt,
            uid=os.getuid(),
            gid=os.getgid(),
        )
        try:
            with _removing_on_signal(name):
                result = _run(command, timeout=timeout)
        finally:
            _remove_container(name)
            if _fingerprint(staged.path) != staged.fingerprint:
                raise ReviewBoundaryError("staged candidate changed during review")
        if "PREFLIGHT_OK" not in result.stderr:
            raise ReviewBoundaryError("review preflight did not complete")
        message, _ = parse_codex_output(result.stdout, verdicts)
        return message


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    args = parser.parse_args()
    print(
        run_review(
            repo=args.repo,
            base_sha=args.base,
            candidate_sha=args.candidate,
            prompt=args.prompt_file.read_text(),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
