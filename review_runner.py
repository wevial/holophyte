#!/usr/bin/env python3
"""Stage and run exact-SHA code reviews in a hardened local container.

The host process is the trusted control plane. It materializes a detached,
zero-remote Git repository and gives the reviewer only two mounts:

* /workspace: the exact candidate repository, read-only;
* /home/reviewer: disposable reviewer state containing a copy of its auth.

The Codex sandbox is deliberately disabled *inside* the container because the
container is the enforcement boundary. No Docker socket or host home is mounted.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parent
DEFAULT_IMAGE = "holophyte-reviewer:ubuntu24.04-v1"
DEFAULT_PROFILE = "codex-sol-medium"
DEFAULT_SCRATCH_ROOT = Path.home() / ".cache" / "holophyte" / "reviews"
DEFAULT_CODEX_AUTH = Path.home() / ".codex" / "auth.json"
DOCKERFILE = ROOT / "docker" / "reviewer.Dockerfile"


class ReviewBoundaryError(RuntimeError):
    """The review boundary could not prove or preserve its contract."""


@dataclass(frozen=True)
class StagedCandidate:
    path: Path
    base_sha: str
    candidate_sha: str
    fingerprint: str


def _run(args: Sequence[str], *, cwd: Path | None = None, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(args), cwd=cwd, capture_output=True, text=True, timeout=timeout
    )
    if result.returncode != 0:
        command = " ".join(args)
        raise ReviewBoundaryError(
            f"command failed ({result.returncode}): {command}\n"
            f"{result.stdout}{result.stderr}".strip()
        )
    return result


def _git(repo: Path, *args: str) -> str:
    return _run(["git", *args], cwd=repo).stdout.strip()


def _resolve_commit(repo: Path, revision: str) -> str:
    resolved = _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}")
    if len(resolved) != 40 or any(ch not in "0123456789abcdef" for ch in resolved):
        raise ReviewBoundaryError(f"Git did not resolve a full commit SHA: {revision!r}")
    return resolved


def candidate_fingerprint(repo: Path) -> str:
    """Hash immutable identity plus status; postflight must match exactly."""
    payload = "\n".join(
        [
            _git(repo, "rev-parse", "HEAD"),
            _git(repo, "rev-parse", "HEAD^{tree}"),
            _git(repo, "rev-parse", "refs/review/base"),
            _git(repo, "status", "--porcelain=v1", "--untracked-files=all"),
            _git(repo, "diff", "--binary", "refs/review/base..HEAD"),
        ]
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def stage_candidate(
    source_repo: Path, stage: Path, base_revision: str, candidate_revision: str
) -> StagedCandidate:
    """Materialize a self-contained detached, zero-remote exact-SHA checkout."""
    source_repo = source_repo.expanduser().resolve(strict=True)
    if _git(source_repo, "rev-parse", "--is-inside-work-tree") != "true":
        raise ReviewBoundaryError(f"not a Git worktree: {source_repo}")
    base_sha = _resolve_commit(source_repo, base_revision)
    candidate_sha = _resolve_commit(source_repo, candidate_revision)
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base_sha, candidate_sha],
        cwd=source_repo,
        capture_output=True,
        text=True,
    )
    if ancestor.returncode != 0:
        raise ReviewBoundaryError(
            f"base {base_sha} is not an ancestor of candidate {candidate_sha}"
        )

    stage = stage.expanduser().resolve()
    if stage.exists():
        raise ReviewBoundaryError(f"review stage already exists: {stage}")
    stage.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _run(["git", "init", "-q", str(stage)])
    _git(stage, "fetch", "--quiet", "--no-tags", str(source_repo), base_sha, candidate_sha)
    _git(stage, "update-ref", "refs/review/base", base_sha)
    _git(stage, "update-ref", "refs/review/candidate", candidate_sha)
    _git(stage, "checkout", "--quiet", "--detach", candidate_sha)

    if _git(stage, "remote"):
        raise ReviewBoundaryError("staged review repository unexpectedly has a remote")
    if _git(stage, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ReviewBoundaryError("staged review repository is not clean")

    return StagedCandidate(
        path=stage,
        base_sha=base_sha,
        candidate_sha=candidate_sha,
        fingerprint=candidate_fingerprint(stage),
    )


def profile_command(profile: str, prompt: str) -> list[str]:
    """Resolve a named reviewer profile to its in-container argv."""
    if profile != DEFAULT_PROFILE:
        raise ReviewBoundaryError(f"unknown reviewer profile: {profile}")
    return [
        "/opt/codex/bin/codex",
        "exec",
        "-C",
        "/workspace",
        "-m",
        "gpt-5.6-sol",
        "-c",
        'model_reasoning_effort="medium"',
        "-s",
        "danger-full-access",
        "--ephemeral",
        prompt,
    ]


def validate_review_transcript(transcript: str) -> None:
    """Reject known tool-host failures and reviews with no successful command."""
    failure_markers = (
        "Code Mode is unavailable",
        "failed to spawn code-mode host",
        "ERROR codex_core::tools::router",
    )
    if any(marker in transcript for marker in failure_markers):
        raise ReviewBoundaryError("reviewer tool host failed; verdict is not evidence")
    has_exec = transcript.startswith("exec\n") or "\nexec\n" in transcript
    if not has_exec or " succeeded in " not in transcript:
        raise ReviewBoundaryError("reviewer produced no successful local command evidence")


def container_command(
    *,
    image: str,
    workspace: Path,
    reviewer_home: Path,
    uid: int,
    gid: int,
    inner_command: Sequence[str],
    codex_release_dir: Path | None = None,
    container_name: str | None = None,
) -> list[str]:
    """Construct the fixed hardened container boundary."""
    workspace = workspace.expanduser().resolve(strict=True)
    reviewer_home = reviewer_home.expanduser().resolve(strict=True)
    if ":" in str(workspace) or ":" in str(reviewer_home):
        raise ReviewBoundaryError("bind source paths may not contain ':'")
    command = [
        "docker",
        "run",
        "--rm",
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
        "--volume",
        f"{workspace}:/workspace:ro",
        "--volume",
        f"{reviewer_home}:/home/reviewer:rw",
    ]
    if container_name is not None:
        if not re_fullmatch_container_name(container_name):
            raise ReviewBoundaryError(f"invalid container name: {container_name!r}")
        command.extend(["--name", container_name])
    if codex_release_dir is not None:
        release_dir = codex_release_dir.expanduser().resolve(strict=True)
        for executable in ("codex", "codex-code-mode-host"):
            path = release_dir / executable
            if not path.is_file() or not os.access(path, os.X_OK):
                raise ReviewBoundaryError(
                    f"Codex release is missing executable {executable}: {release_dir}"
                )
        command.extend(["--volume", f"{release_dir}:/opt/codex/bin:ro"])
    command.extend([image, *inner_command])
    return command


def re_fullmatch_container_name(name: str) -> bool:
    """Docker-compatible conservative name validation without shell parsing."""
    return bool(name) and len(name) <= 128 and all(
        ch.isalnum() or ch in "_.-" for ch in name
    )


def build_image(image: str = DEFAULT_IMAGE) -> None:
    _run(
        [
            "docker",
            "build",
            "--pull=false",
            "--tag",
            image,
            "--file",
            str(DOCKERFILE),
            str(DOCKERFILE.parent),
        ],
        cwd=ROOT,
        timeout=900,
    )


def ensure_image(image: str = DEFAULT_IMAGE) -> None:
    exists = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, text=True
    )
    if exists.returncode != 0:
        build_image(image)


def _prepare_reviewer_home(path: Path, auth_file: Path) -> None:
    auth_file = auth_file.expanduser().resolve(strict=True)
    codex_home = path / ".codex"
    codex_home.mkdir(parents=True, mode=0o700)
    target = codex_home / "auth.json"
    shutil.copyfile(auth_file, target)
    target.chmod(0o600)


def _preflight_command(candidate_sha: str) -> list[str]:
    script = r'''
actual=$(git rev-parse HEAD)
test "$actual" = "$1"
test -z "$(git remote)"
test -z "$(git status --porcelain=v1 --untracked-files=all)"
if touch /workspace/.holophyte-review-write-probe 2>/tmp/write-probe.err; then
  rm -f /workspace/.holophyte-review-write-probe
  echo "workspace write unexpectedly succeeded" >&2
  exit 41
fi
test ! -e /var/run/docker.sock
printf 'PREFLIGHT_OK candidate=%s uid=%s remotes=0 workspace=read-only docker_socket=absent\n' "$actual" "$(id -u)"
'''.strip()
    return ["/bin/sh", "-eu", "-c", script, "preflight", candidate_sha]


def run_review(
    *,
    repo: Path,
    base_sha: str,
    candidate_sha: str,
    prompt: str,
    profile: str = DEFAULT_PROFILE,
    image: str = DEFAULT_IMAGE,
    auth_file: Path = DEFAULT_CODEX_AUTH,
    scratch_root: Path = DEFAULT_SCRATCH_ROOT,
    timeout: int = 1800,
) -> str:
    """Run one exact-SHA review and return the reviewer's combined output."""
    ensure_image(image)
    codex = Path(shutil.which("codex") or "")
    if not codex:
        raise ReviewBoundaryError("Codex CLI is not installed")
    codex = codex.resolve(strict=True)

    scratch_root = scratch_root.expanduser()
    scratch_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="review.", dir=scratch_root) as temporary:
        root = Path(temporary)
        stage = stage_candidate(repo, root / "candidate", base_sha, candidate_sha)
        reviewer_home = root / "home"
        reviewer_home.mkdir(mode=0o700)
        _prepare_reviewer_home(reviewer_home, auth_file)

        common = dict(
            image=image,
            workspace=stage.path,
            reviewer_home=reviewer_home,
            uid=os.getuid(),
            gid=os.getgid(),
            codex_release_dir=codex.parent,
        )
        prefix = "holophyte-" + root.name.replace(".", "-")
        preflight_name = prefix + "-preflight"
        review_name = prefix + "-model"
        try:
            preflight = container_command(
                **common,
                container_name=preflight_name,
                inner_command=_preflight_command(stage.candidate_sha),
            )
            preflight_result = _run(preflight, timeout=120)
            if "PREFLIGHT_OK" not in preflight_result.stdout:
                raise ReviewBoundaryError(
                    "container preflight did not emit its success marker"
                )

            review = container_command(
                **common,
                container_name=review_name,
                inner_command=profile_command(profile, prompt),
            )
            result = _run(review, timeout=timeout)
            transcript = (result.stdout + "\n" + result.stderr).strip()
            validate_review_transcript(transcript)
            return transcript
        finally:
            for name in (preflight_name, review_name):
                subprocess.run(
                    ["docker", "rm", "--force", name],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            observed = candidate_fingerprint(stage.path)
            if observed != stage.fingerprint:
                raise ReviewBoundaryError(
                    "candidate repository changed during read-only review"
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    args = parser.parse_args()
    prompt = args.prompt_file.read_text()
    print(
        run_review(
            repo=args.repo,
            base_sha=args.base,
            candidate_sha=args.candidate,
            prompt=prompt,
            profile=args.profile,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
