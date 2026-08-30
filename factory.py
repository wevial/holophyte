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

`--report` runs none of the above: it prints the store's estimate-vs-actual
table for the target and exits, so the timing the runs recorded is a query
rather than a grep over FINDINGS.md.
"""
import argparse
import hashlib
import json
import re
import statistics
import subprocess
import sys
from pathlib import Path
from time import monotonic, time

import review_runner
import store
import ticket_template

MAX_ROUNDS = 2
DEFAULT_BUDGET_MIN = 20  # per-task wall-clock cap unless the line says "(N min)"
DEFAULT_TARGET = Path("/srv/dev/holo2test")
# The three paths a run works against. They are set by `retarget()` below
# rather than written out here, so the derivation lives in one place and the
# command line is the only thing that chooses a target: importing this module
# used to read `sys.argv[1]`, which made every `python3 -m unittest discover`
# retarget the factory at a directory called "discover".
TARGET = STORE_PATH = WORKTREES = None


def retarget(target):
    """Point TARGET and the two paths derived from it at `target`.

    Called once at import for the default and again by `cli()` for whatever
    the command line names; nothing else moves these, so a caller that wants a
    different target says so here instead of patching one path and leaving the
    other two pointing at the last one.
    """
    global TARGET, STORE_PATH, WORKTREES
    TARGET = Path(target)
    # The loop's durable state: one WAL-mode SQLite file per target repo, a
    # sibling of the target the way its worktree directory is. Outside the
    # repo rather than inside it: the factory's own .gitignore says nothing
    # about the target checkout, so a store written into TARGET would leave
    # the database and its two WAL sidecars untracked in whatever repo the
    # loop is working on -- dirt a task's `git add -A` could sweep into a
    # commit.
    STORE_PATH = TARGET.parent / f"{TARGET.name}.holophyte.db"
    WORKTREES = TARGET.parent / f"{TARGET.name}.worktrees"


retarget(DEFAULT_TARGET)

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
# A real verdict is one short line — `VERDICT: REQUEST_CHANGES` is 24 chars.
# The line truncation is obliged to keep is agent-written, though, and a
# malformed reply is persisted verbatim, so cap it: otherwise `VERDICT: ` plus
# 10k characters is one trailing "verdict" line that carries the whole record
# past its budget.
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


def sanitize_findings(text, limit):
    """Make one agent-authored block safe to keep as a findings record.

    Strips ANSI escape sequences and other control bytes, demotes embedded
    Markdown headings to bold lines so the file's outline stays the factory's
    own `## <timestamp> — <ticket>` entries, and cuts an oversize block down
    with a visible marker. A trailing `VERDICT:` line survives all three: the
    escape and heading rules never match it, and truncation re-attaches it
    below the marker so an oversize entry still records its outcome.

    Applied at the row write (`finding_message`) rather than at a file
    append: FINDINGS.md is rendered from the rows now, so text a row carries
    dirty is text every later render carries dirty. `limit` is the calling
    boundary's own budget, and has no default — there is one boundary, and a
    sanitizer that guesses a bound is one that silently keeps the wrong one.
    """
    text = ANSI_CSI_RE.sub("", text)
    text = CONTROL_RE.sub("", text)
    text = SETEXT_RE.sub(r"**\1**", text)
    text = ATX_RE.sub(r"**\2**", text)
    if len(text) > limit:
        verdict = _trailing_verdict(text)
        tail = f"\n\n{TRUNCATION_MARKER}" + (f"\n\n{verdict}" if verdict else "")
        # max(): a negative bound would slice from the *end* and keep
        # nearly all of an oversize entry.
        head = text[:max(0, limit - len(tail))]
        if text[len(head)] != "\n" and "\n" in head:
            head = head[:head.rindex("\n")]  # never cut a line in half
        text = head.rstrip() + tail
    return text


def ledger(task_id, entry):
    """Archive one record as a Linear comment on the ticket.

    Nothing is appended to FINDINGS.md here any more: that file is rendered
    from the store's rows at close-out (`write_findings`), so it stays a
    bounded window instead of growing by one full transcript per turn. Linear
    comments are unchanged and stay the per-ticket archive of the whole prose
    — the store keeps the structure, Linear keeps the words.
    """
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        import linear_provider
        linear_provider.comment(task_id, f"**{ts}**\n\n{entry}")
    except Exception as e:
        print(f"[holo2] Linear comment failed ({e}); record kept in the store")


# --- review rounds as structured findings ------------------------------------
# The reviewer writes prose; `reviewRounds.findings` wants
# `{path, line?, severity, message}` objects, because a round the store holds
# as a paragraph cannot be compared with the next one. Extraction is therefore
# best-effort over the output format that exists today: what carries a file
# reference becomes a structured finding, an item the reviewer listed without
# one is kept under a placeholder path, and a reply that filed nothing at all
# is still recorded verbatim as a single finding. No complaint the reviewer
# filed is dropped — a round is evidence, and a lossy record of it would make
# the fingerprint agree about rounds that never matched.

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
# The path prefix a finding gets when the reviewer named none. Not a path any
# repository holds, so it cannot collide with a real file's findings, and
# readable in a `path:line:severity` key. A prefix rather than the whole path
# because that key is all §6 compares rounds by: findings sharing one
# placeholder would collapse into a single key, so a round complaining about
# `Dockerfile` and about `Makefile` would fingerprint as one complaint, and an
# unrelated round that filed one pathless p2 would fingerprint identically to
# it -- the false "same round twice" this task exists to make detectable.
# `unparsed_path()` appends a digest of the finding's own text to keep them
# apart.
UNPARSED_PATH = "(unparsed)"
# One finding is a complaint, not a transcript. Long enough for a blocker with
# its reasoning; short enough that a runaway reply cannot make one row the
# size of the review.
MAX_FINDING_CHARS = 2000
# A blank line, or the bullet/number that opens the next item: the boundaries
# a reviewer's findings list actually uses, so a finding keeps the lines that
# explain it instead of being cut to the one that names the file.
BLOCK_BREAK_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def unparsed_path(message):
    """`UNPARSED_PATH` made distinct per complaint, by digesting its message.

    A finding with no path has only its text to say which complaint it is, so
    that is what the placeholder carries. This is not message prose leaking
    into the fingerprint generally -- a finding that cites a file still keys on
    the file -- it is the pathless case having nothing else to key on.

    Whitespace-normalized and lowercased before hashing, so a reviewer that
    rewraps or recapitalizes an unchanged complaint still keys to the same
    place. Prose it genuinely rewrote keys somewhere new, which is the
    direction to fail in: §6 reads a fingerprint match as a stuck review, so
    two distinct complaints reading as one is a false stop, while one complaint
    reworded reading as two only costs the softer overlap signal.
    """
    normalized = " ".join(message.split()).lower()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"{UNPARSED_PATH}:{digest}"


def finding_message(text):
    """One finding's message, safe to store: sanitized and bounded.

    The row write is where `sanitize_findings()` now applies, so a message
    carrying a terminal escape or a Markdown heading is cleaned once, here,
    rather than on every render of the window it will appear in.
    """
    return sanitize_findings(text, MAX_FINDING_CHARS).strip()


def raw_finding(reply):
    """The whole reply as one finding, for a round nothing parsed out of.

    The fallback the extraction is allowed to have: an unparseable reviewer
    reply is still a round that said something, and the alternative to keeping
    it under a placeholder path is a stored round that claims the reviewer
    found nothing.
    """
    message = finding_message(reply)
    return {"path": unparsed_path(message), "severity": DEFAULT_SEVERITY,
            "message": message}


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
    as the message, so the reasoning stays with the complaint it belongs to.

    A block the path pattern found nothing in is still a finding when the
    reviewer wrote it as a list item, filed under an `unparsed_path()`
    placeholder with whatever severity it marked. The reviewer's own bullet is what says it filed a
    complaint, and this parser recognizing no path in it is a fact about the
    parser: `Dockerfile`, `Makefile` and a bare directory carry nothing to
    match on. Keeping only the items whose paths happen to parse would leave
    the round fingerprinted as a shorter complaint than the one that was made,
    and §6 compares those fingerprints. Prose around the list -- an opening
    sentence, the closing `VERDICT:` line -- is narration rather than a filed
    item and is not stored as one; a reply that filed no item at all still
    returns `raw_finding()` over the whole text, so a round is never recorded
    as having said nothing.
    """
    findings = []
    for block in finding_blocks(reply):
        match = FINDING_PATH_RE.search(block)
        listed = BLOCK_BREAK_RE.match(block) is not None
        if match is None and not listed:
            continue
        message = finding_message(block)
        if not message:
            continue  # a bullet with nothing after it is not a complaint
        path, line = ((match.group(1), match.group(2)) if match
                      else (unparsed_path(message), None))
        finding = {"path": path, "severity": finding_severity(block),
                   "message": message}
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
    # never trips over a dirty index. Nothing is written to the file during a
    # run any more, so this is normally a no-op; what it still catches is a
    # window an earlier failed run regenerated and left uncommitted.
    commit_findings(f"FINDINGS: {task_id} review records")

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
    # Nothing tells Linear the ticket is done here any more. The merge makes
    # the ticket `merged` in the store, and `main()` projects that status onto
    # the board through `mirror_push()` once the run has been released — one
    # writer of the workflow state instead of a call from the middle of a run
    # that has not finished ending yet.
    # One greppable line of timing data per merged ticket: the estimate stays
    # write-only otherwise, and a future burndown script reads this format.
    actual_min = (monotonic() - started) / 60
    ledger(task_id, f"MERGED to main (branch {branch} deleted). "
                 f"Verify: {'passed' if ok else 'n/a'}.\n"
                 f"actual: {actual_min:.1f} min · estimate: {budget_min} min · "
                 f"rounds: {rnd}")
    # The task's own commit of FINDINGS.md is `main()`'s, not this frame's:
    # the run's close-out entry exists only once the run has been released,
    # which happens after this returns.
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


