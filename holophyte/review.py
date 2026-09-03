"""Reviewer output as structured findings: reply text in, rows out.

The verdict line a record must keep, the sanitizer every stored message goes
through, the best-effort split of a reviewer's prose into `{path, line?,
severity, message}` findings, the per-criterion checklist the reviewer is held
to, and the collapse of both reviewer vocabularies onto `reviewRounds.verdict`.
Pure text: nothing here reads config, the store, the target or the board, and
the one import beyond the standard library is `review_runner`, whose verdict
reader `round_verdict` wraps.

Third slice of the phase-2 module split; moved verbatim from `factory.py`,
which imports back the names its remaining call sites use.
"""
import hashlib
import re
from pathlib import Path

import review_runner

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
    placeholder with whatever severity it marked. The reviewer's own bullet is
    what says it filed a complaint, and this parser recognizing no path in it
    is a fact about the parser: `Dockerfile`, `Makefile` and a bare directory
    carry nothing to match on. Keeping only the items whose paths happen to
    parse would leave the round fingerprinted as a shorter complaint than the
    one that was made, and §6 compares those fingerprints. Prose around the
    list -- an opening sentence, the closing `VERDICT:` line -- is narration
    rather than a filed item and is not stored as one; a reply that filed no
    item at all still returns `raw_finding()` over the whole text, so a round
    is never recorded as having said nothing.
    """
    findings = []
    # The per-criterion checklist is the reviewer's account of the contract,
    # not a complaint: a `met` line naming its witnessing test cites a path,
    # and read as a finding it would file the test as a blocker. Its lines
    # are dropped before the split; `criteria_findings()` reads them.
    for block in finding_blocks(CRITERION_LINE_RE.sub("", reply)):
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


# One line of the reviewer's per-criterion checklist: `CRITERION n: met —
# TEST_OR_CHECK`, `not met — WHY` or `unwitnessed — WHAT_IS_MISSING`. The
# separator is loose (dash, em dash or colon) because a reviewer paraphrasing
# the prompt's punctuation has still answered the question; the status word is
# not, because `met` is the only answer that clears the gate.
CRITERION_LINE_RE = re.compile(
    r"^\s*CRITERION\s+(\d+)\s*:\s*(met|not met|unwitnessed)\b"
    r"\s*(?:[-\u2013\u2014:]+\s*)?(.*?)\s*$", re.I | re.M)
# The `path` a per-criterion finding is keyed under, with the criterion's
# number as its `line`: not a file any repository holds, and distinct per
# criterion so two unwitnessed criteria fingerprint as two complaints.
CRITERIA_PATH = "criteria"
UNWITNESSED_NOTE = "no CRITERION line in the reply"


def criteria_block(reply):
    """`{n: (status, note)}` for every CRITERION line in `reply`.

    A number the reviewer wrote twice keeps its last line, which is the one
    a reviewer correcting itself meant.
    """
    block = {}
    for match in CRITERION_LINE_RE.finditer(reply):
        block[int(match.group(1))] = (match.group(2).lower(), match.group(3))
    return block


# A test reference inside a witness: `tests/x.py::Cls::test_y`,
# `tests/x.py::test_y`, or the dotted `tests.x.Cls.test_y`. Prose witnesses
# and verify commands match neither and are left alone.
WITNESS_TEST_RE = re.compile(
    r"(?P<path>[\w./-]+\.py)::(?:(?P<cls>\w+)::)?(?P<name>test\w*)"
    r"|(?P<mod>tests(?:\.\w+)+)\.(?P<name2>test\w*)")
MISSING_WITNESS_NOTE = "named test not found: "


def test_references(witness):
    """`[(path, cls, name)]` for every test `witness` names; `cls` is None
    for a module-level test.

    The dotted form maps `tests.a.B.test_c` to `tests/a.py`, class `B`: the
    segment before the test name is a class only when it is capitalised,
    since a module is never written that way and a test class always is.
    """
    references = []
    for match in WITNESS_TEST_RE.finditer(witness or ""):
        if match.group("path"):
            references.append((match.group("path"), match.group("cls"),
                               match.group("name")))
            continue
        segments = match.group("mod").split(".")
        cls = None
        if len(segments) > 1 and segments[-1][0].isupper():
            cls = segments.pop()
        references.append(("/".join(segments) + ".py", cls,
                           match.group("name2")))
    return references


def missing_witnesses(references, root):
    """One message per reference in `references` that `root` does not hold.

    A text scan of the named file, never an import: `def NAME(` at any
    indentation for a module-level test, or inside the block of `class CLS`
    — the lines indented deeper than the class line, up to the next line
    indented at or below it — when a class is named. No test runs here; the
    verify gate does that.
    """
    missing = []
    for path, cls, name in references:
        spec = f"{path}::{cls}::{name}" if cls else f"{path}::{name}"
        file = Path(root) / path
        if not file.is_file():
            missing.append(f"{spec} (no file {path})")
            continue
        lines = file.read_text(errors="replace").splitlines()
        if cls is None:
            if not any(re.match(rf"\s*def {name}\(", line) for line in lines):
                missing.append(f"{spec} (no def {name})")
            continue
        found = _defines_in_class(lines, cls, name)
        if found is None:
            missing.append(f"{spec} (no class {cls})")
        elif not found:
            missing.append(f"{spec} (no def {name} in class {cls})")
    return missing


def _defines_in_class(lines, cls, name):
    """True when `class cls` defines `name`, False when it exists without it,
    None when no such class is in `lines`."""
    seen = False
    for i, line in enumerate(lines):
        match = re.match(rf"(\s*)class {cls}\b", line)
        if match is None:
            continue
        seen = True
        depth = len(match.group(1))
        for inner in lines[i + 1:]:
            if inner.strip() and len(inner) - len(inner.lstrip()) <= depth:
                break
            if re.match(rf"\s*def {name}\(", inner):
                return True
    return False if seen else None


def criteria_findings(reply, criteria, root=None):
    """One finding per criterion `reply` did not witness; `[]` when all met.

    The gate KO-165 lacked: a reviewer that approves while a criterion is
    `not met` or `unwitnessed` — or that never answered for it at all — has
    filed a complaint against the candidate whatever its verdict line says,
    and this is that complaint in the findings shape the round stores. A
    task with no criteria has nothing to witness and always answers `[]`.

    With `root` — the round's worktree — a `met` witness that names a test
    (see `test_references()`) is also checked to exist there, and a criterion
    whose named test is fiction is downgraded to `unwitnessed`. Without it,
    the witness is taken at its word.
    """
    block = criteria_block(reply)
    findings = []
    for n, criterion in enumerate(criteria or (), 1):
        status, note = block.get(n, ("unwitnessed", UNWITNESSED_NOTE))
        if status == "met" and note and root is not None:
            missing = missing_witnesses(test_references(note), root)
            if missing:
                status, note = "unwitnessed", MISSING_WITNESS_NOTE + "; ".join(missing)
        if status == "met" and note:
            continue
        if status == "met":  # claimed, with nothing named to witness it
            status, note = "unwitnessed", "met claimed but no test or check named"
        message = (f"CRITERION {n}: {status} \u2014 {note or '(no reason given)'}"
                   f"\n{criterion}")
        findings.append({"path": CRITERIA_PATH, "line": n,
                         "severity": DEFAULT_SEVERITY,
                         "message": finding_message(message)})
    return findings


def criteria_brief(criteria):
    """The numbered criteria and the reply contract the reviewer is held to;
    empty for a task with none, so the prompt never asks for a block the
    loop would not read."""
    if not criteria:
        return ""
    numbered = "\n".join(f"{n}. {c}" for n, c in enumerate(criteria, 1))
    return (f"Acceptance criteria, numbered:\n{numbered}\n\n"
            "Before the VERDICT line, account for every criterion with "
            "exactly one line each, in this form:\n"
            "CRITERION n: met \u2014 TEST_OR_CHECK  (name the test or check "
            "that witnesses it)\n"
            "CRITERION n: not met \u2014 WHY\n"
            "CRITERION n: unwitnessed \u2014 WHAT_IS_MISSING\n"
            "Name tests as `tests/file.py::TestClass::test_name`; the loop "
            "checks the test exists.\n"
            "A criterion marked not met or unwitnessed, or left out of this "
            "list, is a blocker: the round is REQUEST_CHANGES regardless of "
            "the verdict line.\n\n")


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
