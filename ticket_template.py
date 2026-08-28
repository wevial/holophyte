#!/usr/bin/env python3
"""ticket_template: parser/validator for ticketTemplate.md-shaped tickets.

parse(text) pulls a ticket apart into fields; validate(ticket) returns a
list of human-readable problems (empty list = valid). The checks mirror the
rules the template itself states: required sections in order, at least one
"- [ ]" acceptance criterion, runnable relative-path-only verify commands in
a ``` fence, an optional "Contract checks" fence of "path: literal"
declarations, an "Estimate: N min · Depends on: ..." line, and open questions
reading exactly "- None". Unfilled placeholders ({{...}} or <...>) fail.

Scope is capped mechanically as well: a ticket over MAX_ESTIMATE_MIN minutes,
MAX_CRITERIA acceptance criteria, or MAX_IN_SCOPE "In scope" entries is
rejected — scope rules kept only in prose get skipped. Every list entry
counts toward those caps, whatever its marker ("-", "*", "+", "1."), and an
acceptance criterion that is not a "- [ ]" checkbox is rejected on its own,
so extra scope cannot hide behind list syntax the parser might skip. A
"What:" line that chains deliverables instead yields an
ADVISORY_PREFIX-marked note, which does not affect validity; blocking() drops
advisories for callers that gate on the result.

Markdown that Linear has normalized round-trips: "**What: **" for "**What:**"
is the same structure as the plain form, and any bullet marker ("-", "*",
"+") reads the same, so those variants are accepted. Loose formatting beyond
those equivalences still fails.

CLI: python3 ticket_template.py TICKET.md [...]  ->  exit 0 iff all valid.
"""
import re
import sys
from pathlib import Path

# Every section the template defines, in order. "Contract checks" is optional
# so tickets without literal requirements stay valid; the rest are required.
TEMPLATE_ORDER = [
    "Summary", "What / Why / How", "In scope", "Out of scope",
    "Acceptance criteria", "Verify command(s)", "Contract checks",
    "Implementation notes", "Estimate & dependencies", "Open questions",
]
OPTIONAL_SECTIONS = {"Contract checks"}
SECTION_ORDER = [s for s in TEMPLATE_ORDER if s not in OPTIONAL_SECTIONS]
# Mechanical scope caps. Module-level so a future per-project config can
# override them without touching validate().
MAX_ESTIMATE_MIN = 30
MAX_CRITERIA = 5
MAX_IN_SCOPE = 3
# Marks a validate() entry as guidance rather than a rejection.
ADVISORY_PREFIX = "advisory: "
# Connectives that usually mean a "What:" line describes two deliverables.
# Advisory only: "read and write the cache" is one deliverable, so a human
# decides — the caps above are what actually gate.
SCOPE_CHAINING = (" and ", ";", ", then ")
H1_RE = re.compile(r"^#\s+(.*?)\s*$")
H2_RE = re.compile(r"^##\s+(.*?)\s*$")
# Markdown's three bullet markers. All of them render identically, so a cap
# that recognized only some of them would be bypassable by typing "+".
BULLET = r"[-*+]"
UNCHECKED_RE = re.compile(rf"^{BULLET}\s+\[ \]\s*(.*)$")
CHECKED_RE = re.compile(rf"^{BULLET}\s+\[[xX]\]\s*(.*)$")
# Any list entry, bulleted or numbered. Counting the loosest list shape --
# everywhere a cap applies -- is what keeps entries from slipping past
# MAX_CRITERIA or MAX_IN_SCOPE by wearing a marker the parser skipped.
LIST_ITEM_RE = re.compile(rf"^(?:{BULLET}|\d+[.)])\s+(.*)$")
# Linear rewrites "**What:**" as "**What: **", so the space is allowed inside
# the bold run — but the key, the colon, and the bold markers stay required.
BOLD_KEY_RE = re.compile(r"^\*\*(What|Why|How):[ \t]*\*\*\s*(.*)$")
ESTIMATE_RE = re.compile(r"^Estimate:\s*(\d+)\s*min\s*·\s*Depends on:\s*(.+)$")
LINEAR_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")
PLACEHOLDER_RE = re.compile(r"\{\{[^{}]*\}\}|<[^<>\n\s][^<>\n]*>")
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
FENCE_RE = re.compile(r"^```\w*\s*$")
# One literal contract declaration: a relative path, a colon, and the exact
# value that must appear in that file. Literal only — no regex, no shell.
CONTRACT_RE = re.compile(r"^(\S+?):\s*(.*)$")
OPEN_QUESTIONS_NONE = ("- None", "* None", "+ None")


