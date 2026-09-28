"""One implementer launch seam; host execution remains the default."""

import contextlib
import hashlib
import os
import re
import shlex
import signal
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import review_runner
from holophyte.gates import run_capped
from holophyte.redact import register_values


@dataclass(frozen=True)
class Route:
    backend: str = "none"
    image: str = review_runner.IMAGE
    credential: dict = field(default_factory=dict)
    memory: str = "4g"
    writable: bool = True


def route_for(project):
    from holophyte.config import config_table

    table = config_table(project, "agents")
    value = table.get("implementer_isolation", "none")
    options = value if isinstance(value, dict) else {"backend": value}
    if set(options) - {"backend", "memory", "writable"}:
        raise SystemExit("[agents] implementer_isolation: unknown option")
    backend = options.get("backend", "none")
    if backend not in ("none", "container"):
        raise SystemExit("[agents] implementer_isolation must be none or container")
    memory = options.get("memory", "4g")
    if not isinstance(memory, str) or not re.fullmatch(r"[1-9][0-9]*[mg]", memory):
        raise SystemExit(
            "[agents] implementer_isolation memory must be a positive m/g size"
        )
    writable = options.get("writable", True)
    if not isinstance(writable, bool):
        raise SystemExit("[agents] implementer_isolation writable must be boolean")
    image = table.get("implementer_image", review_runner.IMAGE)
    if not isinstance(image, str) or not re.fullmatch(r"[\w][\w./:@-]*", image):
        raise SystemExit("[agents] implementer_image must be an image name")
    credential = table.get("implementer_credential", {})
    validate_credential(credential)
    return Route(backend, image, credential, memory, writable)


def validate_credential(value):
    valid = isinstance(value, dict)
    if valid and set(value) == {"env"}:
        valid = isinstance(value["env"], str) and bool(
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value["env"])
        )
    elif valid and set(value) == {"file", "destination"}:
        valid = all(isinstance(v, str) and v and ":" not in v for v in value.values())
        destination = Path(value["destination"]) if valid else Path("/")
        valid = (
            valid
            and destination.is_relative_to("/home/implementer")
            and (
                ".." not in destination.parts
                and destination != Path("/home/implementer")
            )
        )
    else:
        valid = valid and not value
    if not valid:
        raise SystemExit(
            '[agents] implementer_credential requires {env="NAME"} or '
            '{file="PATH", destination="/home/implementer/..."}'
        )


def environment(project):
    """A container turn's `[worktree]` values, dotenv quotes removed."""
    from holophyte.config import process_value, worktree_environment

    if route_for(project).backend == "none":
        return None
    values = worktree_environment(project) or {}
    return {name: process_value(value) for name, value in values.items()}


def image_ready(route):
    result = subprocess.run(
        ["docker", "image", "inspect", route.image],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": os.defpath},
    )
    if result.returncode:
        build = shlex.join(
            [
                "docker",
                "build",
                "-t",
                route.image,
                "-f",
                str(review_runner.DOCKERFILE),
                str(review_runner.ROOT),
            ]
        )
        raise RuntimeError(
            f"implementer image {route.image} is not built; run: {build}"
        )


RESERVED_DESTINATIONS = (Path("/workspace"), Path("/home/implementer"))


def file_mount_flags(mounts):
    flags = []
    for path in mounts:
        source = Path(path).resolve()
        if any(source.is_relative_to(reserved) for reserved in RESERVED_DESTINATIONS):
            raise RuntimeError(f"file mount {source} lands in the workspace or home")
        if not source.is_file() or ":" in str(source):
            raise RuntimeError(f"file mount {source} must be a regular file")
        flags += ["--volume", f"{source}:{source}:ro"]
    return flags


