#!/usr/bin/env python3
"""holo2: a minimal software factory.

Loop:
  0. Open the v2 store (WAL-mode SQLite, a sibling file of the target repo).
  1. Claim the first ready ticket from Linear (project in linear_provider),
     mirror it into the store and take the project's run lease before any
     branch exists; the lease goes back when the run ends, merged or not.
  2. Spawn an implementer agent (goal-based) on a branch.
  3. Spawn a read-only reviewer agent on the committed result.
  4. If findings: implementer fixes, one narrow re-review, and a fix round for
     its findings too. Max 2 review rounds.
  5. If neither round approved: one terminal adjudication run — PASS or FAIL on
     the final state, no new findings, no further fixes.
  6. On approval or a terminal PASS: merge to main, check off the task, repeat.
"""
import re
import subprocess
import sys
from pathlib import Path
from time import monotonic, time

import review_runner
import store
import ticket_template

TARGET = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/srv/dev/holo2test")
MAX_ROUNDS = 2
DEFAULT_BUDGET_MIN = 20  # per-task wall-clock cap unless the line says "(N min)"
# The loop's durable state: one WAL-mode SQLite file per target repo, a sibling
# of the target the way its worktree directory is (see WORKTREES below).
# Outside the repo rather than inside it: the factory's own .gitignore says
# nothing about the target checkout, so a store written into TARGET would leave
# the database and its two WAL sidecars untracked in whatever repo the loop is
# working on -- dirt a task's `git add -A` could sweep into a commit.
STORE_PATH = TARGET.parent / f"{TARGET.name}.holophyte.db"

TASK_RE = re.compile(r"^[-*] \[ \] (.+)$", re.M)
BUDGET_RE = re.compile(r"\((\d+)\s*min\)\s*$")
VERIFY_RE = re.compile(r"\(verify:\s*(.+?)\)\s*$")


def parse_task(line):
    """Split 'do thing (verify: cmd) (15 min)' -> text, verify cmd, budget."""
    text = line.strip()
    budget, verify = DEFAULT_BUDGET_MIN, None
    m = BUDGET_RE.search(text)
    if m:
        budget = int(m.group(1))
        text = BUDGET_RE.sub("", text).strip()
    m = VERIFY_RE.search(text)
    if m:
        verify = m.group(1).strip()
        text = VERIFY_RE.sub("", text).strip()
    return text, verify, budget


# Markers the instrumented verify script prints around each `&&` clause, so a
# failure can be attributed to the clause that produced it.
CLAUSE_MARK = "__holo2_verify_clause__"
FAIL_MARK = "__holo2_verify_failed__"

# Zero-test summaries a runner prints while still exiting 0 — a gate that went
# green having verified nothing. Deliberately narrow: only the standard
# unittest and pytest phrasings, anchored to their own line, with no
# natural-language inference about other runners.
VACUOUS_RE = re.compile(r"^\s*(?:Ran 0 tests\b|collected 0 items\b)", re.M)


def split_and_clauses(cmd):
    """Split a verify command on its top-level `&&` operators.

    Returns the clause list, or None when the command uses shell constructs
    whose meaning per-clause instrumentation could change (`||`, `;`, `&`,
    newlines, heredocs, backticks) or whose quoting/nesting is unbalanced.
    Those commands are run verbatim instead."""
    if "<<" in cmd:
        return None
    clauses, buf = [], []
    quote, depth, escaped = None, 0, False
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "`":
            return None
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return None
        elif ch == "#" and (i == 0 or cmd[i - 1].isspace() or cmd[i - 1] in "&("):
            # A comment runs to end of line, so any `&&` inside it is text.
            # Only a trailing comment is safe to keep: a newline would resume
            # code, and inside `(...)` the comment would swallow the closer.
            if depth or "\n" in cmd[i:]:
                return None
            buf.append(cmd[i:])
            break
        elif depth == 0:
            prev = next((c for c in reversed(buf) if not c.isspace()), "")
            if cmd[i:i + 2] == "&&":
                clauses.append("".join(buf).strip())
                buf = []
                i += 2
                continue
            if ch == "&" and prev not in "><":  # background job
                return None
            if cmd[i:i + 2] == "||" or ch in ";\n":
                return None
        buf.append(ch)
        i += 1
    if quote or escaped or depth:
        return None
    clauses.append("".join(buf).strip())
    return None if any(not c for c in clauses) else clauses


def instrumented_script(clauses):
    """One shell script that runs the clauses in order, stopping at the first
    failure with the original exit status. The clauses stay in a single shell,
    so `cd` and exported variables still carry across them.

    The failure is reported from an EXIT trap reading a clause counter, so a
    clause that ends the shell itself (`exit 7`) is still attributed rather
    than escaping as a bare non-zero status."""
    parts = ["__holo2_clause=0",
             "trap '__holo2_rc=$?; [ \"$__holo2_rc\" -eq 0 ] || "
             "printf \"%s\\n\" \"{} $__holo2_clause $__holo2_rc\"' EXIT"
             .format(FAIL_MARK)]
    for idx, clause in enumerate(clauses, 1):
        parts.append("__holo2_clause={}".format(idx))
        parts.append("printf '%s\\n' '{} {}'".format(CLAUSE_MARK, idx))
        parts.append("{{ {}\n}} || exit $?".format(clause))
    return "\n".join(parts)


