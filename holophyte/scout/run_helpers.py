import subprocess
from contextlib import contextmanager
from dataclasses import replace

from holophyte.agents.harness import SCOUT_RUNNER
from holophyte.agents.review_workspace import scratch_git_environment
from holophyte.isolation import launcher
from holophyte.loop.gates import sh
from holophyte.loop.task_worktree import prepare_worktree
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
    ok, report, _ = prepare_worktree(project, checkout, deadline=deadline)
    return None if ok else report


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


def ref_values(repo):
    listed = sh(["git", "for-each-ref", "--format=%(refname) %(objectname)",
                 *REF_KINDS], cwd=repo, env=scratch_git_environment())
    return dict(line.split(" ", 1) for line in listed.splitlines())


def ref_resets(before, after):
    for ref in sorted(before.keys() | after.keys()):
        old, new = before.get(ref), after.get(ref)
        if old == new:
            continue
        if old is None:
            yield ref, "created", ["-d", ref, new]
        elif new is None:
            yield ref, "deleted", [ref, old, ""]
        else:
            yield ref, "moved", [ref, old, new]


def put_back(repo, ref, args, before):
    if subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "update-ref",
                       *args], cwd=repo, env=scratch_git_environment(),
                      capture_output=True).returncode == 0:
        return True
    return ref_values(repo).get(ref) == before.get(ref)


def restore_refs(conn, scout_id, repo, before, turn):
    reset, failed = [], []
    for ref, change, args in ref_resets(before, ref_values(repo)):
        (reset if put_back(repo, ref, args, before) else failed).append(
            f"{ref} ({change})")
    if reset or failed:
        summary = f"a {turn} turn left refs behind; put back: " + (
            ", ".join(reset) or "none")
        if failed:
            summary += "; could not put back: " + ", ".join(failed)
        record_scout_event(conn, scout_id, "scout_refs", summary)


@contextmanager
def refs_guard(conn, scout_id, repo, turn, before=None):
    before = ref_values(repo) if before is None else before
    try:
        yield
    finally:
        restore_refs(conn, scout_id, repo, before, turn)