def private_directory(path):
    path = path.resolve()
    if ":" in str(path):
        raise RuntimeError(f"bind source {path} must not contain a colon")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def project_state(task, project):
    from holophyte.isolation_git import git
    from holophyte.project import state_dir

    root = project.path if project is not None else Path(
        git(task, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ).parent
    return state_dir(root)


def session_directory(task, project):
    task = Path(task).resolve(strict=True)
    digest = hashlib.sha256(str(task).encode()).hexdigest()[:16]
    return private_directory(project_state(task, project) / "sessions" / digest)


CACHE = "/home/implementer/.cache"
CACHE_ENVIRONMENT = {
    "GOPATH": f"{CACHE}/go",
    "GOMODCACHE": f"{CACHE}/go/pkg/mod",
    "GOCACHE": f"{CACHE}/go-build",
    "GOTMPDIR": f"{CACHE}/go-tmp",
    "npm_config_cache": f"{CACHE}/npm",
    "BUN_INSTALL_CACHE_DIR": f"{CACHE}/bun",
    "TMPDIR": "/tmp",
}


def cache_directory(task, project):
    try:
        state = project_state(Path(task).resolve(strict=True), project)
    except subprocess.CalledProcessError:
        return None
    cache = private_directory(state / "cache")
    (cache / "go-tmp").mkdir(exist_ok=True)
    return cache


def container_command(route, worktree, env, argv, name, mounts=(), *, task=None,
                      project=None, cache_for=None):
    uid, gid = os.getuid(), os.getgid()
    if uid == 0:
        raise RuntimeError("container implementer requires a non-root factory user")
    workspace = Path(worktree).resolve(strict=True)
    if ":" in str(workspace):
        raise RuntimeError("worktree bind source must not contain a colon")
    command = [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--name",
        name,
        *review_runner.hardening_flags(uid, gid, route.memory),
        "--tmpfs",
        f"/tmp:rw,nosuid,nodev,noexec,size=1g,uid={uid},gid={gid},mode=1777",
        "--tmpfs",
        f"/home/implementer:rw,nosuid,nodev,size=256m,uid={uid},gid={gid}",
        "--volume",
        f"{workspace}:/workspace:{'rw' if route.writable else 'ro'}",
    ]
    if route.writable and task is not None:
        session = session_directory(task, project)
        command += ["--volume", f"{session}:/home/implementer/.claude:rw"]
    cache = cache_directory(cache_for, project) if cache_for is not None else None
    caches = {} if cache is None else CACHE_ENVIRONMENT
    if cache is not None:
        command += ["--volume", f"{cache}:{CACHE}:rw"]
    credential = route.credential
    if "file" in credential:
        source = Path(credential["file"]).expanduser().resolve(strict=True)
        if not source.is_file() or ":" in str(source):
            raise RuntimeError("implementer credential must be a regular file")
        command += ["--volume", f"{source}:{credential['destination']}:ro"]
    command += file_mount_flags(mounts)
    values = dict(
        env or {},
        **caches,
        HOME="/home/implementer",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL="/dev/null",
    )
    command += [f"--env={key}" for key in values]
    host_env = {"PATH": os.defpath, **values}
    if "env" in credential:
        key = credential["env"]
        if key not in os.environ:
            raise RuntimeError(f"implementer credential variable {key} is not set")
        host_env[key] = os.environ[key]
        register_values([host_env[key]])
        command += [f"--env={key}"]
    return command + [route.image, *argv], host_env


@contextlib.contextmanager
def unwinding_on_signal(name):
    """Unwind cleanup (including Git restoration) before terminating."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def stop(signum, frame):
        review_runner._remove_container(name, env={"PATH": os.defpath})
        raise SystemExit(128 + signum)

    previous = {sig: signal.signal(sig, stop) for sig in review_runner.REMOVAL_SIGNALS}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def launch(route, worktree, env, argv, *, timeout=1800, on_start=None, runner=None,
           project=None, mounts=(), keep_session=False):
    """Preserve host process semantics; always remove isolated descendants."""
    hook = {"on_start": on_start} if on_start is not None else {}
    if route.backend == "none":
        kwargs = {} if env is None else {"env": env}
        return (runner or run_capped)(argv, worktree, timeout, **hook, **kwargs)
    if route.backend != "container":
        raise ValueError(f"unknown isolation backend: {route.backend}")
    from holophyte.isolation_clone import turn_clone

    image_ready(route)
    name = "holophyte-implement-" + uuid.uuid4().hex
    checkout = (turn_clone(worktree, project) if route.writable
                else contextlib.nullcontext((worktree, {})))
    with unwinding_on_signal(name), checkout as (workspace, git_env):
        command, host_env = container_command(
            route, workspace, dict(env or {}, **git_env), argv, name, mounts,
            task=worktree if keep_session else None, project=project,
            cache_for=worktree,
        )
        try:
            return run_capped(command, workspace, timeout, env=host_env, **hook)
        finally:
            review_runner._remove_container(name, env={"PATH": os.defpath})