def parse_clause_output(output):
    """Split marked output into per-clause text. Returns
    (per_clause, failed, cleaned) where failed is (clause index, exit code).

    A clause whose output has no trailing newline glues the next marker onto
    its last line ("first__holo2_verify_clause__ 2"), so markers are split
    off mid-line: the prefix stays with the clause that printed it."""
    per_clause, failed, cleaned, current = {}, None, [], None

    def emit(text):
        cleaned.append(text)
        if current is not None:
            per_clause[current].append(text)

    for line in output.splitlines():
        # Peel off any output a marker got glued onto.
        cut = len(line)
        for mark in (CLAUSE_MARK, FAIL_MARK):
            pos = line.find(mark)
            if pos > 0:
                cut = min(cut, pos)
        if cut < len(line):
            emit(line[:cut])
            line = line[cut:]
        if line.startswith(CLAUSE_MARK + " "):
            current = int(line.split()[1])
            per_clause[current] = []
            continue
        if line.startswith(FAIL_MARK + " "):
            _, idx, rc = line.split()
            failed = (int(idx), int(rc))
            continue
        emit(line)
    return ({k: "\n".join(v) for k, v in per_clause.items()},
            failed, "\n".join(cleaned))


def failure_report(cmd, clauses, per_clause, failed, returncode, cleaned):
    """Name the command that failed and show its output — never a bare
    non-zero exit. Silence is reported as silence, not as an empty pass.

    For a chain, every clause that actually ran is listed with its own
    output, and the clauses the failure short-circuited are named as not
    executed, so the reader can tell "did not run" from "ran and said
    nothing"."""
    if not (failed and clauses and 1 <= failed[0] <= len(clauses)):
        body = cleaned.strip() or "(no output — the command failed silently)"
        return (f"[verify] FAILED: command exited {returncode}\n"
                f"[verify]   full command: {cmd}\n"
                f"[verify]   output:\n{body[-2000:]}")
    idx, rc = failed
    head = (f"[verify] FAILED: clause {idx} of {len(clauses)} exited {rc}\n"
            f"[verify]   full command: {cmd}\n"
            f"[verify]   failing clause: {clauses[idx - 1]}")
    lines = []
    for n in range(1, idx + 1):
        status = f"exit {rc}" if n == idx else "ok"
        lines.append(f"[verify]   --- clause {n} ({status}): {clauses[n - 1]}")
        lines.append(per_clause.get(n, "").strip() or (
            "(no output — the clause failed silently)" if n == idx
            else "(no output)"))
    if idx < len(clauses):
        lines.append("[verify]   not executed: clause " + ", ".join(
            str(n) for n in range(idx + 1, len(clauses) + 1)))
    return f"{head}\n" + "\n".join(lines)[-2000:]


def vacuous_green_report(cmd, cleaned):
    """A test command that exits 0 having collected no tests verified nothing,
    so it is RED. Returns the report naming `vacuous-green`, quoting the
    summary line that gave it away and the output around it, or None when the
    output shows tests actually ran."""
    m = VACUOUS_RE.search(cleaned)
    if not m:
        return None
    summary = cleaned[m.start():].splitlines()[0].strip()
    body = cleaned.strip() or "(no output)"
    return (f"[verify] FAILED: vacuous-green — exited 0 but ran no tests\n"
            f"[verify]   zero-test summary: {summary}\n"
            f"[verify]   full command: {cmd}\n"
            f"[verify]   output:\n{body[-2000:]}")


def contract_report(contracts, cwd):
    """Run the ticket's literal contract checks — each a (relative path,
    expected literal) pair parsed from its `## Contract checks` fence — against
    the worktree.

    Returns the report for the first declaration that does not hold, naming the
    path and the expected literal so a drifted value (KO-106's port) is
    actionable, or None when every declared literal is present. The comparison
    is a verbatim substring test: no globs, no regex, no shell.

    The checked file's contents are never echoed. A ticket may point a
    declaration at a configuration file holding credentials, and this report is
    forwarded to the reviewer; the path and the missing literal say what
    drifted without logging a secret.
    """
    root = Path(cwd)
    for path, literal in contracts or ():
        problem = ticket_template.contract_path_problem(path)
        if problem is None and not literal:
            problem = "declaration has an empty expected literal"
        target = root / path
        if problem is None and not target.is_file():
            problem = "declared file does not exist"
        if problem is None:
            if literal in target.read_text(errors="replace"):
                continue
            problem = "expected literal is absent from the file"
        return (f"[verify] FAILED: contract check — {problem}\n"
                f"[verify]   path: {path}\n"
                f"[verify]   expected literal: {literal}")
    return None