# --- FINDINGS.md as a window over the rows ------------------------------------
# The ledger used to be the source of truth, appended to once per agent turn,
# and it reached 53 KB in a single day: unreadable for a human and a context
# hazard for an agent working in the checkout. The rows are the history now --
# complete, queryable, cheap -- and the file is a rendering of their recent
# tail, so a reader gets the last few runs and everything older is one query
# away rather than deleted.
#
# The rendering is a function of the rows and of nothing else: no clock, no
# environment, ties in the ordering broken by row id, and the sanitizing
# already done at the row write. That is what makes regenerating the whole file
# at every close-out reviewable -- identical rows produce identical bytes, so
# the diff of a regeneration is exactly the entries the run added.
FINDINGS_WINDOW = 25  # entries kept in the file; per-project config is future work
# What separates the frozen pre-store history from the rendered window. Every
# regeneration reproduces the bytes above this line and replaces everything
# below it, so a ledger written before the store existed is preserved by not
# being rendered at all.
FINDINGS_MARKER = "<!-- store-rendered below -->"
FINDINGS_ARCHIVE = "[{n} earlier entries in holophyte.db — query runs/reviewRounds]"
# One finding is one line of the window: enough to recognize the complaint by,
# with the row holding all of it.
FINDING_LINE_CHARS = 160