def _clean(s):
    return re.sub(r"\s+", " ", s).strip()


def _list_items(body):
    """Every list entry in the section, bulleted (-, *, +) or numbered."""
    out = []
    for line in body.splitlines():
        m = LIST_ITEM_RE.match(line.strip())
        if m:
            out.append(_clean(m.group(1)))
    return out


def _criteria(body):
    """Acceptance-criteria list entries as (unchecked, checked, other).

    "other" is every list entry that is not a "- [ ]"/"- [x]" checkbox — a
    plain bullet, a numbered item. The author wrote those as criteria, so
    dropping them would let a ticket carry more than MAX_CRITERIA and still
    validate clean; validate() counts them toward the cap and rejects their
    form instead.
    """
    unchecked, checked, other = [], [], []
    for line in body.splitlines():
        s = line.strip()
        item = LIST_ITEM_RE.match(s)
        if not item:
            continue
        done, todo = CHECKED_RE.match(s), UNCHECKED_RE.match(s)
        if todo:
            unchecked.append(_clean(todo.group(1)))
        elif done:
            checked.append(_clean(done.group(1)))
        else:
            other.append(_clean(item.group(1)))
    return unchecked, checked, other


def _fenced_lines(body):
    """Non-blank lines inside the section's ``` fence."""
    out, in_fence = [], False
    for line in body.splitlines():
        s = line.strip()
        if FENCE_RE.match(s):
            in_fence = not in_fence
            continue
        if in_fence and s:
            out.append(s)
    return out


def _verify_commands(body):
    """Command lines inside the section's ``` fence.

    Blank lines and the template's own annotation ("Rules:" plus its bullet
    list) are skipped; everything else left in the fence is a command.
    """
    return [s for s in _fenced_lines(body)
            if not s.startswith("Rules:") and not s.startswith("- ")]


def _contract_checks(body):
    """Literal contract declarations inside the section's ``` fence.

    Each line is "relative/path: exact literal" and becomes a (path, literal)
    pair, verbatim — no globbing, no regex, no substitution. A line without a
    colon yields ("", line) so validate() can name the malformed declaration
    instead of silently dropping a contract the ticket meant to enforce.

    The template's trailing "Rules:" annotation and everything under it are
    prose, not declarations, so scanning stops there.
    """
    checks = []
    for line in _fenced_lines(body):
        if line.startswith("Rules:"):
            break
        m = CONTRACT_RE.match(line)
        checks.append((m.group(1), m.group(2).strip()) if m else ("", line))
    return checks


def _deps(raw):
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) == 1 and parts[0].lower() == "none":
        return []
    return parts


class Ticket:
    """Parsed ticket. sections keeps raw body text keyed by heading."""

    def __init__(self):
        self.title = ""
        self.order = []
        self.sections = {}
        self.stray_h1s = []
        self.summary = ""
        self.what = self.why = self.how = ""
        self.in_scope = []
        self.out_of_scope = []
        self.acceptance = []
        self.acceptance_done = []
        self.acceptance_other = []
        self.verify_commands = []
        self.contract_checks = []
        self.notes = []
        self.estimate_min = None
        self.depends_on = None
        self.open_questions_none = False