def run_verify(cmd, cwd=None, contracts=None):
    """Mechanical acceptance check. Returns (ok, output). Runs via shell on
    purpose: the command is author-supplied on the ticket, not agent output.

    A failure is always attributable: a top-level `&&` chain is marked clause
    by clause inside one shell, and the report points at the clause that
    exited non-zero, including when that clause failed without printing
    anything. An exit-0 run that reports zero collected tests is failed as
    `vacuous-green` rather than passed.

    Literal contract checks declared on the ticket run first: they are
    deterministic, need no subprocess, and a drifted literal is a RED result
    even when the command itself passes. A ticket declaring none is unaffected.
    """
    drifted = contract_report(contracts, cwd or TARGET)
    if drifted:
        return False, drifted
    passed = (f"[verify] contract checks passed: {len(contracts)}\n"
              if contracts else "")
    if not cmd:
        return True, passed + "(no verify command)"
    clauses = split_and_clauses(cmd)
    marked = bool(clauses) and len(clauses) > 1
    r = subprocess.run(instrumented_script(clauses) if marked else cmd,
                       shell=True, cwd=str(cwd or TARGET),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, timeout=300)
    per_clause, failed, cleaned = parse_clause_output(r.stdout)
    if r.returncode == 0:
        vacuous = vacuous_green_report(cmd, cleaned)
        return (False, vacuous) if vacuous else (True, passed + cleaned.strip()[-2000:])
    return False, failure_report(cmd, clauses if marked else None,
                                 per_clause, failed, r.returncode, cleaned)


def sh(args, cwd=None):
    """Run an argv list — no shell, so task text can't break quoting."""
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"`{args}` failed:\n{r.stdout}\n{r.stderr}")
    return r.stdout.strip()


# Role -> harness/model pins. Each gate uses a distinct, live-probed route:
# Claude Code / Opus High implements; the local container boundary runs Codex /
# GPT-5.6 Sol Medium against a detached, zero-remote, read-only candidate.
IMPL_MODEL = "opus"
IMPL_EFFORT = "high"
REVIEW_PROFILE = "codex-sol-medium"


def agent(role, goal, cwd, *, base_sha=None, candidate_sha=None):
    """Run one agent turn for a role. Returns combined output text.

    `adjudicate` is the terminal pass/fail round. It takes the same
    independent reviewer route as `review` — a fresh dispatch that knows only
    the diff and the ticket — but its verdict is not enforced at the boundary:
    a reply that names no clean verdict has to reach the loop as text so it
    can be recorded and read as FAIL.
    """
    if role == "implement":
        cmd = ["claude", "-p", goal, "--model", IMPL_MODEL,
               "--effort", IMPL_EFFORT]
    elif role in ("review", "adjudicate"):
        if not base_sha or not candidate_sha:
            raise ValueError(f"{role} requires exact base_sha and candidate_sha")
        return review_runner.run_review(
            repo=Path(cwd),
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            prompt=goal,
            profile=REVIEW_PROFILE,
            timeout=1800,
            verdicts=(review_runner.REVIEW_VERDICTS if role == "review" else None),
        )
    else:
        raise ValueError(role)
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=1800)
    return (r.stdout + "\n" + r.stderr).strip()


# Agent replies reach the ledger as raw terminal output — ANSI-coloured tool
# traces — and as reviewer prose that heads its own sections. FINDINGS.md is
# append-only evidence, so whatever lands there is permanent: sanitize at the
# append boundary rather than cleaning the file up afterwards.
# A CSI sequence is introduced either by ESC-[ or by the single C1 byte \x9b;
# matching only the first left `\x9b31m` to lose its introducer and print as
# literal `31m`.
ANSI_CSI_RE = re.compile(r"(?:\x1b\[|\x9b)[0-9;?]*[ -/]*[@-~]")
# C0 and C1 controls, minus \t and \n.
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# Both Markdown heading forms, which the outline treats identically: ATX
# (`## Blockers`, indentable by up to three spaces) and Setext (a paragraph
# line underlined by `===` or `---`). The Setext lookahead skips a line that
# is itself an ATX heading or a list/quote marker, leaving those to ATX_RE
# and to the thematic break they actually are.
SETEXT_RE = re.compile(r"^ {0,3}(?![-*+>#\s])(.+?)[ \t]*\n {0,3}(?:=+|-+)[ \t]*$", re.M)
ATX_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t#]*$", re.M)
MAX_ENTRY_CHARS = 4000
# A real verdict is one short line — `VERDICT: REQUEST_CHANGES` is 24 chars.
# The line truncation is obliged to keep is agent-written, though, and a
# malformed reply is persisted verbatim, so cap it: otherwise `VERDICT: ` plus
# 10k characters is one trailing "verdict" line that carries the whole entry
# past MAX_ENTRY_CHARS.
MAX_VERDICT_CHARS = 200
TRUNCATION_MARKER = "[… truncated]"