def _stamp(ms):
    """Epoch milliseconds as the ledger's UTC timestamp."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _entry(at, ticket, lines):
    """One entry: the heading the file has always used, then its body."""
    return "\n".join([f"## {_stamp(at)} — {ticket}", *lines])


def finding_line(finding):
    """One stored finding as one line: where, how bad, and the gist.

    The message is collapsed to a single line and cut, because the window is a
    place to notice a complaint rather than to read it: the whole message is in
    `reviewRounds.findings`, and the alternative -- full prose per finding --
    is the unbounded file this rendering replaces.
    """
    where = finding["path"]
    if finding.get("line"):
        where = f"{where}:{finding['line']}"
    # The reviewer's own bullet marker opens most stored messages; the line
    # this renders already is a bullet, so it is dropped rather than nested.
    gist = BLOCK_BREAK_RE.sub("", " ".join(str(finding.get("message", "")).split()),
                              count=1)
    if len(gist) > FINDING_LINE_CHARS:
        gist = gist[:FINDING_LINE_CHARS].rstrip() + "…"
    return f"- {where} [{finding['severity']}] {gist}".rstrip()


def round_entry(row):
    """One `reviewRounds` row as an entry: the verdict and what it filed."""
    at, ticket, number, verdict, model, results, findings = row
    results = json.loads(results)
    verify = ""
    if results:
        verify = (" · verify "
                  + ("passed" if all(r.get("exitCode") == 0 for r in results)
                     else "failed"))
    findings = json.loads(findings)
    lines = [f"Round {number}: {verdict} · reviewer {model}{verify}"]
    if findings:
        lines.append(f"Findings ({len(findings)}):")
        lines.extend(finding_line(finding) for finding in findings)
    return _entry(at, ticket, lines)


def run_entry(row):
    """One ended `runs` row as an entry: its outcome and the timing line.

    Every number on the timing line is read off the run row itself -- the two
    stamps, the estimate it was claimed under, and the round count stamped at
    close-out (terminal adjudication included) -- rather than off the loop's
    in-frame counters or the ticket's current estimate. That is what makes the
    line and the row say the same thing: `--report` queries the same columns,
    and a rendering that needs the loop's variables is not a rendering of the
    rows.
    """
    at, ticket, outcome, reason, branch, started, time_box, rounds = row
    if outcome == "merged":
        head = ("MERGED to main"
                + (f" (branch {branch} deleted)" if branch else "") + ".")
    else:
        head = (outcome or "ended").upper()
        head += f": {' '.join(reason.split())}" if reason else "."
    # Byte-stable: a burndown script greps this line.
    estimate = f"{time_box // 60000} min" if time_box else "n/a"
    return _entry(at, ticket, [
        head,
        f"actual: {(at - started) / 60000:.1f} min · "
        f"estimate: {estimate} · rounds: {rounds}",
    ])


def findings_entries(conn):
    """Every entry the store holds, oldest first.

    Two row kinds are entries: a review round, and a run that ended. A run
    still in flight has no entry -- it has no outcome yet, and an entry that
    changed on the next render would make the file churn.
    """
    rounds = conn.execute(
        "SELECT COALESCE(rr.endedAt, rr.startedAt), t.linearIdentifier,"
        " rr.round, rr.verdict, rr.reviewerModel, rr.verificationResults,"
        " rr.findings, rr.id"
        " FROM reviewRounds rr JOIN runs r ON r.id = rr.runId"
        " JOIN tickets t ON t.id = r.ticketId").fetchall()
    runs = conn.execute(
        "SELECT r.endedAt, t.linearIdentifier, r.outcome, r.outcomeReason,"
        " r.branch, r.startedAt, r.timeBoxMs, r.reviewRoundCount, r.id"
        " FROM runs r JOIN tickets t ON t.id = r.ticketId"
        " WHERE r.endedAt IS NOT NULL").fetchall()
    entries = [(row[0], "round", row[-1], round_entry(row[:-1])) for row in rounds]
    entries += [(row[0], "run", row[-1], run_entry(row[:-1])) for row in runs]
    # Two rows stamped the same millisecond still have one order: kind, then
    # the id the database gave them.
    entries.sort(key=lambda entry: entry[:3])
    return [entry[3] for entry in entries]


def render_findings(conn, preamble=""):
    """The whole of FINDINGS.md: `preamble`, the marker, then the window.

    Only the newest `FINDINGS_WINDOW` entries are rendered; the rest collapse
    into the one archive line that says how many there are and where to read
    them. Newest last, so the file reads forwards like the append-only ledger
    it replaces.
    """
    entries = findings_entries(conn)
    hidden = max(0, len(entries) - FINDINGS_WINDOW)
    blocks = [FINDINGS_ARCHIVE.format(n=hidden)] if hidden else []
    blocks += entries[hidden:]
    # The preamble is reproduced byte for byte and only padded away from the
    # marker, which keeps this idempotent: the padded text is what the next
    # render reads back as the preamble.
    if preamble and not preamble.endswith("\n\n"):
        preamble += "\n" if preamble.endswith("\n") else "\n\n"
    body = "\n\n".join(blocks)
    return preamble + FINDINGS_MARKER + "\n" + (f"\n{body}\n" if body else "")


def frozen_preamble(text):
    """The half of `text` a regeneration must reproduce: above the marker.

    A file with no marker is entirely pre-store history, so all of it is
    preamble and the window is written below it. The marker counts only as a
    line of its own, which is what keeps a reviewer who quoted it from moving
    the boundary: no line this module renders can be a bare marker -- every
    one of them is a heading, a `Round`/outcome line, a `- ` finding or the
    archive line -- so the first standalone marker line is always the real one.
    """
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip() == FINDINGS_MARKER:
            return "".join(lines[:i])
    return text


def write_findings(conn, path=None):
    """Regenerate FINDINGS.md in place, keeping everything above the marker."""
    path = Path(path) if path else TARGET / "FINDINGS.md"
    existing = path.read_text() if path.exists() else ""
    path.write_text(render_findings(conn, frozen_preamble(existing)))
    return path


def commit_findings(message):
    """Commit FINDINGS.md in the target checkout, if the render changed it.

    Returns whether it committed. The guard is not an optimization: a
    regeneration that produced the same bytes has nothing to record, and
    `git commit` on an unchanged tree fails.
    """
    r = subprocess.run(["git", "status", "--porcelain", "FINDINGS.md"],
                       cwd=TARGET, capture_output=True, text=True)
    if not r.stdout.strip():
        return False
    sh(["git", "add", "FINDINGS.md"], TARGET)
    sh(["git", "commit", "-m", message], TARGET)
    return True


def refresh_findings(conn):
    """Render the window over the store's rows into the target's FINDINGS.md.

    A `conn` of None makes this a no-op, like `set_phase()` and
    `record_round()`: a storeless `run_task()` has no rows to render, and the
    file it would otherwise overwrite with an empty window is left alone.
    """
    if conn is None:
        return
    write_findings(conn)


def claim_run(conn, project, task):
    """Mirror the claimed ticket and take the project's lease; return both ids.

    Raises `store.ClaimConflict` when the project already has an active run —
    the point of routing the claim through the store, since that is what stops
    two loops from working one project at once.

    The ticket id comes back beside the run id because the two are separate
    subjects: the run is what phases and rounds are recorded against, and the
    ticket is what status is projected onto Linear from.

    Where the mirror lands is the store's routing rule (§2) applied to what
    the provider parsed: a ticket carrying both acceptance criteria and a
    verify command is `ready`, and one missing either is `needs_spec` and not
    pickable. The loop still picks in Linear, so what that decides here is
    whether the ticket can legally enter `in_flight` — an under-specced mirror
    cannot, and the board simply keeps saying whatever a human last set.

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
        acceptance_criteria=task.get("criteria") or (),
        verification_commands=[task["verify"]] if task.get("verify") else (),
        time_box_ms=task["budget_min"] * 60 * 1000,
    )
    return ticket_id, store.claim(conn, project, ticket_id)


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


