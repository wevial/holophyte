"""Required checks that never report on a pull request's head (KO-652).

A check the base branch requires but that has reported nothing -- no run,
no status -- is not a slow check: the babysitter's wait notices it after
`[merge] missing_check_sec`, wakes it with one empty commit per candidate
when `[merge] retrigger_missing_checks` is on, and otherwise parks naming it.
"""
import store
from holophyte import pr
from holophyte.config_tables import merge_config
from holophyte.gates import sh
from holophyte.pr_head import _just_pushed_state
from holophyte.redact import safe_print as print
from holophyte.stop import stop_if_requested


class Retrigger:
    """One empty commit per candidate to wake the required checks that never
    reported on it (KO-652), pushed on the babysitter's own push path so its
    later pushes stay fast-forward. `sha` and `reviewed` follow the push:
    an empty commit changes no tree the last review covered."""

    def __init__(self, run, beat_s, pull, sha, reviewed, woken):
        self.run, self.beat_s, self.pull = run, beat_s, pull
        self.sha, self.reviewed, self.woken = sha, reviewed, woken

    def __call__(self, names):
        """The pushed head's state, or None when `[merge]
        retrigger_missing_checks` is off or the head is itself a retrigger."""
        run = self.run
        if (not merge_config(run.target).retrigger_missing_checks
                or self.sha in self.woken):
            return None
        stop_if_requested(run.conn, run.run_id, "merge_gate")
        listed = ", ".join(names)
        sh(["git", "commit", "--allow-empty", "-m",
            f"Retrigger missing checks: {listed}"], cwd=run.wt)
        pr.push_branch(run.target, run.branch)
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
        self.woken.add(sha)
        return _just_pushed_state(
            run.target, run.conn, run.run_id, run.provider, run.task_id,
            run.branch, sha, self.beat_s, self.pull, self.reviewed)


def unreported(state, absent, limit_s, now):
    """The required checks the head has carried no report of for `limit_s`
    seconds at monotonic `now`; `absent` keeps when each (head, check) was
    first seen so, and forgets it once the check reports or the head moves."""
    for key in [k for k in absent if k[0] != state.head_sha
                or k[1] not in state.missing_checks]:
        del absent[key]
    return tuple(name for name in state.missing_checks
                 if now - absent.setdefault((state.head_sha, name), now)
                 >= limit_s)
