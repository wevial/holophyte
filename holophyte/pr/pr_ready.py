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


def hold_for_frontend(run, pull, state, reviewed):
    from holophyte.pr.pullrequest import _park_on_pr
    if not state.draft or merge_config(run.project).pr_draft != "ui":
        return
    if run.conn is not None and run.run_id is not None:
        store.record_event(run.conn, run.run_id, "pr_draft_held",
                           f"{pull.url} held as a draft at {run.sha}")
    _park_on_pr(run.project, run.conn, run.run_id, run.provider, run.task_id,
                run.branch, run.sha, pull, HELD, (), reviewed=reviewed)


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