def _trailing_verdict(text):
    """The `VERDICT:` line `text` ends on, which the record must keep.

    `review_runner.terminal_verdict` reads a verdict only in final position,
    so that is the one line truncation may never drop: it is the outcome the
    whole entry is evidence for. Any line opening `VERDICT:` counts, malformed
    ones included — those are exactly what a FAIL gets recorded from — and the
    line is cut to `MAX_VERDICT_CHARS` so that keeping it stays an exemption
    for one short line rather than a way around the entry budget.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not lines[-1].startswith("VERDICT:"):
        return None
    verdict = lines[-1]
    if len(verdict) > MAX_VERDICT_CHARS:
        verdict = (verdict[:MAX_VERDICT_CHARS - len(TRUNCATION_MARKER)]
                   + TRUNCATION_MARKER)
    return verdict


def sanitize_findings(text):
    """Make one agent-authored block safe to append to FINDINGS.md.

    Strips ANSI escape sequences and other control bytes, demotes embedded
    Markdown headings to bold lines so the file's outline stays the factory's
    own `## <timestamp> — <ticket>` entries, and cuts an oversize block down
    with a visible marker. A trailing `VERDICT:` line survives all three: the
    escape and heading rules never match it, and truncation re-attaches it
    below the marker so an oversize entry still records its outcome.
    """
    text = ANSI_CSI_RE.sub("", text)
    text = CONTROL_RE.sub("", text)
    text = SETEXT_RE.sub(r"**\1**", text)
    text = ATX_RE.sub(r"**\2**", text)
    if len(text) > MAX_ENTRY_CHARS:
        verdict = _trailing_verdict(text)
        tail = f"\n\n{TRUNCATION_MARKER}" + (f"\n\n{verdict}" if verdict else "")
        # max(): a negative bound would slice from the *end* and keep
        # nearly all of an oversize entry.
        head = text[:max(0, MAX_ENTRY_CHARS - len(tail))]
        if text[len(head)] != "\n" and "\n" in head:
            head = head[:head.rindex("\n")]  # never cut a line in half
        text = head.rstrip() + tail
    return text


def ledger(task_id, entry):
    """Persist a findings record: Linear comment (primary) + FINDINGS.md."""
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        import linear_provider
        linear_provider.comment(task_id, f"**{ts}**\n\n{entry}")
    except Exception as e:
        print(f"[holo2] Linear comment failed ({e}); FINDINGS.md only")
    p = TARGET / "FINDINGS.md"
    with p.open("a") as f:
        f.write(f"\n## {ts} — {task_id}\n{sanitize_findings(entry)}\n")


# --- review rounds as structured findings ------------------------------------
# The reviewer writes prose; `reviewRounds.findings` wants
# `{path, line?, severity, message}` objects, because a round the store holds
# as a paragraph cannot be compared with the next one. Extraction is therefore
# best-effort over the output format that exists today: what carries a file
# reference becomes a structured finding, and a reply that carries none is
# still recorded verbatim as a single finding. Nothing the reviewer said is
# dropped — a round is evidence, and a lossy record of it would make the
# fingerprint agree about rounds that never matched.

# A path as a reviewer cites one: a token carrying a directory separator, or
# one whose extension is at least two characters, optionally followed by
# `:line`. Deliberately narrow — a bare word is prose, and a wrong path is
# worse than none, since the fingerprint keys on it. The two-character
# extension is what keeps `e.g.` and `i.e.` out of the findings; a one-letter
# extension still parses when the citation carries a directory (`src/a.c`).
# The lookbehind makes the match start at a token boundary, so a URL in the
# reviewer's prose is not read as the path `//linear.app`.
FINDING_PATH_RE = re.compile(
    r"(?<![\w:/.\-])"
    r"([\w.\-]*/[\w.\-/]*\.\w+|[\w.\-/]*[\w\-]\.[A-Za-z]\w+)(?::(\d+))?")
# An *explicit* severity marker, bracketed (`[P0]`, `(blocker)`) or opening the
# line (`- BLOCKER: ...`). Only a marker moves a finding off the default: tone
# is not severity, and a reviewer that sounds alarmed has not filed a p0.
SEVERITY_RE = re.compile(
    r"[\[(]\s*(p0|p1|p2|nit|blocker)\s*[\])]"
    r"|^[\s\-*\d.)]*(p0|p1|p2|nit|blocker)\b\s*[:\-]", re.I | re.M)
DEFAULT_SEVERITY = "p2"
# The path a finding gets when the reviewer named none. Not a path any
# repository holds, so it cannot collide with a real file's findings, and
# readable in a `path:line:severity` key.
UNPARSED_PATH = "(unparsed)"
# One finding is a complaint, not a transcript. Long enough for a blocker with
# its reasoning; short enough that a runaway reply cannot make one row the
# size of the review.
MAX_FINDING_CHARS = 2000
# A blank line, or the bullet/number that opens the next item: the boundaries
# a reviewer's findings list actually uses, so a finding keeps the lines that
# explain it instead of being cut to the one that names the file.
BLOCK_BREAK_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def finding_message(text):
    """One finding's message, safe to store: no terminal escapes, bounded."""
    text = CONTROL_RE.sub("", ANSI_CSI_RE.sub("", text)).strip()
    if len(text) > MAX_FINDING_CHARS:
        text = (text[:MAX_FINDING_CHARS - len(TRUNCATION_MARKER)]
                + TRUNCATION_MARKER)
    return text


def raw_finding(reply):
    """The whole reply as one finding, for a round nothing parsed out of.

    The fallback the extraction is allowed to have: an unparseable reviewer
    reply is still a round that said something, and the alternative to keeping
    it under a placeholder path is a stored round that claims the reviewer
    found nothing.
    """
    return {"path": UNPARSED_PATH, "severity": DEFAULT_SEVERITY,
            "message": finding_message(reply)}


