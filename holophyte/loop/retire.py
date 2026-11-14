"""The retire step: after approval, one turn drops run-added tests the gate fails."""
import ast
import json
import re
import subprocess
from functools import partial
from pathlib import PurePosixPath

import store
import store.run_reads
import store.stories
from holophyte.agents.agent_routes import routes
from holophyte.agents.review_workspace import review_base
from holophyte.config.config_tables import trim_config
from holophyte.loop.gates import sh
from holophyte.loop.implement import OUTPUT_TAIL, _commands_brief, run_cap_reason
from holophyte.loop.steering import adopted_verify
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
from holophyte.review.reply_parsing import _GO_NAMES, _JS_TITLE, test_references

SUBJECT = "retire: run-added tests"
NO_CANDIDATE = "no run-added test outside the protected set"
NOT_REPRODUCED = "the run took the not-reproduced path"
HOOKS = {"setUp", "tearDown", "setUpClass", "tearDownClass", "asyncSetUp",
         "asyncTearDown", "setUpModule", "tearDownModule"}
SKIPPED = re.compile(r"'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\""
                     r"|`(?:[^`\\]|\\.)*`|//[^\n]*|/\*.*?\*/", re.S)
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
    verify_cmd = adopted_verify(conn, run_id, verify_cmd)
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
    cut = [owner for path in changed if _is_test(path)
           for owner in _cut_tests(wt, sha, head, path, {
               test.partition("::")[2] for test in removed
               if test in candidates and test.startswith(f"{path}::")})]
    if cut:
        reasons.append("the commit deleted lines that kept tests own or use: "
                       + ", ".join(cut))
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


def _diff(wt, sha, head, path):
    return subprocess.run(
        ["git", "--literal-pathspecs", "diff", "--no-renames", "--no-textconv",
         "--no-ext-diff", "--text", "-U0", sha, head, "--", path],
        cwd=wt, capture_output=True, check=True).stdout.decode(errors="replace")


def _adds_non_import(wt, sha, head, path):
    hunks = _diff(wt, sha, head, path).partition("\n@@")[2]
    return any(not _is_import(line[1:], PurePosixPath(path).suffix)
               for line in hunks.splitlines() if line.startswith("+"))


def _is_import(line, suffix):
    if suffix != ".py":
        return (line.strip().startswith(("import ", "from "))
                and ";" not in line.strip().rstrip(";"))
    try:
        body = ast.parse(line.strip()).body
    except (SyntaxError, ValueError):
        return False
    return bool(body) and all(isinstance(node, (ast.Import, ast.ImportFrom))
                              for node in body)


def _deleted_hunks(wt, sha, head, path):
    hunks = []
    for line in _diff(wt, sha, head, path).splitlines():
        start = re.match(r"@@ -(\d+)", line)
        if start:
            hunks.append([])
            number = int(start[1])
        elif hunks and line.startswith("-"):
            hunks[-1].append((number, line[1:]))
            number += 1
    return [[(number, text) for number, text in hunk
             if text.strip() and not text.strip().startswith("#")]
            for hunk in hunks]


def _cut_tests(wt, sha, head, path, gone):
    hunks = [hunk for hunk in _deleted_hunks(wt, sha, head, path) if hunk]
    if not hunks:
        return []
    deleted = {number for hunk in hunks for number, _ in hunk}
    tree, text = _tree(wt, sha, path)
    if tree is None:
        owned = _candidate_lines(text, PurePosixPath(path).suffix, gone)
        return [f"{path}:{min(number for number, _ in hunk if number not in owned)}"
                for hunk in hunks if any(number not in owned for number, _ in hunk)]
    kept = {number for number, line in enumerate(text.splitlines(), 1)
            if line.strip() and not line.strip().startswith("#")} - deleted
    used = _used_names(wt, head, path)
    owners = (_owner(tree, number, kept, gone, used)
              for number in sorted(deleted))
    return sorted({f"{path}{owner}" for owner in owners if owner})


def _tree(wt, sha, path):
    shown = subprocess.run(["git", "show", f"{sha}:{path}"], cwd=wt,
                           capture_output=True)
    text = shown.stdout.decode(errors="replace")
    if PurePosixPath(path).suffix != ".py" or shown.returncode:
        return None, text
    try:
        return ast.parse(text), text
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None, text


def _used_names(wt, head, path):
    tree, _ = _tree(wt, head, path)
    if tree is None:
        return None
    return ({node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            | {node.attr for node in ast.walk(tree)
               if isinstance(node, ast.Attribute)})


def _owner(tree, number, kept, gone, used):
    chain = _enclosing(tree, number)
    whole = next((node for node in chain if kept.isdisjoint(_span(node))), None)
    inside = chain[:chain.index(whole)] if whole is not None else chain
    if whole is not None and not any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for node in inside) and _only_gone_use(whole, gone, used):
        return None
    named = [node for node in chain if hasattr(node, "name")]
    tests = [node for node in named if _is_test_def(node)]
    return f"::{(tests or named)[-1].name}" if named else f":{number}"


def _enclosing(tree, number):
    chain, body = [], tree.body
    while True:
        node = next((node for node in body if number in _span(node)), None)
        if node is None:
            return chain
        chain.append(node)
        body = [child for field in ("body", "orelse", "finalbody", "handlers")
                for child in getattr(node, field, [])
                if isinstance(child, ast.AST)]


def _only_gone_use(node, gone, used):
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return True
    if used is None:
        return False
    if _is_test_def(node):
        return node.name in gone
    if isinstance(node, ast.ClassDef):
        return node.name not in used and all(
            child.name in gone for child in ast.walk(node)
            if _is_test_def(child))
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name not in HOOKS | used
    targets = (node.targets if isinstance(node, ast.Assign)
               else [node.target] if isinstance(node, ast.AnnAssign) else None)
    return targets is not None and all(
        isinstance(target, ast.Name) and target.id not in used
        for target in targets)


def _is_test_def(node):
    return (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test"))


def _span(node):
    start = min([node.lineno, *(dec.lineno for dec in
                                getattr(node, "decorator_list", []))])
    return range(start, node.end_lineno + 1)


def _candidate_lines(text, suffix, gone):
    starts = [(match.start(), name, opener)
              for match, name, opener in _declarations(text, suffix)]
    lines = set()
    for index, (start, name, opener) in enumerate(starts):
        if name not in gone:
            continue
        bound = starts[index + 1][0] if index + 1 < len(starts) else len(text)
        close = _closing(text, text.find(opener, start), bound)
        if close is not None:
            first = text.count("\n", 0, start) + 1
            lines.update(range(first, text.count("\n", 0, close) + 2))
    return lines


def _declarations(text, suffix):
    if suffix == ".go":
        return [(match, match[1] if match[1] is not None else match[2],
                 "{" if match[1] is not None else "(")
                for match in _GO_NAMES.finditer(text)
                if match[1] is not None or match[2] is not None]
    if suffix == ".py":
        return []
    return [(match, match[2] if match[2] is not None else match[3], "(")
            for match in _JS_TITLE.finditer(text)]


def _closing(text, i, bound):
    depth = 0
    while 0 <= i < bound:
        skip = SKIPPED.match(text, i)
        if skip:
            i = skip.end()
            continue
        depth += {"(": 1, "[": 1, "{": 1, ")": -1, "]": -1, "}": -1}.get(
            text[i], 0)
        if depth == 0:
            return i
        i += 1
    return None


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