def parse(text):
    """Parse markdown text into a Ticket. Lenient: malformed input yields a
    sparsely-filled Ticket; validate() reports what's wrong."""
    t = Ticket()
    lines = text.splitlines()
    first_h1 = next((i for i, ln in enumerate(lines) if H1_RE.match(ln)), None)
    if first_h1 is None:
        body_start = 0
    else:
        t.title = H1_RE.match(lines[first_h1]).group(1)
        t.stray_h1s = [H1_RE.match(ln).group(1) for ln in lines[first_h1 + 1:]
                       if H1_RE.match(ln)]
        body_start = first_h1 + 1

    cur, buf = None, []
    for ln in lines[body_start:]:
        h2 = H2_RE.match(ln)
        if h2:
            if cur is not None:
                _keep(t, cur, "\n".join(buf))
            cur, buf = h2.group(1).strip(), []
        elif cur is not None:
            buf.append(ln)
    if cur is not None:
        _keep(t, cur, "\n".join(buf))

    t.summary = _clean(t.sections.get("Summary", ""))
    kv = {}
    for ln in t.sections.get("What / Why / How", "").splitlines():
        m = BOLD_KEY_RE.match(ln.strip())
        if m:
            kv[m.group(1)] = _clean(m.group(2))
    t.what, t.why, t.how = kv.get("What", ""), kv.get("Why", ""), kv.get("How", "")
    t.in_scope = _list_items(t.sections.get("In scope", ""))
    t.out_of_scope = _list_items(t.sections.get("Out of scope", ""))
    t.acceptance, t.acceptance_done, t.acceptance_other = _criteria(
        t.sections.get("Acceptance criteria", ""))
    t.verify_commands = _verify_commands(t.sections.get("Verify command(s)", ""))
    t.contract_checks = _contract_checks(t.sections.get("Contract checks", ""))
    t.notes = _list_items(t.sections.get("Implementation notes", ""))
    est = None
    for ln in t.sections.get("Estimate & dependencies", "").splitlines():
        m = ESTIMATE_RE.match(ln.strip())
        if m:
            est = m
            break
    t.estimate_min = int(est.group(1)) if est else None
    t.depends_on = _deps(est.group(2)) if est else None
    oq = COMMENT_RE.sub("", t.sections.get("Open questions", ""))
    t.open_questions_none = _clean(oq) in OPEN_QUESTIONS_NONE
    return t


def _keep(t, name, body):
    t.order.append(name)
    t.sections.setdefault(name, body)


def _labeled_texts(t):
    """Every filled text field as (label, text) — placeholder scanning."""
    yield "title", t.title
    yield "Summary", t.summary
    for label, v in (("What:", t.what), ("Why:", t.why), ("How:", t.how)):
        if v:
            yield label, v
    lists = (("In scope", t.in_scope), ("Out of scope", t.out_of_scope),
             ("Acceptance criteria", t.acceptance),
             ("Implementation notes", t.notes))
    for label, items in lists:
        for i, item in enumerate(items, 1):
            yield f"{label} #{i}", item
    for cmd in t.verify_commands:
        yield "verify command", cmd
    for path, literal in t.contract_checks:
        yield "contract check", f"{path}: {literal}"


def _has_nonrelative_path(cmd):
    scrubbed = re.sub(r'"[^"]*"', '""', cmd)
    for piece in re.split(r"[=\s]", scrubbed):
        if piece.startswith("/") or piece.startswith("~/"):
            return True
        if re.match(r"^[A-Za-z]:[\\/]", piece):
            return True
    return False


def contract_path_problem(path):
    """Why `path` is unusable as a contract-check target, or None if it is a
    plain relative repository path. Shared with the factory's verify gate so
    the ticket-time rule and the run-time rule cannot drift apart."""
    if not path:
        return "declaration must read 'relative/path: expected literal'"
    if (path.startswith("/") or path.startswith("~")
            or re.match(r"^[A-Za-z]:[\\/]", path)):
        return "path must be relative to the repo root"
    if ".." in path.split("/"):
        return "path must stay inside the repo (no '..' segments)"
    return None


