import store
from holophyte.config.config_tables import merge_config
from holophyte.pr import github, pr_activity
from holophyte.redact import safe_print as print

READY_MUTATION = """
mutation($pull: ID!) {
  markPullRequestReadyForReview(input: {pullRequestId: $pull}) {
    pullRequest { isDraft }
  }
}"""
DRAFT = ("the pull request is a draft; mark it ready for review, then"
         " --babysit or --approve the run")
HELD = ("the pull request is a draft held for frontend review; mark it ready"
        " for review when that review is done, and the factory picks the run"
        " up again")


def held_for_frontend(project, state):
    return bool(state.draft) and merge_config(project).pr_draft == "ui"


def record_hold(conn, run_id, pull, sha):
    store.record_event(conn, run_id, "pr_draft_held",
                       f"{pull.url} held as a draft at {sha}")


def hold_for_frontend(run, pull, state, reviewed, before_review=False):
    from holophyte.babysit.babysitter import _fixes_reviewed
    from holophyte.pr.pullrequest import _park_on_pr
    if (not held_for_frontend(run.project, state)
            or before_review and _fixes_reviewed(merge_config(run.project))):
        return
    _park_on_pr(run.project, run.conn, run.run_id, run.provider, run.task_id,
                run.branch, run.sha, pull, HELD, (), reviewed=reviewed,
                held=True)


def mark_or_park(run, pull, state, reviewed, marked):
    """The factory marks its own draft ready once per pull request."""
    from holophyte.pr.pullrequest import _park_on_pr
    hold_for_frontend(run, pull, state, reviewed)
    if (marked or merge_config(run.project).pr_draft is not True
            or pr_activity.latest(run.conn, run.run_id, "pr_ready")):
        _park_on_pr(run.project, run.conn, run.run_id, run.provider,
                    run.task_id, run.branch, run.sha, pull, DRAFT, (),
                    reviewed=reviewed)
    github.graphql(run.project, pull, READY_MUTATION, {"pull": state.node_id})
    if run.conn is not None and run.run_id is not None:
        store.record_event(run.conn, run.run_id, "pr_ready",
                           f"{pull.url} marked ready for review at {run.sha}")
    print(f"[holo2] {pull.url} marked ready for review at {run.sha[:12]}")
    return True