def finding_blocks(text):
    """The reply split into candidate findings: bullet items and paragraphs."""
    blocks, current = [], []
    for line in text.splitlines():
        if not line.strip() or BLOCK_BREAK_RE.match(line):
            if current:
                blocks.append("\n".join(current))
            current = [line] if line.strip() else []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def parse_findings(reply):
    """Structured findings from one reviewer reply, best effort.

    Each block that cites a file becomes one finding: the cited path and line,
    an explicit severity marker if the block carries one, and the block itself
    as the message, so the reasoning stays with the complaint it belongs to. A
    reply no block parsed out of returns `raw_finding()` rather than nothing.
    """
    findings = []
    for block in finding_blocks(reply):
        match = FINDING_PATH_RE.search(block)
        if match is None:
            continue
        path, line = match.group(1), match.group(2)
        finding = {"path": path, "severity": finding_severity(block),
                   "message": finding_message(block)}
        if line and int(line) > 0:
            finding["line"] = int(line)
        findings.append(finding)
    return findings or [raw_finding(reply)]


def finding_severity(block):
    """`p2` unless the block carries an explicit severity marker."""
    match = SEVERITY_RE.search(block)
    if match is None:
        return DEFAULT_SEVERITY
    marker = (match.group(1) or match.group(2)).lower()
    return "p0" if marker == "blocker" else marker


def round_verdict(reply, verdicts):
    """The reply's verdict as `reviewRounds.verdict` spells it.

    Both reviewer vocabularies collapse onto §2's three: an approval or a
    terminal PASS is `pass`, findings or a terminal FAIL is
    `changes_requested`, and a reply with no clean verdict line is `error` —
    which is what the loop already reads a malformed adjudication as.
    """
    try:
        return {"APPROVE": "pass", "REQUEST_CHANGES": "changes_requested",
                "PASS": "pass", "FAIL": "changes_requested"}[
                    review_runner.terminal_verdict(reply, verdicts)]
    except review_runner.ReviewBoundaryError:
        return "error"


WORKTREES = TARGET.parent / f"{TARGET.name}.worktrees"