def validate(t):
    """All template violations as human-readable strings.

    An entry starting with ADVISORY_PREFIX is scope guidance, not a
    violation: the ticket is valid iff blocking(validate(t)) is empty."""
    p = []
    if not t.title:
        p.append("missing H1 title ('# ...' on the first heading line)")
    if t.stray_h1s:
        p.append(f"{len(t.stray_h1s)} extra H1 heading(s); exactly one allowed")

    seen_first = {}
    for idx, name in enumerate(t.order):
        seen_first.setdefault(name, idx)
    # Compare only the template sections actually present, so an absent
    # optional section does not open a gap in the order check.
    present = [n for n in TEMPLATE_ORDER if n in seen_first]
    for a, b in zip(present, present[1:]):
        if seen_first[a] >= seen_first[b]:
            p.append(f"sections out of template order: '## {a}' must come before '## {b}'")
    for name in TEMPLATE_ORDER:
        n = t.order.count(name)
        if n == 0 and name not in OPTIONAL_SECTIONS:
            p.append(f"missing section '## {name}'")
        elif n > 1:
            p.append(f"duplicate section '## {name}' ({n}x)")
    for u in sorted(set(t.order) - set(TEMPLATE_ORDER)):
        p.append(f"unknown section '## {u}' (not in ticketTemplate.md)")

    for label, text in _labeled_texts(t):
        m = PLACEHOLDER_RE.search(text)
        if m:
            p.append(f"unfilled template placeholder in {label}: {m.group(0)}")

    if not t.summary:
        p.append("'Summary' is empty")
    if not t.what:
        p.append("'**What:**' line missing under 'What / Why / How'")
    if not t.how:
        p.append("'**How:**' line missing under 'What / Why / How' "
                 "('Why' is optional)")
    if not t.in_scope:
        p.append("'In scope' has no entries")
    elif len(t.in_scope) > MAX_IN_SCOPE:
        p.append(f"'In scope' has {len(t.in_scope)} entries; the cap is "
                 f"{MAX_IN_SCOPE} — split the ticket")
    if not t.out_of_scope:
        p.append("'Out of scope' has no entries")

    if not t.acceptance and not t.acceptance_done:
        p.append("'Acceptance criteria' has no '- [ ] Given/when/then' items")
    elif not t.acceptance:
        p.append("all acceptance criteria are already checked off")
    for i, ac in enumerate(t.acceptance, 1):
        if not ac:
            p.append(f"acceptance criterion #{i} is empty")
    for item in t.acceptance_other:
        p.append(f"acceptance criterion is not a '- [ ] ...' checkbox: {item}")
    n_criteria = (len(t.acceptance) + len(t.acceptance_done)
                  + len(t.acceptance_other))
    if n_criteria > MAX_CRITERIA:
        p.append(f"'Acceptance criteria' has {n_criteria} items; the cap is "
                 f"{MAX_CRITERIA} — split the ticket")

    if not t.verify_commands:
        p.append("'Verify command(s)' has no runnable command lines inside "
                 "a ``` fence")
    for cmd in t.verify_commands:
        if _has_nonrelative_path(cmd):
            p.append(f"verify command uses a non-relative path "
                     f"(template rule: relative only): {cmd}")

    if "Contract checks" in t.order and not t.contract_checks:
        p.append("'Contract checks' section has no 'relative/path: expected "
                 "literal' declarations inside a ``` fence")
    for path, literal in t.contract_checks:
        problem = contract_path_problem(path)
        if problem:
            p.append(f"contract check {problem}: {path or literal}")
        elif not literal:
            p.append(f"contract check has an empty expected literal: {path}")

    if t.estimate_min is None:
        p.append("'Estimate & dependencies' must read "
                 "'Estimate: N min · Depends on: <ticket IDs or \"none\">'")
    else:
        if t.estimate_min > MAX_ESTIMATE_MIN:
            p.append(f"estimate is {t.estimate_min} min; the cap is "
                     f"{MAX_ESTIMATE_MIN} min — split the ticket")
        for d in t.depends_on:
            if not LINEAR_ID_RE.match(d):
                p.append(f"'Depends on' entry is not a ticket ID or \"none\": {d}")

    if not t.open_questions_none:
        p.append("'Open questions' must read exactly '- None' before the "
                 "ticket enters the pickable queue")

    chained = next((m for m in SCOPE_CHAINING if m in t.what.lower()), None)
    if chained:
        p.append(f"{ADVISORY_PREFIX}'**What:**' chains scope on {chained!r}; "
                 f"consider splitting it into one ticket per deliverable")
    return p


def blocking(problems):
    """The entries of a validate() result that make a ticket invalid.

    Advisories are guidance about scope shape, not template violations, so a
    caller gating on validity filters them out and still shows them."""
    return [pr for pr in problems if not pr.startswith(ADVISORY_PREFIX)]


def main(argv):
    if not argv:
        print("usage: python3 ticket_template.py TICKET.md [...]", file=sys.stderr)
        return 2
    invalid = 0
    for path in argv:
        problems = validate(parse(Path(path).read_text()))
        blockers = blocking(problems)
        if blockers:
            invalid += 1
            print(f"{path}: INVALID")
        else:
            print(f"{path}: OK")
        advisories = [a for a in problems if a.startswith(ADVISORY_PREFIX)]
        for pr in blockers + advisories:
            print(f"  - {pr}")
    return 1 if invalid else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
