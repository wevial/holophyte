"""The retire step: after approval, one turn drops run-added tests the gate fails."""
import json
import subprocess
from functools import partial

import store
import store.run_reads
import store.stories
from holophyte.agents.agent_routes import routes
from holophyte.agents.review_workspace import review_base
from holophyte.config.config_tables import trim_config
from holophyte.loop.gates import sh
from holophyte.loop.implement import OUTPUT_TAIL, _commands_brief, run_cap_reason
from holophyte.loop.test_removal import _changed_pairs, removed_outside_scope
from holophyte.loop.trim import (
    _below_swept_wip,
    _commits,
    _green,
    _is_test,
    _land,
    _test_ids,
    _turn,
    _untracked,
    criterion_files,
)
from holophyte.redact import known_secrets, redact_prose
from holophyte.redact import safe_print as print
from holophyte.review.reply_parsing import test_references

SUBJECT = "retire: run-added tests"
NO_CANDIDATE = "no run-added test outside the protected set"
NOT_REPRODUCED = "the run took the not-reproduced path"
TEST_VALUE_GATE = """\
**The test-value gate.** Before adding a test, answer four questions: what
behavior, invariant or contract does it protect? What credible regression
makes it fail? Why does existing coverage not already catch that? Does it
need a production seam no production caller needs? A missing answer means
the test does not land."""
RETIRE_BRIEF = """\
Retire this run's own tests that fail the test-value gate. The change on
this branch is approved; this is not a review and changes no behavior.

The candidates are tests this run added that witness no acceptance
criterion:
{candidates}

The repository's AGENTS.md states the gate:

{gate}

Read each candidate and the code it exercises. Delete only a listed test
that fails the gate, plus a helper or import that only deleted tests used.
Touch no other test, no file that is not a test file, and add no line but
an import. Keep a listed test that passes the gate.

The check command, run from the worktree root, must stay green:
{commands}

Make at most one commit, with subject exactly `{subject}` and no tool
attribution or co-author lines. Its body holds one line per deleted test,
`RETIRED path::name: REASON`, the reason naming the gate question it fails.
If every candidate passes the gate, commit nothing and say so. Never amend,
rebase, branch, merge or push.
"""


def retire(project, conn, run_id, beat_s, wt, base_sha, sha, ticket,
           verify_cmd, contracts, criteria, unreproduced=False):
    config = trim_config(project)
    if not config.enabled:
        return sha
    base = review_base(wt, base_sha, sha)
    added = _run_added(wt, base, sha)
    protected = _protected(conn, run_id, wt, sha, criteria, added)
    candidates = [test for test in added if test not in protected]
    record = partial(_record, project, conn, run_id, candidates=candidates,
                     protected=protected, approved=sha)
    reason = (NOT_REPRODUCED if unreproduced else
              _skip_reason(project, conn, run_id, config.budget_min, sha,
                           candidates))
    if reason:
        record("skipped", reason)
        return sha
    untracked = _untracked(wt)
    goal = RETIRE_BRIEF.format(
        candidates="\n".join(f"- `{test}`" for test in candidates),
        gate=TEST_VALUE_GATE, subject=SUBJECT,
        commands=_commands_brief(project, verify_cmd).strip())
    out, reason = _turn(project, conn, run_id, beat_s, wt, config.budget_min,
                        goal)
    _land(wt, _below_swept_wip(wt, sha), untracked)
    if not reason and not _commits(wt, sha) and _head(wt) == sha:
        record("nothing", None, out=out)
        return sha
    green = partial(_green, project, conn, run_id, beat_s, wt, verify_cmd,
                    contracts)
    reason = reason or _unsound(wt, base, sha, ticket, candidates, green)
    if reason:
        _land(wt, sha, untracked)
        record("reverted", reason, out=out)
        return sha
    head = _head(wt)
    record("kept", None, retired=_retired(wt, sha, head), final=head, out=out)
    return head


def _skip_reason(project, conn, run_id, budget_min, sha, candidates):
    if not candidates:
        return NO_CANDIDATE
    if routes(project).trimmer_failed:
        return "trimmer route down"
    if run_cap_reason(project, conn, run_id, budget_min, sha):
        return f"the run cap would refuse a {budget_min} min turn"
    return None


def _run_added(wt, base, sha):
    added = []
    for old, new in _changed_pairs(wt, base, sha):
        if new and _is_test(new):
            held = _test_ids(wt, base, old)
            added += [f"{new}::{name}" for name in sorted(_test_ids(wt, sha, new))
                      if name not in held]
    return added


def _protected(conn, run_id, wt, sha, criteria, added):
    files = criterion_files(conn, run_id, wt, sha, criteria)
    cited = _cited(conn, run_id)
    witnessed = _story_files(conn, run_id)
    held = _reproduced_sha(conn, run_id)
    renamed = ({new: old for old, new in _changed_pairs(wt, held, sha) if new}
               if held else {})
    reasons = {}
    for test in added:
        path, _, name = test.partition("::")
        why = ("in a criterion-named file" if path in files
               else "cited by the approving review" if test in cited
               else "in a story witness file" if path in witnessed
               else "held at the reproduction commit"
               if held and _test_ids(wt, held, renamed.get(path, path))[name]
               else None)
        if why:
            reasons[test] = why
    return reasons


