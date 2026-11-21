import contextlib
import contextvars
import errno
import hashlib
import itertools
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

from holophyte.loop.gates import run_capped
from holophyte.redact import register_values
from holophyte.review import review_runner


@dataclass(frozen=True)
class Route:
    backend: str = "none"
    image: str = review_runner.IMAGE
    credential: dict = field(default_factory=dict)
    memory: str = "4g"
    writable: bool = True
    codex: bool = False
    codex_login: bool = False


CREDENTIAL_NEEDED = contextvars.ContextVar("credential_needed", default=True)


@contextlib.contextmanager
def without_credential():
    reset = CREDENTIAL_NEEDED.set(False)
    try:
        yield
    finally:
        CREDENTIAL_NEEDED.reset(reset)


def runs_codex(value):
    from holophyte.agents.harness import route_text

    try:
        return shlex.split(str(route_text(value) or ""))[:1] == ["codex"]
    except ValueError:
        return False


def route_for(project):
    from holophyte.config.reader import config_table

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
    codex = any(runs_codex(table.get(key))
                for key in ("implementer", "implementer_fallback"))
    codex_login = runs_codex(table.get("implementer_fallback"))
    return Route(backend, image, credential, memory, writable, codex, codex_login)


def container_docker_problems(project, ticket):
    from holophyte.tickets.ticket_template import _shell_commands

    lines = [line for line in ticket.verify_commands
             if any(_needs_docker(tokens) for tokens in _shell_commands(line))]
    if not lines:
        return []
    try:
        backend = route_for(project).backend
    except SystemExit as error:
        return [f"verify command needs Docker, checked against [agents],"
                f" which is malformed: {error}"]
    if backend != "container":
        return []
    return [f"verify command needs Docker: {line}; the project verifies inside"
            " the review container, which has no Docker, so move this check to"
            " an operator witness noted in the criterion, or to a CI job"
            for line in lines]


def _needs_docker(tokens):
    if "HOLOPHYTE_TEST_DOCKER=1" in tokens:
        return True
    words = itertools.dropwhile(
        lambda token: re.fullmatch(r"[A-Za-z_]\w*=.*", token), tokens)
    return next(words, None) == "docker"


def turn_route(project, argv):
    route = route_for(project)
    return replace(route, codex=route.codex or argv[:1] == ["codex"])


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
        if valid and destination in AGY_LOGIN_DESTINATION.parents:
            raise SystemExit(
                f"[agents] implementer_credential destination {destination} "
                "is a directory required by the agy login"
            )
    else:
        valid = valid and not value
    if not valid:
        raise SystemExit(
            '[agents] implementer_credential requires {env="NAME"} or '
            '{file="PATH", destination="/home/implementer/..."}'
        )


def environment(project):
    from holophyte.config.worktree_settings import process_value, worktree_environment

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


CODEX_BIN = "/opt/codex/bin"
AGY_BIN = "/opt/agy/bin"
AGY_LOGIN = PurePosixPath(".gemini/antigravity-cli/antigravity-oauth-token")
AGY_LOGIN_DESTINATION = PurePosixPath("/home/implementer", AGY_LOGIN)
GEMINI_HOME = PurePosixPath("/home/implementer/.gemini")


def readonly_volume(source, destination):
    if ":" in str(source):
        raise RuntimeError(f"bind source {source} must not contain a colon")
    return ["--volume", f"{source}:{destination}:ro"]


def agy_mount_flags():
    found = shutil.which("agy")
    if not found:
        raise RuntimeError("agy executable missing from PATH")
    return readonly_volume(Path(found).resolve(strict=True), f"{AGY_BIN}/agy")


def open_gemini_subdirectory(parent, name):
    flags = os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        return os.open(name, flags, dir_fd=parent)
    except PermissionError:
        os.chmod(name, 0o700, dir_fd=parent, follow_symlinks=False)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            os.unlink(name, dir_fd=parent)
        elif error.errno != errno.ENOENT:
            raise
        with contextlib.suppress(FileExistsError):
            os.mkdir(name, mode=0o700, dir_fd=parent)
    return os.open(name, flags, dir_fd=parent)


def remove_gemini_entry(parent, name, scratch):
    try:
        os.unlink(name, dir_fd=parent)
    except IsADirectoryError:
        os.chmod(name, 0o700, dir_fd=parent, follow_symlinks=False)
        os.replace(name, tempfile.mkdtemp(dir=scratch), src_dir_fd=parent)