# --- Linear as the notice board ----------------------------------------------
# State-model §1: Holophyte owns the in-flight substate and Linear is where it
# gets posted, so ticket status travels one way — store to Linear, never read
# back — and `mirror_push()` is the loop's only writer of a workflow state.
# That is what stops loop logic from depending on which column a human dragged
# a card into: the drag is overwritten by the next push, and nothing branches
# on it.
#
# The table is module-level config rather than five literals at the call sites
# because the thing likeliest to change is the mapping, not the pushing —
# per-project mappings and §9's team-vs-project question (does a project or a
# team own these state names?) both land here. Neither is answered here.
MIRROR_STATES = {
    "ready": "Todo",
    "in_flight": "In Progress",
    "merged": "Done",
    "abandoned": "Canceled",
    # No Linear state means "waiting on an operator", so a blocked ticket goes
    # back to the column a human picks work out of, until there is a state
    # that says it properly. Telling the operator what is being waited on is a
    # comment, and status comments are not this projection's business.
    "blocked_on_operator": "Todo",
}
# `needs_spec` and `blocked_on_deps` are deliberately unmapped: neither is a
# statement about the board — an unspecced ticket is wherever its author left
# it, and a dependency-blocked one is the resolver's business — so the
# projection leaves those tickets' Linear state alone rather than inventing a
# column for them.