def run_task(task, conn=None, run_id=None):
    """task: dict from a provider — {id, title, verify, budget_min}.

    Each task works in its own git worktree (TARGET stays on main, untouched),
    so a dirty/failed task can never block the repo or the next ticket.

    `conn` and `run_id` are the store and the claimed run the loop took the
    lease with. Every stage boundary below records its phase against them
    through `set_phase()`, and every review or adjudication turn records its
    round through `record_round()`, so in-flight state outlives the process:
    the run row, its rounds and its event stream say what the loop was doing
    and what the reviewer found, instead of that living only in this frame and
    in prose. Both default to None for a direct call with no store, which runs
    the same stages and records nothing.
    """
    task_id = task["id"]
    # Claim-to-merge wall clock: run_task is entered immediately after the
    # claim, so this is the ticket's actual duration as far as the loop knows.
    started = monotonic()
    verify_cmd, budget_min = task.get("verify"), task["budget_min"]
    contracts = task.get("contracts")
    task = task["title"]
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower())[:30].strip("-")
    branch = f"task/{slug}"
    wt = WORKTREES / slug
    # §4's one edge out of `claimed`, taken before the first git command:
    # cutting the worktree is already this run doing the ticket's work, so a
    # crash in it belongs to `working` and not to a run that still looks
    # freshly claimed.
    set_phase(conn, run_id, "working", f"cutting {branch} and implementing")
    if wt.exists():
        # leftover from a previous failed run — reuse it as-is so preserved
        # work survives; the branch check below still gates on commits.
        sh(["git", "worktree", "prune"], TARGET)
        r = subprocess.run(["git", "worktree", "list", "--porcelain"],
                           cwd=TARGET, capture_output=True, text=True)
        registered = str(wt) in r.stdout
        if not registered:
            sh(["git", "worktree", "add", "--detach", str(wt), "main"], TARGET)
        sh(["git", "checkout", "-B", branch, "main"], cwd=wt)
    else:
        sh(["git", "worktree", "add", "--detach", str(wt), "main"], TARGET)
        sh(["git", "checkout", "-b", branch], cwd=wt)
    base_sha = sh(["git", "rev-parse", "HEAD"], cwd=wt)

    def timed(goal):
        """Run one agent turn with a hard wall-clock cap; None on timeout."""
        import signal

        def handler(signum, frame):
            raise TimeoutError()

        old = signal.signal(signal.SIGALRM, handler)
        signal.alarm(budget_min * 60)
        try:
            return agent("implement", goal, wt)
        except TimeoutError:
            print(f"[holo2] task exceeded {budget_min} min budget")
            return None
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)

    # 1. implement
    out = timed(f"Implement this task in this repo: {task}\n"
                "Acceptance criteria are part of the task line; the task is done "
                "only when they hold. Commit your work with a clear message. "
                "Stay strictly on-scope; do not expand the task.")
    if out is None or sh(["git", "rev-parse", "HEAD"], cwd=wt) == sh(["git", "rev-parse", "main"], TARGET):
        print(f"[holo2] implementer made no commits for: {task}")
        sh(["git", "worktree", "remove", "--force", str(wt)], TARGET)
        sh(["git", "branch", "-D", branch], TARGET)
        return False

    sha = sh(["git", "rev-parse", "HEAD"], cwd=wt)

    def verify_brief(ok, out):
        """The verify result as the reviewer sees it — omitted when the ticket
        declares no command, so the brief never implies a gate that never ran."""
        if not verify_cmd:
            return ""
        return (f"A mechanical verification command was run and "
                f"{'PASSED' if ok else 'FAILED with output below'}:\n{out}\n")

    # 2. review rounds (MAX_ROUNDS). Verify runs before each review and its
    # result goes into the brief; every round that is not a clean approval —
    # round 2 included — gets a fix round, because a round-2 blocker is the
    # cheapest fix in the loop and used to need a human to close it out.
    for rnd in range(1, MAX_ROUNDS + 1):
        set_phase(conn, run_id, "verifying", f"round {rnd}: verify before review")
        ok, out = run_verify(verify_cmd, wt, contracts)
        if ok:
            print(f"[holo2] verify ok before round {rnd}")
        else:
            print(f"[holo2] verify FAILED before round {rnd}:\n{out}")

        set_phase(conn, run_id, "reviewing", f"round {rnd} review")
        round_started = int(time() * 1000)
        verdict = agent("review",
            f"You are a READ-ONLY code reviewer. Review commit {sha} using "
            "refs/review/base as the frozen base and refs/review/candidate as "
            "the candidate "
            f"in this repo against the task: {task}\n"
            + verify_brief(ok, out)
            + "Do not modify anything. End your reply with exactly one line:\n"
            "VERDICT: APPROVE  or  VERDICT: REQUEST_CHANGES\n"
            "If REQUEST_CHANGES, list only concrete blockers.", wt,
            base_sha=base_sha, candidate_sha=sha)
        # Before the approval check, so the round that ends the loop is stored
        # like every other one: a review the store has no row for is a round
        # §6 cannot compare the next one against.
        record_round(conn, run_id, rnd, "review", verdict, verify_cmd, ok, out,
                     started_at=round_started)

        if ok and review_runner.terminal_verdict(verdict) == "APPROVE":
            break

        # 3. implementer addresses findings (same branch, new commit)
        set_phase(conn, run_id, "addressing", f"round {rnd}: addressing findings")
        fixes = timed(f"A reviewer left findings on your work for task: {task}\n\n"
                      f"{verdict}\n\n"
                      "For EACH finding, adjudicate it first: ADDRESS (concrete "
                      "blocker — fix now), FOLLOW_UP (valid but out of scope — name "
                      "it in the commit message), or DECLINE (invalid/out-of-scope — "
                      "state the rationale in the commit message). Then fix only the "
                      "ADDRESS items and commit.")
        ledger(task_id, f"Round {rnd}: REQUEST_CHANGES -> fix round\n"
                     f"Reviewer findings:\n{verdict}\n\n"
                     f"Implementer response:\n{fixes}")
        if fixes is None or sh(["git", "rev-parse", "HEAD"], cwd=wt) == sha:
            print(f"[holo2] fix round timed out or made no progress; "
                  f"leaving branch {branch} at {sha} for a human.")
            return False
        sha = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    else:
        # 3b. Terminal adjudication: both review rounds and their fixes are
        # spent, so one fresh independent run issues a bare verdict on the
        # final state. There is no further fix round under any outcome —
        # anything but PASS preserves the branch and stops the loop.
        set_phase(conn, run_id, "verifying", "verify before terminal adjudication")
        ok, out = run_verify(verify_cmd, wt, contracts)
        if not ok:
            print(f"[holo2] verify FAILED before adjudication; leaving branch "
                  f"{branch} (worktree {wt}) at {sha} for a human:\n{out}")
            ledger(task_id, f"FAILED verify before terminal adjudication after "
                         f"{MAX_ROUNDS} review rounds; branch {branch} preserved "
                         f"at {sha}\n\n{out}")
            return False
        print("[holo2] verify ok before adjudication")

        set_phase(conn, run_id, "reviewing", "terminal adjudication")
        round_started = int(time() * 1000)
        reply = agent("adjudicate",
            f"You are a READ-ONLY final adjudicator. Judge commit {sha} using "
            "refs/review/base as the frozen base and refs/review/candidate as "
            "the candidate "
            f"in this repo against the task: {task}\n"
            + verify_brief(ok, out)
            + "This candidate has already had its review rounds and their "
            "fixes; no further fix round exists. Your job is a verdict on the "
            "state as it stands, not a review.\n"
            "Do not modify anything. Do NOT list findings, request changes, or "
            "propose follow-up work — a reply that reads as a findings list is "
            "not a verdict and is treated as FAIL. Give at most one short "
            "paragraph of justification, then exactly one final line:\n"
            "VERDICT: PASS  or  VERDICT: FAIL\n"
            "PASS means the candidate is mergeable as it stands.", wt,
            base_sha=base_sha, candidate_sha=sha)
        # The adjudication is a round of the run like the reviews before it —
        # numbered after them, so the run's rounds read in the order they
        # happened.
        record_round(conn, run_id, MAX_ROUNDS + 1, "adjudicate", reply,
                     verify_cmd, ok, out, started_at=round_started)
        try:
            decision = review_runner.terminal_verdict(
                reply, review_runner.ADJUDICATION_VERDICTS)
        except review_runner.ReviewBoundaryError:
            decision = "MALFORMED"  # no clean verdict — read as FAIL
        if decision != "PASS":
            print(f"[holo2] terminal adjudication: {decision}; leaving branch "
                  f"{branch} (worktree {wt}) at {sha} for a human. Task: {task}")
            ledger(task_id, f"Terminal adjudication after {MAX_ROUNDS} review "
                         f"rounds: {decision}; branch {branch} preserved at "
                         f"{sha}\n\nAdjudicator reply:\n{reply}")
            return False
        print("[holo2] terminal adjudication: PASS")
        ledger(task_id, f"Terminal adjudication after {MAX_ROUNDS} review "
                     f"rounds: PASS\n\nAdjudicator reply:\n{reply}")

    # 4. pre-merge verify (catches fix-round regressions), then merge. Both
    # happen under `merge_gate`: §4's gate node is the one edge out of a
    # passing review, and this verify is the mechanical half of what the gate
    # asks. Under the `personal` autonomy profile the human half is a no-op,
    # so the run passes through the node rather than around it and a failed
    # pre-merge verify is a run stopped at the gate.
    set_phase(conn, run_id, "merge_gate", "pre-merge verify, then the autonomy gate")
    ok, out = run_verify(verify_cmd, wt, contracts)
    if not ok:
        print(f"[holo2] verify FAILED before merge; leaving branch {branch} "
              f"at {sha} for a human:\n{out}")
        return False
    print("[holo2] verify ok before merge")

    # Commit any pending FINDINGS.md changes BEFORE merging so the merge
    # never trips over a dirty index.
    r = subprocess.run(["git", "status", "--porcelain", "FINDINGS.md"],
                       cwd=TARGET, capture_output=True, text=True)
    if r.stdout.strip():
        sh(["git", "add", "FINDINGS.md"], TARGET)
        sh(["git", "commit", "-m", f"FINDINGS: {task_id} review records"], TARGET)

    # `squashing` is skipped, not faked: this merge is --no-ff and rewrites
    # no history, so the run goes merging -> done and the phase §4 puts
    # between them names an activity that never happens here.
    set_phase(conn, run_id, "merging", f"--no-ff merge of {branch} into main")
    mr = subprocess.run(["git", "merge", "--no-ff", branch, "-m",
                         f"Merge {branch}: {task}"], cwd=TARGET,
                        capture_output=True, text=True)
    if mr.returncode != 0 and "FINDINGS.md" in (mr.stdout + mr.stderr):
        # conflict limited to FINDINGS.md — prefer the branch side (fuller log)
        subprocess.run(["git", "checkout", "--theirs", "FINDINGS.md"],
                       cwd=TARGET, capture_output=True, text=True)
        sh(["git", "add", "FINDINGS.md"], TARGET)
        sh(["git", "commit", "--no-edit"], TARGET)
    else:
        assert mr.returncode == 0, (mr.stdout, mr.stderr)
    sh(["git", "worktree", "remove", str(wt)], TARGET)
    sh(["git", "branch", "-d", branch], TARGET)
    import linear_provider
    linear_provider.complete(task_id)
    # One greppable line of timing data per merged ticket: the estimate stays
    # write-only otherwise, and a future burndown script reads this format.
    actual_min = (monotonic() - started) / 60
    ledger(task_id, f"MERGED to main (branch {branch} deleted). "
                 f"Verify: {'passed' if ok else 'n/a'}.\n"
                 f"actual: {actual_min:.1f} min · estimate: {budget_min} min · "
                 f"rounds: {rnd}")
    sh(["git", "add", "FINDINGS.md"], TARGET)
    sh(["git", "commit", "-m", f"Complete task {task_id}: {task}"], TARGET)
    print(f"[holo2] merged: {task}")
    return True