@contextlib.contextmanager
def install_gemini_credential(home, destination, scratch, *, temporary):
    relative = destination.relative_to(GEMINI_HOME)
    with contextlib.ExitStack() as directories:
        parent = os.open(home, os.O_DIRECTORY | os.O_NOFOLLOW)
        directories.callback(os.close, parent)
        for name in relative.parent.parts:
            parent = open_gemini_subdirectory(parent, name)
            directories.callback(os.close, parent)
            os.fchmod(parent, 0o700)
        source = scratch / relative.name
        try:
            os.replace(source, relative.name, dst_dir_fd=parent)
        except IsADirectoryError:
            remove_gemini_entry(parent, relative.name, scratch)
            os.replace(source, relative.name, dst_dir_fd=parent)
        try:
            yield
        finally:
            if temporary:
                os.fchmod(parent, 0o700)
                with contextlib.suppress(FileNotFoundError):
                    remove_gemini_entry(parent, relative.name, scratch)


def agy_login_flags(route, task, project, scratch, stack):
    destination = AGY_LOGIN_DESTINATION
    configured = PurePosixPath(route.credential.get("destination", "/"))
    if not CREDENTIAL_NEEDED.get():
        return route, []
    login = {"file": str(Path.home() / AGY_LOGIN), "destination": str(destination)}
    login_dir = (None if configured == destination else stack.enter_context(
        credential_copy(replace(route, credential=login), task, project)))
    staging = Path(login_dir or scratch).parent
    home = (session_directory(task, project, "gemini") if route.writable
            else Path(stack.enter_context(tempfile.TemporaryDirectory(dir=staging))))
    if configured.is_relative_to(GEMINI_HOME):
        stack.enter_context(install_gemini_credential(
            home, configured, scratch, temporary=True))
        route = replace(route, credential={})
    if configured != destination:
        stack.enter_context(install_gemini_credential(
            home, destination, login_dir, temporary=False))
    return route, ["--volume", f"{home}:{GEMINI_HOME}:rw"]


def codex_mount_flags():
    found = shutil.which("codex")
    release = Path(found).resolve(strict=True).parent if found else None
    flags = []
    for name in review_runner.CODEX_FILES:
        source = release / name if release else Path(name)
        if release is None or not source.is_file() or not os.access(source, os.X_OK):
            raise RuntimeError(f"Codex release is missing executable: {source}")
        flags += readonly_volume(source, f"{CODEX_BIN}/{name}")
    return flags


CODEX_HOME = PurePosixPath("/home/implementer/.codex")
CODEX_LOGIN = PurePosixPath("/opt/codex/login/auth.json")


def codex_login_flags(credential, codex_home):
    destination = PurePosixPath(credential.get("destination", "/"))
    if destination == PurePosixPath(codex_home, CODEX_LOGIN.name):
        return []
    home = os.environ.get("CODEX_HOME") or Path.home() / ".codex"
    source = Path(home, CODEX_LOGIN.name).resolve()
    if not source.is_file():
        return []
    return readonly_volume(source, CODEX_LOGIN)