def warn(conn, ticket_id, summary):
    """Record a warning on the ticket's run and print it; never raise.

    Best-effort work that failed is still something the run has to account
    for, so it goes in the same event stream as the phase changes, as a
    `warning` row a reader can pick out of the narrative. It is written
    against the ticket's run — the active one, or the last one when the run
    has already been released — because that is the stream a reader looking
    at this ticket is already reading.
    """
    print(f"[holo2] {summary}")
    if conn is None:
        return
    row = conn.execute(
        "SELECT COALESCE(activeRunId, lastRunId) FROM tickets WHERE id = ?",
        (ticket_id,)).fetchone()
    if row is None or row[0] is None:
        return  # no run to hang it on; the printed line is the whole record
    store.record_event(conn, row[0], "warning", summary)


def mirror_push(conn, ticket_id, provider=None):
    """Project the ticket's stored status onto its Linear state; return it.

    Last-write-wins and one-way: the store's `tickets.status` is the truth,
    `MIRROR_STATES` says what that truth looks like on the board, and nothing
    is read back. Returns the state pushed, or None when there was nothing to
    push — an unmapped status, or a push that failed.

    Failure is a warning, not an error. Linear being unreachable must not fail
    a run that has already merged, so a push that raises leaves the board
    stale, the store right, and a `warning` row in the run's stream saying
    which projection did not land. That is the failure §1 asks for: the notice
    board is allowed to be behind, the loop is not allowed to be wrong.

    A `conn` of None makes this a no-op, like the rest of the store seam: a
    storeless `run_task()` holds no status to project.
    """
    if conn is None:
        return None
    row = conn.execute(
        "SELECT linearIssueId, linearIdentifier, status FROM tickets"
        " WHERE id = ?", (ticket_id,)).fetchone()
    if row is None:
        raise ValueError(f"no ticket {ticket_id}")
    issue_id, identifier, status = row
    state = MIRROR_STATES.get(status)
    if state is None:
        return None
    if provider is None:
        import linear_provider as provider
    try:
        provider.set_state(issue_id, state)
    except Exception as e:
        warn(conn, ticket_id, f"Linear mirror push failed for {identifier}: "
                              f"{status} -> {state} ({e}); the store keeps"
                              " the status and the board stays stale")
        return None
    return state