# --- store seam --------------------------------------------------------------
# Every store call the loop makes goes through one of the helpers below, so a
# later wiring ticket extends this seam instead of threading SQL through
# run_task().


def open_store(path=None):
    """Open the loop's store, creating and migrating the schema if needed."""
    conn = store.open(str(path or STORE_PATH))
    store.init(conn)
    return conn


def set_phase(conn, run_id, phase, note=None):
    """Record a stage boundary: the run's phase, its heartbeat, one event.

    The loop's single writer of `runs.phase` (state-model §6), which is why
    every stage below calls it instead of writing its own row: phase,
    heartbeat and narrative event move together in the store or not at all, so
    a crashed loop leaves a run parked where it stopped rather than parked in
    whatever phase it was last seen entering.

    A `conn` of None makes this a no-op, for `run_task()` driven directly
    without a store. That keeps one set of call sites rather than a storeless
    copy of the loop, and the phases are then simply not recorded.
    """
    if conn is None:
        return
    store.set_phase(conn, run_id, phase, note)


def record_round(conn, run_id, rnd, role, reply, verify_cmd, ok, out,
                 started_at=None):
    """Record one review or adjudication round as a `reviewRounds` row.

    The round the loop just ran, as the store holds it: the verdict, the
    reviewer route that issued it, the verify result the reviewer was briefed
    with, and the findings — structured where the reply let them be extracted.
    `store.record_review_round()` fingerprints them on the way in, which is
    what makes two rounds comparable and is the whole reason the prose in
    FINDINGS.md is not enough.

    Which findings a round carries follows the round's kind. A `review` round
    that asked for changes carries what parsed out of its reply; an approval
    carries none, because approving prose is not a findings list. An
    adjudication is a verdict and nothing else by its own prompt, so PASS and
    FAIL both store an empty list — a reply that named no verdict at all is
    the exception, and its raw text is kept as the one finding rather than
    recorded as a round that said nothing.

    A `conn` of None makes this a no-op, like `set_phase()`, so a storeless
    `run_task()` runs the same stages and records nothing.
    """
    if conn is None:
        return
    verdicts = (review_runner.REVIEW_VERDICTS if role == "review"
                else review_runner.ADJUDICATION_VERDICTS)
    verdict = round_verdict(reply, verdicts)
    if verdict == "error":
        findings = [raw_finding(reply)]
    elif verdict == "changes_requested" and role == "review":
        findings = parse_findings(reply)
    else:
        findings = []
    # `run_verify()` reports a pass/fail gate rather than a raw status — the
    # failing clause and its exit code live in the output it builds — so the
    # exit code stored here is that verdict, and `output` is the detail.
    results = ([{"command": verify_cmd, "exitCode": 0 if ok else 1,
                 "output": out}] if verify_cmd else [])
    store.record_review_round(conn, run_id, rnd, verdict, REVIEW_PROFILE,
                              findings=findings, verification_results=results,
                              started_at=started_at,
                              ended_at=int(time() * 1000))


