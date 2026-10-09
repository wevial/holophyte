import os
import shutil
import subprocess
import sys

import review_runner
from holophyte.agents import harness
from holophyte.config.agent_settings import (
    agent_command,
    budget_scale,
    fallback_entries,
    implementer_session,
    review_mode,
    review_route,
)
from holophyte.config.config_tables import (
    BOARD_PREFIX_ALIAS,
    board_config,
    loop_config,
    merge_config,
    report_config,
    story_config,
    sweep_config,
    trim_config,
    verify_config,
)
from holophyte.config.reader import (
    AGENT_CONFIG_KEYS,
    DEFAULT_IMPLEMENTER,
    DEFAULT_REVIEWER,
    DOCKER_PROBE_TIMEOUT,
    KNOWN_KEYS,
    adversary_credential,
)
from holophyte.config.review_settings import review_config
from holophyte.config.serve_settings import console_config, serve_config
from holophyte.config.worktree_settings import (
    branch_prefix,
    capture_environment,
    carry_directories,
    setup_commands,
    setup_timeout,
    worktree_environment,
)


def check_config_keys(project):
    for table, known in KNOWN_KEYS.items():
        section = project.config().get(table)
        if not isinstance(section, dict):
            continue
        for key in section:
            if key not in known:
                raise SystemExit(
                    f"[holo2] {project.config_path}: [{table}] {key}: unknown key; "
                    f"[{table}] accepts: {', '.join(sorted(known))}")


def check_config(project):
    merge = merge_config(project)
    from holophyte.questions import settings
    try:
        settings(project.config())
    except ValueError as error:
        raise SystemExit(str(error)) from None
    verify_config(project)
    check_config_keys(project)
    board_alias_notice(project)
    harness.check_target(project)
    budget_scale(project)
    review_mode(project)
    implementer_session(project)
    from holophyte.agents.fix_session import resume_template
    resume_template(project)
    from holophyte.isolation.launcher import route_for
    route = route_for(project)
    if merge.ui_capture and route.backend == "container" and not route.writable:
        raise SystemExit(
            "[merge] ui_capture requires [agents] implementer_isolation "
            "writable = true for its worktree output directory")
    check_agent_fallbacks(project)
    adversary_credential(project)
    sweep_config(project)
    loop_config(project)
    report_config(project)
    story_config(project)
    trim_config(project)
    review_config(project)
    console_config(project)
    serve_config(project)


def board_alias_notice(project):
    table = project.config().get("board")
    if isinstance(table, dict) and BOARD_PREFIX_ALIAS in table:
        print(f"[holo2] {project.config_path}: [board] key is deprecated; "
              "rename it to prefix", file=sys.stderr)


def check_document(project):
    check_config(project)
    board_config(project)
    review_route(project)
    for role, key in AGENT_CONFIG_KEYS.items():
        argv = agent_command(project, role, "")
        if argv is not None:
            check_command_path(project, key, argv[0])
    check_worktree_setup(project)


def check_agent_fallbacks(project):
    for role, key in AGENT_CONFIG_KEYS.items():
        for entry in range(len(fallback_entries(project, role))):
            fallback = agent_command(project, role, "", fallback=True,
                                     entry=entry)
            if fallback == agent_command(project, role, ""):
                raise SystemExit(f"[holo2] {project.config_path}: [agents] "
                                 f"{key}_fallback may not equal {key}")
            check_command_path(project, key + "_fallback", fallback[0])
    review_route(project)
    review_route(project, fallback=True)


def check_agent_commands(project):
    from holophyte.isolation.launcher import route_for
    isolated = route_for(project).backend == "container"
    review_route(project)
    unused = () if review_config(project).adversary else ("adversary",)
    default_container_keys = []
    for role, key in AGENT_CONFIG_KEYS.items():
        argv = agent_command(project, role, "")
        if role in ("write", "trim") and argv is not None:
            check_command_path(project, key, argv[0])
        # The critic and trimmer are optional: their probes, not the host, settle them.
        if (role in ("write", "critic", "trim", *unused)
                or (role == "implement" and isolated)):
            continue
        if argv is None:
            if agent_command(project, role, "", fallback=True) is not None:
                continue
            if role == "implement":
                check_default_implementer(project)
            else:
                default_container_keys.append(key)
            continue
        program = argv[0]
        check_command_path(project, key, program)
        if (shutil.which(program) is None
                and agent_command(project, role, "", fallback=True) is None):
            raise SystemExit(
                f"[holo2] {project.config_path}: [agents] {key}: no executable "
                f"{program!r} on PATH")
    if default_container_keys:
        check_default_reviewer(project, default_container_keys)
    if merge_config(project).mode == "pr":
        # Deferred: `holophyte.pr.github` imports the gates, which import this.
        from holophyte.pr.github import check_pr_route
        check_pr_route(project)


def check_command_path(project, key, program):
    if os.path.dirname(program) and not os.path.isabs(program):
        raise SystemExit(
            f"[holo2] {project.config_path}: [agents] {key}: relative command path "
            f"{program!r} -- rounds run in a task worktree, so name the "
            f"program by an absolute path or leave it to PATH")


def check_default_implementer(project):
    if shutil.which(DEFAULT_IMPLEMENTER) is None:
        raise SystemExit(
            f"[holo2] {project.config_path}: [agents] implementer is not set, so the "
            f"implementer runs `{DEFAULT_IMPLEMENTER}`, and there is no "
            f"executable {DEFAULT_IMPLEMENTER!r} on PATH -- install the Claude "
            f"CLI or set [agents] implementer to the command to run instead")


def check_default_reviewer(project, keys):
    unset = " and ".join(keys)
    remedy = (f"start the Docker daemon or set [agents] {unset} to the "
              f"command to run instead")
    if shutil.which(DEFAULT_REVIEWER) is None:
        raise SystemExit(
            f"[holo2] {project.config_path}: [agents] {unset} not set, so the review "
            f"runs in a `{DEFAULT_REVIEWER}` container ({review_runner.IMAGE}), "
            f"and there is no executable {DEFAULT_REVIEWER!r} on PATH -- "
            f"install Docker or set [agents] {unset} to the command to run "
            f"instead")
    probe = docker_probe(project, ["info"], unset, remedy)
    if probe.returncode:
        detail = (probe.stderr or probe.stdout).strip().splitlines()
        reason = detail[-1] if detail else f"exit {probe.returncode}"
        raise SystemExit(
            f"[holo2] {project.config_path}: [agents] {unset} not set, so the review "
            f"runs in a `{DEFAULT_REVIEWER}` container, and the Docker daemon "
            f"did not answer `{DEFAULT_REVIEWER} info`: {reason} -- {remedy}")
    image = docker_probe(project, ["image", "inspect", review_runner.IMAGE],
                         unset, remedy)
    if image.returncode:
        print(f"[holo2] review image {review_runner.IMAGE} is not built on this "
              f"host; the first review round builds it from "
              f"{review_runner.DOCKERFILE}")


def docker_probe(project, args, unset, remedy):
    argv = [DEFAULT_REVIEWER, *args]
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=DOCKER_PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise SystemExit(
            f"[holo2] {project.config_path}: [agents] {unset} not set, so the review "
            f"runs in a `{DEFAULT_REVIEWER}` container, and the Docker daemon "
            f"did not answer `{' '.join(argv)}` within "
            f"{DOCKER_PROBE_TIMEOUT}s -- {remedy}") from None


def check_worktree_setup(project):
    setup_commands(project)
    setup_timeout(project)
    branch_prefix(project)
    carry_directories(project)
    worktree_environment(project)
    capture_environment(project)