def private_directory(path):
    path = path.resolve()
    if ":" in str(path):
        raise RuntimeError(f"bind source {path} must not contain a colon")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def project_state(task, project):
    from holophyte.config.project import state_dir
    from holophyte.isolation.isolation_git import git

    root = project.path if project is not None else Path(
        git(task, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ).parent
    return state_dir(root)


def session_directory(task, project, group="sessions"):
    task = Path(task).resolve(strict=True)
    digest = hashlib.sha256(str(task).encode()).hexdigest()[:16]
    return private_directory(project_state(task, project) / group / digest)


CACHE = "/home/implementer/.cache"
CACHE_ENVIRONMENT = {
    "GOPATH": f"{CACHE}/go",
    "GOMODCACHE": f"{CACHE}/go/pkg/mod",
    "GOCACHE": f"{CACHE}/go-build",
    "GOTMPDIR": f"{CACHE}/go-tmp",
    "npm_config_cache": f"{CACHE}/npm",
    "BUN_INSTALL_CACHE_DIR": f"{CACHE}/bun",
}


def cache_directory(task, project):
    try:
        state = project_state(Path(task).resolve(strict=True), project)
    except subprocess.CalledProcessError:
        return None
    cache = private_directory(state / "cache")
    (cache / "go-tmp").mkdir(exist_ok=True)
    return cache


def stage_credential(credential, scratch):
    source = Path(credential["file"]).expanduser().resolve(strict=True)
    if not source.is_file() or ":" in str(source):
        raise RuntimeError("implementer credential must be a regular file")
    copy = scratch / PurePosixPath(credential["destination"]).name
    shutil.copyfile(source, copy)
    copy.chmod(0o600)


@contextlib.contextmanager
def credential_copy(route, task, project):
    if "file" not in route.credential:
        yield None
        return
    try:
        parent = project_state(Path(task).resolve(strict=True), project) / "credentials"
    except subprocess.CalledProcessError:
        parent = Path(tempfile.gettempdir())
    scratch = private_directory(parent / f"holophyte-credential-{uuid.uuid4().hex}")
    try:
        stage_credential(route.credential, scratch)
        yield scratch
    finally:
        remove_scratch(scratch)


def remove_scratch(scratch):
    try:
        scratch.chmod(0o700)
        for directory, names, _ in os.walk(scratch):
            for name in names:
                if not (path := Path(directory, name)).is_symlink():
                    path.chmod(0o700)
        shutil.rmtree(scratch)
    except OSError as error:
        raise RuntimeError(
            f"implementer credential copy remains at {scratch}: {error}") from error


def credential_mount_flags(credential, scratch, mounted):
    if scratch is None:
        raise RuntimeError("a file credential needs its private copy")
    destination = PurePosixPath(credential["destination"])
    for mount_point, source in mounted.items():
        if destination.parent == mount_point or (
                source is not None and destination.parent.is_relative_to(mount_point)):
            if source is not None:
                (source / destination.parent.relative_to(mount_point)).mkdir(
                    mode=0o700, parents=True, exist_ok=True)
            return ["--volume", f"{scratch / destination.name}:{destination}:rw"]
    return ["--volume", f"{scratch}:{destination.parent}:rw"]


def container_command(route, worktree, env, argv, name, mounts=(), *, task=None,
                      project=None, cache_for=None, credential_scratch=None,
                      carry=None, git_flags=(), agy_flags=()):
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
        f"/tmp:rw,nosuid,nodev,exec,size=1g,uid={uid},gid={gid},mode=1777",
        "--tmpfs",
        f"/home/implementer:rw,nosuid,nodev,size=256m,uid={uid},gid={gid}",
        "--volume",
        f"{workspace}:/workspace:{'rw' if route.writable else 'ro'}",
        *git_flags,
    ]
    for entry, source in (carry or {}).items():
        source = Path(source).resolve(strict=True)
        if ":" in str(source):
            raise RuntimeError(f"carry bind source {source} must not contain a colon")
        command += ["--volume", f"{source}:{PurePosixPath('/workspace', entry)}:rw"]
    mounted = {PurePosixPath("/home/implementer"): None}
    if route.writable and task is not None:
        session = session_directory(task, project)
        command += ["--volume", f"{session}:/home/implementer/.claude:rw"]
        mounted[PurePosixPath("/home/implementer/.claude")] = session
    cache = cache_directory(cache_for, project) if cache_for is not None else None
    caches = {} if cache is None else CACHE_ENVIRONMENT
    if cache is not None:
        command += ["--volume", f"{cache}:{CACHE}:rw"]
        mounted[PurePosixPath(CACHE)] = cache
    credential = route.credential
    if "file" in credential:
        command += credential_mount_flags(credential, credential_scratch, mounted)
    command += file_mount_flags(mounts)
    command += agy_flags
    if route.codex:
        command += codex_mount_flags()
        home = (env or {}).get("CODEX_HOME") or CODEX_HOME
        login = codex_login_flags(credential, home) if route.codex_login else []
        link = (f'{{ mkdir -p "${{CODEX_HOME:-{CODEX_HOME}}}" && ln -sf {CODEX_LOGIN} '
                f'"${{CODEX_HOME:-{CODEX_HOME}}}/auth.json"; }} 2>/dev/null || '
                f'{{ CODEX_HOME=$(mktemp -d) && export CODEX_HOME && ln -s '
                f'{CODEX_LOGIN} "$CODEX_HOME/auth.json"; }} && ' if login else "")
        command += login
        argv = ["/bin/sh", "-c", f'{link}PATH="{CODEX_BIN}:$PATH" exec "$@"',
                "codex", *argv]
    values = dict(
        env or {},
        **caches,
        HOME="/home/implementer",
        TMPDIR="/tmp",
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


class SignalExit(SystemExit):
    pass


@contextlib.contextmanager
def unwinding_on_signal(name):
    """Unwind cleanup (including Git restoration) before terminating."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def stop(signum, frame):
        review_runner._remove_container(name, env={"PATH": os.defpath})
        raise SignalExit(128 + signum)

    previous = {sig: signal.signal(sig, stop) for sig in review_runner.REMOVAL_SIGNALS}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@contextlib.contextmanager
def container_runtime(route, worktree, project):
    image_ready(route)
    name = "holophyte-implement-" + uuid.uuid4().hex
    if not CREDENTIAL_NEEDED.get():
        if "env" in route.credential:
            register_values([os.environ.get(route.credential["env"], "")])
        route = replace(route, credential={}, codex_login=False)
    with (unwinding_on_signal(name),
          credential_copy(route, worktree, project) as scratch):
        yield route, name, scratch


def run_container(route, workspace, env, argv, name, timeout, hook, **options):
    command, host_env = container_command(route, workspace, env, argv, name, **options)
    try:
        return run_capped(command, workspace, timeout, env=host_env, **hook)
    finally:
        review_runner._remove_container(name, env={"PATH": os.defpath})


def verify_checkout(route, workspace, env, argv, timeout, on_start, project):
    from holophyte.isolation.isolation_git import git

    common = Path(git(workspace, "rev-parse", "--path-format=absolute",
                      "--git-common-dir"))
    admin = Path(git(workspace, "rev-parse", "--absolute-git-dir"))
    mounted = PurePosixPath("/opt/holophyte/git")
    with (tempfile.TemporaryDirectory() as directory,
          container_runtime(route, workspace, project) as (route, name, scratch)):
        gitfile = Path(directory, "gitfile")
        gitdir = mounted / admin.relative_to(common)
        gitfile.write_text(f"gitdir: {gitdir}\n")
        mounts = [(gitfile, "/workspace/.git"), (admin, gitdir)]
        mounts.extend((common / entry, mounted / entry)
                      for entry in ("objects", "refs", "packed-refs", "shallow")
                      if (common / entry).exists())
        flags = []
        for source, destination in mounts:
            if ":" in str(source):
                raise RuntimeError(f"Git bind source {source} must not contain a colon")
            flags += ["--volume", f"{source}:{destination}:ro"]
        return run_container(route, workspace, env, argv, name, timeout,
                             {"on_start": on_start}, project=project,
                             cache_for=workspace, credential_scratch=scratch,
                             git_flags=flags)


def launch(route, worktree, env, argv, *, timeout=1800, on_start=None, runner=None,
           project=None, mounts=(), keep_session=False, carry=None, quiet=None,
           on_event=None, around=None):
    around = around or (lambda checkout: contextlib.nullcontext())
    hook = {"on_start": on_start} if on_start is not None else {}
    if quiet is not None:
        hook["quiet"] = quiet
    if on_event is not None:
        hook["on_event"] = on_event
    if route.backend == "none":
        kwargs = {} if env is None else {"env": env}
        with around(worktree):
            return (runner or run_capped)(argv, worktree, timeout, **hook,
                                          **kwargs)
    if route.backend != "container":
        raise ValueError(f"unknown isolation backend: {route.backend}")
    from holophyte.config.worktree_settings import carry_directories
    from holophyte.isolation.isolation_clone import carry_mounts, turn_clone

    with container_runtime(route, worktree, project) as (route, name, scratch):
        if carry is None:
            carry = carry_directories(project) if project is not None else []
        task = Path(worktree).resolve()
        carry = carry_mounts(task, carry) if route.writable else []
        checkout = (turn_clone(worktree, project, carry) if route.writable
                    else contextlib.nullcontext((worktree, {})))
        with (checkout as (workspace, git_env), around(workspace),
              contextlib.ExitStack() as staged):
            agy_flags = []
            if argv[:1] == ["agy"]:
                agy_flags = agy_mount_flags()
                route, login_flags = agy_login_flags(
                    route, worktree, project, scratch, staged)
                agy_flags += login_flags
                argv = ["/bin/sh", "-c", f'PATH="{AGY_BIN}:$PATH" exec "$@"',
                        "agy", *argv]
            return run_container(
                route, workspace, dict(env or {}, **git_env), argv, name, timeout, hook,
                mounts=mounts, task=worktree if keep_session else None, project=project,
                cache_for=worktree, credential_scratch=scratch,
                carry={entry: task / entry for entry in carry}, agy_flags=agy_flags,
            )