def mirror_status(conn, ticket_id, status, provider=None):
    """Move the ticket to `status` in the store, then push the move to Linear.

    The pair the loop calls at a boundary that changes ticket status, in that
    order: the store is written first because it is the truth, and Linear is
    told afterwards because it is a copy.

    Returns whether the store took the move, not what was pushed, because the
    two failures are not the same kind of thing. A push that does not land
    leaves a stale board and a `mirror_push()` warning, and the caller carries
    on regardless; a move the §3 diagram does not draw means the ticket is not
    where the caller thought it was, and only the caller knows whether its
    next step still makes sense. So the refusal is warned about, nothing is
    pushed — the board keeps showing the status the ticket actually has — and
    False says the status change did not happen.

    A storeless caller gets False for the same reason `mirror_push()` gets
    None: there is no status here to move or to project.
    """
    if conn is None:
        return False
    try:
        store.transition(conn, ticket_id, status)
    except store.IllegalTransition as e:
        warn(conn, ticket_id, f"ticket status left where it was: {e}")
        return False
    mirror_push(conn, ticket_id, provider)
    return True


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
                ticket_id, run_id = claim_run(conn, project, task)
            except store.ClaimConflict as e:
                # Before any branch or worktree exists: another loop holds the
                # project, so this one stops rather than working beside it.
                print(f"[holo2] claim refused, not starting a run: {e}")
                return
            # §3's `ready -> in_flight`, and the first thing the board is told
            # about this run: the claim is the moment the ticket starts being
            # worked, and the projection replaces the state call the provider
            # used to make on its own.
            if not mirror_status(conn, ticket_id, "in_flight", provider):
                # The store refused the move, so this ticket is not `ready`
                # and no work may start on it. The ordinary cause is a board
                # that is behind the store — a ticket already `merged` whose
                # Done push did not land is still non-terminal in Linear and
                # so is offered again on the next pass — and §1 is exactly
                # that the store, not the column, decides. Running anyway
                # would re-implement merged work, once per pass for as long
                # as the board stays stale.
                #
                # So: give the lease straight back, re-project the status the
                # ticket really has as one more best-effort attempt at
                # unsticking the board, and stop. Stopping rather than taking
                # the next ticket is the same call as a refused claim above —
                # store and board disagree about what is workable, and a
                # human wants to know — and it is also what keeps a stale
                # ticket the re-push cannot move (an unmapped status, a Linear
                # that is down) from being claimed round and round forever.
                store.release(conn, run_id, "failed",
                              "ticket was not ready when the run was claimed;"
                              " no work started")
                mirror_push(conn, ticket_id, provider)
                print("[holo2] claimed ticket is not in a status work starts"
                      " from; stopping for a human")
                return
            merged = False
            try:
                merged = run_task(task, conn, run_id)
            finally:
                release_run(conn, run_id, merged)
                if merged:
                    # `in_flight -> merged`, projected as Done. A run that did
                    # not merge leaves the ticket in flight on purpose: the
                    # branch is preserved for a human and the board should go
                    # on saying the work is open, so there is nothing to push.
                    mirror_status(conn, ticket_id, "merged", provider)
                # Close-out, and the first moment the run's own outcome is a
                # row: the window is regenerated here rather than inside
                # `run_task()` so the entry that ends the run is in it.
                refresh_findings(conn)
            if not merged:
                # The regenerated window stays uncommitted, like the preserved
                # branch it describes: a human closes both out.
                return  # stop on first failure; ticket stays In Progress for a human
            commit_findings(f"Complete task {task['id']}: {task['title']}")
    finally:
        conn.close()


