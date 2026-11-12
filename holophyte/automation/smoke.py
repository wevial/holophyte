"""The smoke check: a project's own command on main's head, on a schedule."""
import subprocess
import tempfile

import store.automations
import store.board
from holophyte.admission import project_of
from holophyte.automation.automation_config import (
    ACT,
    DRY_RUN,
    SMOKE,
    smoke_config,
    unapproved,
)
from holophyte.automation.policy import report_only_reason
from holophyte.board.provider import board_for
from holophyte.config.config_tables import verify_config
from holophyte.loop.claim import run_worktree_setup
from holophyte.loop.gates import run_capped, sh
from holophyte.loop.runs import open_store
from holophyte.pr import github
from holophyte.redact import redact_prose

REVISION = 1
SUBJECT = "main"
TAIL_LINES = 60
HOUR_MS = 60 * 60 * 1000
LEASE_MS = 5 * 60 * 1000
ESTIMATE_MIN = 60


def smoke_firings(conn, project):
    return [firing for firing in store.automations.firings(conn)
            if firing.projectId == project and firing.automation == SMOKE]


def open_filed(board, fired):
    filed = list(dict.fromkeys(firing.detail["ticket"] for firing in fired
                               if firing.detail.get("ticket")))
    closed = board.closed_identifiers(filed) if filed else {}
    return next((key for key in filed if key not in closed), None)


def run_smoke(target, command):
    """`(sha, exit status or why it has none, output)` at origin/main's head."""
    sh(["git", "fetch", "--quiet", "origin", "main"], target.path)
    sha = sh(["git", "rev-parse", "origin/main"], target.path).strip()
    target.worktrees.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="smoke-",
                                     dir=target.worktrees) as tmp:
        tree = f"{tmp}/tree"
        sh(["git", "worktree", "add", "--detach", "--quiet", tree, sha],
           target.path)
        try:
            ready, report = run_worktree_setup(target, tree)
            if not ready:
                return sha, "failed its worktree setup", report
            try:
                status, out = run_capped(command, tree,
                                         verify_config(target).timeout_sec)
            except subprocess.TimeoutExpired as expired:
                status = f"timed out after {expired.timeout}s"
                out = expired.output or ""
        finally:
            sh(["git", "worktree", "remove", "--force", tree], target.path)
    return sha, status, out


def title(sha):
    return f"Smoke check fails on main at {sha[:12]}"


def ticket_body(sha, command, status, out):
    tail = redact_prose("\n".join(out.splitlines()[-TAIL_LINES:]))
    quoted = "\n".join(f"> {line}" for line in ["```", *tail.splitlines(),
                                                "```"])
    exited = (f"exited with status {status}" if isinstance(status, int)
              else status)
    return (f"# {title(sha)}\n\n## Summary\n\n"
            f"The scheduled smoke check `{command}` {exited} on main at"
            f" `{sha}`.\n\n## What / Why / How\n\n**What:** the smoke check"
            " passes on main again.\n\n**Why:** main fails its own smoke"
            " check.\n\n**How:** fix the cause the output below shows.\n\n"
            "## In scope\n\n- The fix that makes the smoke check pass.\n\n"
            "## Out of scope\n\n- Changing the smoke command.\n\n"
            "## Acceptance criteria\n\n- [ ] Given main with the fix, when"
            f" `{command}` runs, then it exits 0.\n\n"
            f"## Verify command(s)\n\n```\n{command}\n```\n\n"
            "## Implementation notes\n\n- The smoke check ran in a fresh"
            f" detached worktree of main; its last {TAIL_LINES} lines of"
            f" output:\n\n{quoted}\n\n## Estimate & dependencies\n\n"
            f"Estimate: {ESTIMATE_MIN} min · Depends on: none\n\n"
            "## Open questions\n\n- None\n")


def record(conn, project, mode, sha, now, reason,
           state=store.automations.CONFIRMED):
    """A firing settled as `state`; `None` leaves it `proposed`."""
    firing = store.automations.reserve_firing(
        conn, project, SMOKE, REVISION, SUBJECT, f"{sha}@{now}", mode=mode,
        reason=reason, detail={"sha": sha}, now=now,
        lease_until=None if state is None else now + LEASE_MS)
    if state is not None:
        store.automations.settle_firing(conn, firing, None, state, reason,
                                        now=now)
    return firing


def file_failure(board, conn, project, fired, sha, failure, now):
    heading = title(sha)
    firing = store.automations.reserve_firing(
        conn, project, SMOKE, REVISION, SUBJECT, f"{sha}@{now}", mode=ACT,
        reason=f"filing {heading}", detail={"sha": sha}, now=now,
        lease_until=now + LEASE_MS)
    state = store.automations.CONFIRMED
    try:
        key = open_filed(board, fired)
        if key is not None:
            reason = f"already filed {key}"
        else:
            key = board.file(heading, ticket_body(sha, *failure), ESTIMATE_MIN,
                             "Todo")
            reason = f"filed {key}: {heading}"
    except store.board.FilingRefused as refused:
        state, reason = store.automations.REFUSED, f"filing refused: {refused}"
    except Exception as bad:
        store.automations.settle_firing(
            conn, firing, None, store.automations.UNKNOWN,
            f"filing {heading} failed: {bad}", now=now)
        raise
    store.automations.settle_firing(
        conn, firing, None, state, reason, now=now,
        detail={"sha": sha, **({"ticket": key} if key else {})})
    return firing


def smoke_one(target, listed, now):
    config = smoke_config(target)
    if config.mode not in (DRY_RUN, ACT):
        return None
    conn = open_store(target)
    try:
        project = project_of(conn, target)
        if (project is None
                or store.automations.automation_pause(conn, SMOKE) is not None):
            return None
        fired = smoke_firings(conn, project)
        if fired and now - fired[0].createdAt < config.every_hours * HOUR_MS:
            return None
        board = board_for(target)
        if config.mode == ACT and board is None:
            raise RuntimeError("the project has no board to file on")
        sha, status, out = run_smoke(target, config.command)
        if status == 0:
            return record(conn, project, config.mode, sha, now,
                          f"smoke passed at {sha[:12]}")
        pull = github._origin_pull(target)
        proposal = f"would file {title(sha)}"
        if config.mode == ACT:
            held = (unapproved(config, REVISION, SMOKE)
                    or (pull and report_only_reason(pull, listed)))
            if held is None:
                return file_failure(board, conn, project, fired, sha,
                                    (config.command, status, out), now)
            proposal = f"{proposal}; dry run: {held}"
        return record(conn, project, DRY_RUN, sha, now, proposal, state=None)
    finally:
        conn.close()


def run_smoke_checks(entries, listed, now, stop, out, key):
    for entry in entries:
        if stop.is_set():
            return
        try:
            smoke_one(entry.target, listed, now)
        except (Exception, SystemExit) as bad:  # noqa: BLE001 - the boundary
            print(f"[holo2] {key(entry)}: smoke check failed: {bad}", file=out)
