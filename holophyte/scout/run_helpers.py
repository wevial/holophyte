import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

from holophyte.agents.harness import SCOUT_RUNNER
from holophyte.agents.probes import PROBE_TIMEOUT
from holophyte.agents.review_workspace import scratch_git_environment
from holophyte.isolation import launcher
from holophyte.loop.gates import RunFailure, sh
from holophyte.loop.task_worktree import prepare_worktree
from holophyte.scout.scout_probes import probe_scout_route
from store.enums import ScoutKind, ScoutTaskMode, ScoutTaskPhase
from store.scouts import record_scout_event

HELPER_OPENING = "Scout helper turn"
READ_ONLY = ("You help a scout: a read-only investigation of this checkout. "
             "Read and search only: do not edit files, commit or push.")
RUN_PERMISSION = (
    "You help a scout reproduce a bug in this checkout, your own worktree. "
    "You may edit files and run the project's tests in this worktree, and "
    "should try to write a failing test that shows the bug. You must not "
    "commit to, create or move any branch, and must not push.")
RUN_REPORT = (
    "Report each command you ran with its exit status and an excerpt of its "
    "output, and the reproduction level you reached: test (a failing test "
    "shows the bug), real_tool (a command shows the bad behaviour) or "
    "not_reproduced.\n")
CLAIMED = ("test", "real_tool")
NO_RUN_FINISHED = "no run helper finished"
REF_KINDS = ("refs/heads", "refs/tags")
LAUNCH_FAILURES = (OSError, RuntimeError, RunFailure)
PRE_PUSH = "#!/bin/sh\necho 'a scout run helper does not push' >&2\nexit 1\n"


def runs(row, task):
    return (row.kind == ScoutKind.BUG.value
            and task.mode == ScoutTaskMode.RUN.value)


def task_fields(row, task):
    paths = ", ".join(task.paths) or "none"
    return (f"Ask: {row.ask}\nKind: {row.kind}\nBase commit: {row.base_sha}\n\n"
            f"Question: {task.question}\nPaths: {paths}\nMode: {task.mode}\n\n")


def run_goal(row, task):
    return (f"{HELPER_OPENING}\n\n{RUN_PERMISSION}\n\n"
            f"{task_fields(row, task)}{RUN_REPORT}")


def run_seat(project, seat):
    binary = seat.binary
    if launcher.route_for(project).backend == "container":
        binary = seat.adapter.binary
    return replace(seat, binary=binary, role=SCOUT_RUNNER)


def set_up(project, checkout, deadline):
    """The setup's failure report, or None once every entry has run."""
    try:
        ok, report, _ = prepare_worktree(project, checkout, deadline=deadline)
    except LAUNCH_FAILURES as failed:
        return f"[holo2] worktree setup could not launch: {failed}"
    return None if ok else report


def isolated_probe(project, route, seat, checkout, remaining_s):
    if launcher.route_for(project).backend != "container":
        return None
    probe = probe_scout_route(
        project, f"{SCOUT_RUNNER}:{route}", seat,
        min(PROBE_TIMEOUT, remaining_s),
        run=lambda project, argv, cap: launch_turn(project, argv, checkout,
                                                    cap))
    if probe.ok:
        return None
    return f"helper route {route} is down in the container: {probe.reason()}"


def launch_turn(project, argv, checkout, cap):
    return launcher.launch(launcher.turn_route(project, argv), checkout,
                           launcher.environment(project), argv, timeout=cap,
                           project=project)


def reproduced(row, swarm, report):
    reproduction = report.get("reproduction")
    if (row.kind != ScoutKind.BUG.value or not isinstance(reproduction, dict)
            or reproduction.get("level") not in CLAIMED):
        return report
    if any(runs(row, task)
           and swarm.answered(task).phase == ScoutTaskPhase.DONE.value
           for task in swarm.tasks):
        return report
    return dict(report, reproduction=dict(
        reproduction, level="not_reproduced",
        claimed_level=reproduction["level"], why=NO_RUN_FINISHED))


def run_clone(project, scout_id, task_id, base_sha):
    from holophyte.scout.scout_process import scout_dir

    path = scout_dir(project, scout_id) / f"run-{task_id}"
    env = scratch_git_environment()
    shutil.rmtree(path, ignore_errors=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    sh(["git", "-c", "init.templateDir=", "clone", "--shared", "--no-checkout",
        "--quiet", str(project.path), str(path)], cwd=project.path, env=env)
    sh(["git", "remote", "remove", "origin"], cwd=path, env=env)
    sh(["git", "checkout", "--detach", "--quiet", base_sha], cwd=path, env=env)
    common = Path(sh(["git", "rev-parse", "--path-format=absolute",
                      "--git-common-dir"], cwd=project.path, env=env))
    if (common / "info" / "exclude").is_file():
        (path / ".git" / "info").mkdir(exist_ok=True)
        shutil.copyfile(common / "info" / "exclude",
                        path / ".git" / "info" / "exclude")
    hook = path / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(PRE_PUSH)
    hook.chmod(0o755)
    for key in ("user.name", "user.email"):
        value = subprocess.run(["git", "config", "--get", key],
                               cwd=project.path, env=env, capture_output=True,
                               text=True).stdout.strip()
        if value:
            sh(["git", "config", key, value], cwd=path, env=env)
    return path


def clone_refs(clone):
    listed = sh(["git", "for-each-ref",
                 "--format=%(refname) %(objectname) %(symref)", *REF_KINDS],
                cwd=clone, env=scratch_git_environment())
    return dict(line.split(" ", 1) for line in listed.splitlines())


def change(old, new):
    if old is None:
        return "created"
    return "deleted" if new is None else "moved"


def record_refs(conn, scout_id, before, after):
    changed = [f"{ref} ({change(before.get(ref), after.get(ref))})"
               for ref in sorted(before.keys() | after.keys())
               if before.get(ref) != after.get(ref)]
    if changed:
        record_scout_event(
            conn, scout_id, "scout_refs",
            "a run helper turn changed refs in its own clone: "
            + ", ".join(changed))