# --- estimate vs actual ------------------------------------------------------
# The rows already carry every number a burndown needs: when a run started and
# ended, the estimate it was claimed under, how many rounds it took. So the
# report is a query and an aligned print rather than a grep over FINDINGS.md --
# the ledger line was the only reading of this data until now, and a rendering
# of the newest 25 entries is not something a calibration question can be
# asked of. Nothing here writes, claims or calls Linear.
REPORT_HEADERS = ("ticket", "actual", "estimate", "ratio", "rounds", "outcome")
REPORT_GAP = "  "


def report_rows(conn):
    """Every ended run, oldest first, as the report's own tuple.

    `(ticket, actual_min, estimate_min, ratio, rounds, outcome)`, with
    `estimate` and `ratio` None when the run was claimed against no estimate
    -- an older run, or a ticket Linear gave no points. None rather than zero
    because "not comparable" is not a ratio of nothing, and the summary below
    leaves those runs out of its averages instead of dragging them to 0.
    """
    rows = []
    for ticket, started, ended, time_box, rounds, outcome in conn.execute(
            "SELECT t.linearIdentifier, r.startedAt, r.endedAt, r.timeBoxMs,"
            " r.reviewRoundCount, r.outcome"
            " FROM runs r JOIN tickets t ON t.id = r.ticketId"
            " WHERE r.endedAt IS NOT NULL"
            " ORDER BY r.endedAt, r.id").fetchall():
        actual = (ended - started) / 60000
        estimate = time_box / 60000 if time_box else None
        rows.append((ticket, actual, estimate,
                     actual / estimate if estimate else None,
                     rounds, outcome or "ended"))
    return rows