def claim_run(conn, project, task):
    """Mirror the claimed ticket and take the project's run lease; return its id.

    Raises `store.ClaimConflict` when the project already has an active run —
    the point of routing the claim through the store, since that is what stops
    two loops from working one project at once.

    The provider's task dict carries no acceptance-criteria list, so the mirror
    lands in `needs_spec` by the store's own routing rule: it has the ticket's
    verify command but not its criteria, and an under-specced mirror must not
    look pickable. Nothing reads that yet — the loop still picks in Linear —
    and it becomes `ready` as soon as the mirror carries both lists.

    The two ids the mirror stores are different ids: `linearIssueId` is the
    canonical issue UUID the provider hands over as `issue_id`, and is what
    the mirror is keyed and re-found by, while `linearIdentifier` is the human
    "KO-123" label. Storing the label in both would key the mirror on the
    mutable one, so a later UUID-carrying writer — webhook wiring, say — would
    mirror the same issue a second time under its real id. A provider with no
    UUID to give still gets a mirror, keyed on the identifier it does have.
    """
    ticket_id = store.mirror_ticket(
        conn,
        project,
        linear_issue_id=task.get("issue_id") or task["id"],
        linear_identifier=task["id"],
        title=task["title"],
        verification_commands=[task["verify"]] if task.get("verify") else (),
        time_box_ms=task["budget_min"] * 60 * 1000,
    )
    return store.claim(conn, project, ticket_id)


def release_run(conn, run_id, merged):
    """Give the lease back when the loop is done with a run, merged or not.

    Called from the loop's `finally`, because the failure paths are the ones
    that matter: a run that dies holding the lease blocks every later claim on
    the project, and a preserved branch is meant to wait for a human without
    also freezing the queue.

    A failure reason names the phase the run stopped in, read back from the
    store rather than remembered here: on a crash the loop's own idea of where
    it was died with the exception, while the phase `set_phase()` last wrote
    is exactly where the run got to.
    """
    if merged:
        store.release(conn, run_id, "merged")
        return
    store.release(conn, run_id, "failed",
                  f"run stopped in phase {store.run_phase(conn, run_id)};"
                  " branch preserved for a human")


def main(provider=None):
    if provider is None:
        import linear_provider as provider
    conn = open_store()
    try:
        # The provider knows its team by name rather than by id; the column's
        # contract is one row per Linear team, which the name keys just as
        # well until the provider resolves the id.
        project = store.ensure_project(conn, provider.TEAM, TARGET)
        while True:
            task = provider.claim_next()
            if not task:
                print("[holo2] Linear has no ready tickets. done.")
                return
            try:
                run_id = claim_run(conn, project, task)
            except store.ClaimConflict as e:
                # Before any branch or worktree exists: another loop holds the
                # project, so this one stops rather than working beside it.
                print(f"[holo2] claim refused, not starting a run: {e}")
                return
            merged = False
            try:
                merged = run_task(task, conn, run_id)
            finally:
                release_run(conn, run_id, merged)
            if not merged:
                return  # stop on first failure; ticket stays In Progress for a human
    finally:
        conn.close()


if __name__ == "__main__":
    main()