def _cited(conn, run_id):
    if conn is None:
        return set()
    events = store.run_reads.criteria_events(conn, [run_id])
    entries = events[0].payload.get("criteria", []) if events else []
    cited = set()
    for entry in entries:
        if entry.get("status") != "met":
            continue
        for ref in test_references(entry.get("note") or ""):
            path, _, name = ref
            cited.add(f"{path}::{name}")
        for resolved in entry.get("resolved") or ():
            path, *_, name = resolved["as"].split("::")
            cited.add(f"{path}::{name}")
    return cited


def _story_files(conn, run_id):
    if conn is None:
        return set()
    ticket_id = store.read.run_snapshot(conn, run_id).ticketId
    found = store.stories.story(conn, ticket_id)
    return {witness.file for witness in found.witnesses} if found else set()


def _reproduced_sha(conn, run_id):
    if conn is None:
        return None
    row = conn.execute(
        "SELECT payload FROM runEvents WHERE runId = ? AND kind = 'reproduced'"
        " ORDER BY seq DESC LIMIT 1", (run_id,)).fetchone()
    return json.loads(row[0]).get("sha") if row else None


def _unsound(wt, base, sha, ticket, candidates, green):
    shape = _shape(wt, sha)
    if shape:
        return shape
    head = _head(wt)
    changed = _changed(wt, sha, head)
    removed = _removed(wt, sha, head, changed)
    reasons = [f"the commit changed {path}, not a test file"
               for path in changed if not _is_test(path)]
    reasons += [f"the commit added a line that is not an import in {path}"
                for path in changed if _adds_non_import(wt, sha, head, path)]
    outside = [test for test in removed if test not in candidates]
    if outside:
        reasons.append("the commit removed tests that are not candidates: "
                       + ", ".join(outside))
    unexplained = [test for test in removed
                   if test not in _reasons(wt, head, removed)]
    if unexplained:
        reasons.append("the commit body has no RETIRED line for: "
                       + ", ".join(unexplained))
    reasons += removed_outside_scope(wt, base, head, ticket)
    if not reasons and not green():
        reasons.append("verify was red at the retire commit")
    return "; ".join(reasons) or None


def _shape(wt, sha):
    if subprocess.run(["git", "merge-base", "--is-ancestor", sha, "HEAD"],
                      cwd=wt, capture_output=True).returncode:
        return f"the turn rewrote history below {sha[:12]}"
    head, *parents = sh(["git", "rev-list", "--parents", "-n", "1", "HEAD"],
                        cwd=wt).split()
    if parents != [sha]:
        return f"the turn made more than one commit on {sha[:12]}"
    subject = sh(["git", "log", "-1", "--format=%s", head], cwd=wt)
    if subject != SUBJECT:
        return f"subject {subject!r} is not {SUBJECT!r}"
    return None


def _adds_non_import(wt, sha, head, path):
    diff = subprocess.run(
        ["git", "--literal-pathspecs", "diff", "--no-renames", "--no-textconv",
         "--no-ext-diff", "--text", "-U0", sha, head, "--", path],
        cwd=wt, capture_output=True, check=True).stdout.decode(errors="replace")
    hunks = diff.partition("\n@@")[2]
    return any(not line[1:].strip().startswith(("import ", "from "))
               for line in hunks.splitlines() if line.startswith("+"))


def _head(wt):
    return sh(["git", "rev-parse", "HEAD"], cwd=wt)


def _changed(wt, sha, head):
    return [path for path in sh(["git", "diff", "--name-only", "--no-renames",
                                 "-z", sha, head], cwd=wt).split("\0") if path]


def _removed(wt, sha, head, changed):
    return [f"{path}::{name}" for path in changed
            for name in sorted(_test_ids(wt, sha, path)
                               - _test_ids(wt, head, path))]


def _reasons(wt, head, removed):
    longest_first = sorted(removed, key=len, reverse=True)
    reasons = {}
    for line in sh(["git", "log", "-1", "--format=%b", head],
                   cwd=wt).splitlines():
        rest = line.removeprefix("RETIRED ")
        test = next((test for test in longest_first
                     if rest.startswith(f"{test}: ")), None)
        if line.startswith("RETIRED ") and test and rest[len(test) + 2:].strip():
            reasons[test] = rest[len(test) + 2:].strip()
    return reasons


def _retired(wt, sha, head):
    removed = _removed(wt, sha, head, _changed(wt, sha, head))
    reasons = _reasons(wt, head, removed)
    return [{"test": test, "reason": reasons[test]} for test in removed]


def _record(project, conn, run_id, outcome, reason, *, candidates, protected,
            approved, retired=(), final=None, out=""):
    summary = "; ".join(filter(None, (
        f"retire {outcome}",
        retired and f"{len(retired)} retired",
        reason)))
    print(f"[holo2] {summary}")
    if conn is None or run_id is None:
        return
    store.record_event(conn, run_id, "retire", summary)
    reply = redact_prose(str(out or "").strip(), known_secrets(project.config()))
    store.record_event(conn, run_id, "retire_result", summary, level="detail",
                       payload=json.dumps({
                           "outcome": outcome, "reason": reason,
                           "candidates": candidates,
                           "protected": [{"test": test, "why": why}
                                         for test, why in protected.items()],
                           "retired": retired, "approved_sha": approved,
                           "final_sha": final or approved,
                           "reply": reply[-OUTPUT_TAIL:]}))
