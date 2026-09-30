import subprocess

import store
from holophyte.config.config_tables import merge_config
from holophyte.environment_git import factory_identity
from holophyte.loop.gates import sh
from holophyte.loop.stop import stop_if_requested
from holophyte.pr import github
from holophyte.pr.pr_head import _just_pushed_state
from holophyte.redact import safe_print as print

SUBJECT = "Retrigger missing checks: "


class Retrigger:
    """Pushed on the babysitter's own push path, so later pushes fast-forward."""

    def __init__(self, run, beat_s, pull, sha, reviewed):
        self.run, self.beat_s, self.pull = run, beat_s, pull
        self.sha, self.reviewed = sha, reviewed

    def __call__(self, names):
        run = self.run
        if (not merge_config(run.project).retrigger_missing_checks
                or retriggered(run.wt, self.sha)):
            return None
        stop_if_requested(run.conn, run.run_id, "merge_gate")
        listed = ", ".join(names)
        sh(["git", *factory_identity(run.wt), "commit", "--allow-empty", "-m",
            f"{SUBJECT}{listed}"], cwd=run.wt)
        github.push_branch(run.project, run.branch)
        sha = sh(["git", "rev-parse", run.branch], run.wt)
        if run.conn is not None and run.run_id is not None:
            store.record_event(run.conn, run.run_id, "pull_request",
                               f"required checks never reported on {self.sha}:"
                               f" {listed}; pushed empty commit {sha} to"
                               " retrigger them")
        print(f"[holo2] required checks never reported on {self.pull.url}"
              f" ({listed}); pushed empty commit {sha[:12]} to retrigger them")
        if self.reviewed == self.sha:
            self.reviewed = sha
        self.sha = sha
        return _just_pushed_state(
            run.project, run.conn, run.run_id, run.provider, run.task_id,
            run.branch, sha, self.beat_s, self.pull, self.reviewed)


def retriggered(wt, sha):
    """Read off the commit, so a resumed babysit cannot push a second one."""
    def git(*args):
        return subprocess.run(["git", *args], cwd=wt, capture_output=True,
                              text=True)
    subject = git("log", "-1", "--format=%s", sha)
    return (subject.returncode == 0
            and subject.stdout.startswith(SUBJECT)
            and git("diff", "--quiet", f"{sha}^", sha, "--").returncode == 0)


def unreported(state, absent, limit_s, clock):
    for key in [k for k in absent if k[0] != state.head_sha
                or k[1] not in state.missing_checks]:
        del absent[key]
    if not state.missing_checks:
        return ()
    now = clock()
    return tuple(name for name in state.missing_checks
                 if now - absent.setdefault((state.head_sha, name), now)
                 >= limit_s)
