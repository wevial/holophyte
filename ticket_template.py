#!/usr/bin/env python3
"""ticket_template: parser/validator for ticketTemplate.md-shaped tickets.

parse(text) pulls a ticket apart into fields; validate(ticket) returns a
list of human-readable problems (empty list = valid). The checks mirror the
rules the template itself states: required sections in order, at least one
"- [ ]" acceptance criterion, runnable relative-path-only verify commands in
a ``` fence, an "Estimate: N min · Depends on: ..." line, and open questions
reading exactly "- None". Unfilled placeholders ({{...}} or <...>) fail.

CLI: python3 ticket_template.py TICKET.md [...]  ->  exit 0 iff all valid.
"""
import re
import sys
from pathlib import Path

SECTION_ORDER = [
    "Summary", "What / Why / How", "In scope", "Out of scope",
    "Acceptance criteria", "Verify command(s)", "Implementation notes",
    "Estimate & dependencies", "Open questions",
]
H1_RE = re.compile(r"^#\s+(.*?)\s*$")
H2_RE = re.compile(r"^##\s+(.*?)\s*$")
BULLET_RE = re.compile(r"^[-*]\s+(.*)$")
UNCHECKED_RE = re.compile(r"^[-*]\s+\[ \]\s*(.*)$")
CHECKED_RE = re.compile(r"^[-*]\s+\[[xX]\]\s*(.*)$")
BOLD_KEY_RE = re.compile(r"^\*\*(What|Why|How):\*\*\s*(.*)$")
ESTIMATE_RE = re.compile(r"^Estimate:\s*(\d+)\s*min\s*·\s*Depends on:\s*(.+)$")
LINEAR_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")
PLACEHOLDER_RE = re.compile(r"\{\{[^{}]*\}\}|<[^<>\n\s][^<>\n]*>")
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
FENCE_RE = re.compile(r"^```\w*\s*$")


def _clean(s):
    return re.sub(r"\s+", " ", s).strip()


def _bullets(body):
    out = []
    for line in body.splitlines():
        m = BULLET_RE.match(line.strip())
        if m:
            out.append(_clean(m.group(1)))
    return out


def _checkboxes(body, rx):
    out = []
    for line in body.splitlines():
        m = rx.match(line.strip())
        if m:
            out.append(_clean(m.group(1)))
    return out


def _verify_commands(body):
    """Command lines inside the section's ``` fence.

    Blank lines and the template's own annotation ("Rules:" plus its bullet
    list) are skipped; everything else left in the fence is a command.
    """
    cmds, in_fence = [], False
    for line in body.splitlines():
        s = line.strip()
        if FENCE_RE.match(s):
            in_fence = not in_fence
            continue
        if not in_fence or not s or s.startswith("Rules:") or s.startswith("- "):
            continue
        cmds.append(s)
    return cmds


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
        self.verify_commands = []
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
    t.in_scope = _bullets(t.sections.get("In scope", ""))
    t.out_of_scope = _bullets(t.sections.get("Out of scope", ""))
    ac = t.sections.get("Acceptance criteria", "")
    t.acceptance = _checkboxes(ac, UNCHECKED_RE)
    t.acceptance_done = _checkboxes(ac, CHECKED_RE)
    t.verify_commands = _verify_commands(t.sections.get("Verify command(s)", ""))
    t.notes = _bullets(t.sections.get("Implementation notes", ""))
    est = None
    for ln in t.sections.get("Estimate & dependencies", "").splitlines():
        m = ESTIMATE_RE.match(ln.strip())
        if m:
            est = m
            break
    t.estimate_min = int(est.group(1)) if est else None
    t.depends_on = _deps(est.group(2)) if est else None
    oq = COMMENT_RE.sub("", t.sections.get("Open questions", ""))
    t.open_questions_none = _clean(oq) == "- None"
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


def _has_nonrelative_path(cmd):
    scrubbed = re.sub(r'"[^"]*"', '""', cmd)
    for piece in re.split(r"[=\s]", scrubbed):
        if piece.startswith("/") or piece.startswith("~/"):
            return True
        if re.match(r"^[A-Za-z]:[\\/]", piece):
            return True
    return False


def validate(t):
    """All template violations as human-readable strings; [] means valid."""
    p = []
    if not t.title:
        p.append("missing H1 title ('# ...' on the first heading line)")
    if t.stray_h1s:
        p.append(f"{len(t.stray_h1s)} extra H1 heading(s); exactly one allowed")

    for name in SECTION_ORDER:
        n = t.order.count(name)
        if n == 0:
            p.append(f"missing section '## {name}'")
        elif n > 1:
            p.append(f"duplicate section '## {name}' ({n}x)")
    for u in sorted(set(t.order) - set(SECTION_ORDER)):
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
        p.append("'In scope' has no bullets")
    if not t.out_of_scope:
        p.append("'Out of scope' has no bullets")

    if not t.acceptance and not t.acceptance_done:
        p.append("'Acceptance criteria' has no '- [ ] Given/when/then' items")
    elif not t.acceptance:
        p.append("all acceptance criteria are already checked off")
    for i, ac in enumerate(t.acceptance, 1):
        if not ac:
            p.append(f"acceptance criterion #{i} is empty")

    if not t.verify_commands:
        p.append("'Verify command(s)' has no runnable command lines inside "
                 "a ``` fence")
    for cmd in t.verify_commands:
        if _has_nonrelative_path(cmd):
            p.append(f"verify command uses a non-relative path "
                     f"(template rule: relative only): {cmd}")

    if t.estimate_min is None:
        p.append("'Estimate & dependencies' must read "
                 "'Estimate: N min · Depends on: <ticket IDs or \"none\">'")
    else:
        for d in t.depends_on:
            if not LINEAR_ID_RE.match(d):
                p.append(f"'Depends on' entry is not a ticket ID or \"none\": {d}")

    if not t.open_questions_none:
        p.append("'Open questions' must read exactly '- None' before the "
                 "ticket enters the pickable queue")
    return p


def main(argv):
    if not argv:
        print("usage: python3 ticket_template.py TICKET.md [...]", file=sys.stderr)
        return 2
    invalid = 0
    for path in argv:
        problems = validate(parse(Path(path).read_text()))
        if problems:
            invalid += 1
            print(f"{path}: INVALID")
            for pr in problems:
                print(f"  - {pr}")
        else:
            print(f"{path}: OK")
    return 1 if invalid else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