def report_summary(rows):
    """The last line: how many runs, and how the ratios sit.

    Mean and median together, because they answer different halves of the
    calibration question -- the mean carries the one run that blew its budget,
    the median says what a typical ticket costs -- and the early signal worth
    watching (actuals running several times under estimate) is a claim about
    the middle, not about the total.
    """
    ratios = [row[3] for row in rows if row[3] is not None]
    if not ratios:
        return f"{len(rows)} runs · no estimates to compare against"
    counted = (f"{len(rows)} runs" if len(ratios) == len(rows)
               else f"{len(rows)} runs · {len(ratios)} with an estimate")
    return (f"{counted} · mean ratio {statistics.fmean(ratios):.2f}"
            f" · median ratio {statistics.median(ratios):.2f}")


def report_lines(conn):
    """The whole report as lines: a header, one line per ended run, a summary.

    Columns are padded to the widest cell in them so the numbers line up in a
    terminal; the ticket and the outcome read left, everything numeric reads
    right. A store with no ended run says so rather than printing a header
    over nothing.
    """
    rows = report_rows(conn)
    if not rows:
        return ["no completed runs yet"]
    table = [REPORT_HEADERS]
    for ticket, actual, estimate, ratio, rounds, outcome in rows:
        table.append((
            ticket,
            f"{actual:.1f}",
            f"{estimate:.0f}" if estimate is not None else "n/a",
            f"{ratio:.2f}" if ratio is not None else "n/a",
            str(rounds),
            outcome,
        ))
    widths = [max(len(cell) for cell in column) for column in zip(*table)]
    lines = [
        REPORT_GAP.join(
            cell.ljust(width) if i in (0, len(widths) - 1) else cell.rjust(width)
            for i, (cell, width) in enumerate(zip(row, widths))).rstrip()
        for row in table
    ]
    return lines + [report_summary(rows)]


def report(conn=None, out=None):
    """Print the target store's estimate-vs-actual table. Returns nothing.

    `--report`'s whole body: it reads rows and prints them, so no ticket is
    claimed, no worktree is cut and no provider is imported -- which is what
    makes it safe to run against the store of a loop that is still working.

    The one write it can make is `open_store()`'s migration, so a store older
    than the run row's estimate column is brought up to the schema this
    queries instead of failing on the missing column. A target with no store
    at all is not created for the sake of an empty table; it is reported.
    """
    out = out or sys.stdout
    if conn is None and not STORE_PATH.exists():
        print(f"[holo2] no store at {STORE_PATH}", file=out)
        return
    owned = conn is None
    conn = conn if conn is not None else open_store()
    try:
        print("\n".join(report_lines(conn)), file=out)
    finally:
        if owned:
            conn.close()


def cli(argv=None):
    """Parse the command line and run the mode it names.

    An explicit parser rather than `sys.argv[1]`, which is what the target
    path used to be read from at import: every first argument was a repository
    path, so `--help` named a repository called "--help" and a mistyped flag
    started a real loop somewhere unintended. Both are now argparse errors,
    and the module can be imported without a command line at all.
    """
    parser = argparse.ArgumentParser(
        prog="factory.py",
        description="Holophyte: a minimal Linear-driven software factory.")
    parser.add_argument(
        "target", nargs="?", default=str(DEFAULT_TARGET),
        help="repository the loop works in (default: %(default)s)")
    parser.add_argument(
        "--report", action="store_true",
        help="print the target store's estimate-vs-actual table and exit; "
             "reads only -- claims no ticket, cuts no worktree, calls nobody")
    args = parser.parse_args(argv)
    retarget(args.target)
    if args.report:
        return report()
    return main()


if __name__ == "__main__":
    cli()
