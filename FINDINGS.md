
## 2026-08-22T06:21:20Z — KO-105
MERGED to main (branch task/holophyte-rewrite-readme-for-t deleted). Verify: passed. Rounds used: 1.

## 2026-08-22T21:39:00Z — KO-107
Round 1: REQUEST_CHANGES -> fix round
Reviewer findings:
I reviewed `ticket_template.py`, `test_ticket_template.py`, and the README change in a4cf6dd, and checked the repo layout.

**Why the verify command failed:** the suite is `./test_ticket_template.py` at the repo root; there is no `tests/` directory (and none anywhere in the repo history of this worktree — `Glob **/*` shows only flat modules). `python -m unittest discover tests` therefore aborts at import, so the mechanical gate the loop runs before review and before merge cannot pass as committed. The README (line 40-41) documents a *different* invocation (`python3 -m unittest test_ticket_template`), so the code and the gate disagree; whichever is authoritative, one of them has to move.

**Second, a real correctness bug in the module's core job:** `parse()` collapses the whole "Estimate & dependencies" section with `_clean()` before matching `ESTIMATE_RE` (ticket_template.py:143). `ticketTemplate.md` ships an explanatory blockquote directly under that line ("> Machine-checkable dependencies are Linear **blocks** relations… Keep this line in sync…"), so any ticket authored by copying the template verbatim yields `depends_on = ['none > Machine-checkable dependencies are Linear **blocks** relations — the loop enforces only those. Anything outside Linear (DNS', 'human review', ' hardware) gates via triage…']` — a valid ticket is reported invalid with several garbled `'Depends on' entry is not a ticket ID` problems. The tests miss this because `FILLED` drops the blockquote. Match the estimate line per-line instead of over the cleaned section.

Non-blocking, for whoever picks this up: the module docstring claims it checks "required sections in order", but `validate()` only checks presence/duplicates/unknowns — never `t.order` sequence; and `main()` lets a missing path raise a raw traceback instead of a usage-level error.

Blockers:
1. `python -m unittest discover tests` fails — no importable `tests` package. Either add `tests/__init__.py` and move `test_ticket_template.py` there (keeping `import ticket_template` working), or correct the ticket's verify command to the flat-layout invocation the README documents.
2. Estimate parsing breaks on the template's own trailing blockquote, so template-derived tickets fail validation with bogus dependency errors (ticket_template.py:143). Add a test covering a `FILLED` variant that retains the blockquote.

VERDICT: REQUEST_CHANGES

Implementer response:
Both findings confirmed. Let me check how the factory resolves verify commands and look at commit style before deciding how to fix.
The factory extracts the verify command from the Linear ticket description (`linear_provider.py:115`), so the gate command isn't something I can edit here — blocker 1 must be fixed repo-side by making `tests/` discoverable. Let me confirm finding 2's failure mode first.
Confirmed the class of bug. Now implementing both ADDRESS fixes. First, the move for blocker 1:
Garble reproduced exactly as reported. Now the edits — test file first:
Now the `HERE` references that must point at the repo root:
Now blocker 2 — per-line estimate matching in `ticket_template.py:143`:
And the README so code, docs, and gate agree:
Now run the gate exactly as the ticket specifies, plus the README invocation, from a clean state:
All green — 34 tests (+3 new). Final sanity: end-to-end validate of a template-derived ticket that keeps the blockquote, plus leftover-reference sweep:
`estimate: None` there is just because the template's literal `N` isn't digits (unfilled template is expected invalid). One realistic end-to-end with digits, keeping the blockquote verbatim from the shipped template:
Parser now extracts the estimate line cleanly (`depends_on: ['X']` is just my crude placeholder fill for `<ticket IDs or "none">`; a real ticket has IDs/`none`, and the blockquote no longer leaks). Committing with adjudications:
Committed as `97f1a98`; gate `python -m unittest discover tests` passes (34 tests, exit 0).

**Adjudications:**

1. **Tests layout / verify command — ADDRESS.** The gate command comes from the Linear ticket (`linear_provider.py:115`), which I can't edit, so the repo had to move: added `tests/__init__.py`, moved the suite to `tests/test_ticket_template.py`, updated its path constants and run docstring, and pointed README.md:40-41 at `python3 -m unittest discover tests`. Code, docs, and gate now agree.

2. **Estimate parsing vs blockquote — ADDRESS.** `ticket_template.py` now matches `ESTIMATE_RE` per-line instead of over the `_clean()`-collapsed section (the fix for the `'none > Machine-checkable…'` garble). Added three tests on a new `FILLED_WITH_BLOCKQUOTE` fixture retaining the template's verbatim blockquote: parse fields with no deps, parse with ID deps, and full `validate() == []`.

**FOLLOW_UPs** (named in the commit message, untouched): `validate()` doesn't enforce section order despite the docstring claim; `main()` tracebacks on a missing path.

[0m
> build · stealth/ox-alpha
[0m
[0m$ [0mgit log --oneline -5 && git status && ls
a4cf6dd ticket_template: parser/validator module for ticketTemplate.md tickets
1987292 Complete task KO-105: Holophyte: rewrite README for the factory era
d810ec4 Merge task/holophyte-rewrite-readme-for-t: Holophyte: rewrite README for the factory era
e89b201 README: rewrite for the factory era (Linear tickets, worktrees, verify gates)
2378ac8 factory: Linear project ID comes only from HOLO2_PROJECT_ID env (KO-93)
On branch task/holophyte-ticket-template-pars
nothing to commit, working tree clean
FINDINGS.md
README.md
__pycache__
factory.py
linear_provider.py
strman.py
test_ticket_template.py
ticketTemplate.md
ticket_template.py
[0m
[0m→ [0mRead ticket_template.py
[0m→ [0mRead ticketTemplate.md
[0m→ [0mRead README.md
[0m→ [0mRead test_ticket_template.py
[0m→ [0mRead FINDINGS.md
[0m
[0m$ [0mpython -m unittest discover tests 2>&1; echo "EXIT: $?"; python3 -m unittest test_ticket_template 2>&1; echo "EXIT: $?"
Traceback (most recent call last):
  File "<frozen runpy>", line 198, in _run_module_as_main
  File "<frozen runpy>", line 88, in _run_code
  File "/home/tiger/.local/share/uv/python/cpython-3.11.15-linux-x86_64-gnu/lib/python3.11/unittest/__main__.py", line 18, in <module>
    main(module=None)
  File "/home/tiger/.local/share/uv/python/cpython-3.11.15-linux-x86_64-gnu/lib/python3.11/unittest/main.py", line 101, in __init__
    self.parseArgs(argv)
  File "/home/tiger/.local/share/uv/python/cpython-3.11.15-linux-x86_64-gnu/lib/python3.11/unittest/main.py", line 127, in parseArgs
    self._do_discovery(argv[2:])
  File "/home/tiger/.local/share/uv/python/cpython-3.11.15-linux-x86_64-gnu/lib/python3.11/unittest/main.py", line 247, in _do_discovery
    self.createTests(from_discovery=True, Loader=Loader)
  File "/home/tiger/.local/share/uv/python/cpython-3.11.15-linux-x86_64-gnu/lib/python3.11/unittest/main.py", line 157, in createTests
    self.test = loader.discover(self.start, self.pattern, self.top)
                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/tiger/.local/share/uv/python/cpython-3.11.15-linux-x86_64-gnu/lib/python3.11/unittest/loader.py", line 332, in discover
    raise ImportError('Start directory is not importable: %r' % start_dir)
ImportError: Start directory is not importable: 'tests'
EXIT: 1
...............................
----------------------------------------------------------------------
Ran 31 tests in 0.083s

OK
EXIT: 0
[0m
[0m$ [0mgit log -1 --format='%B' a4cf6dd && git log -1 --format='%B' e89b201 | head -20 && cat .gitignore 2>/dev/null; git check-ignore __pycache__ && echo IGNORED
ticket_template: parser/validator module for ticketTemplate.md tickets

parse(text) -> Ticket fields; validate(ticket) -> template violations
(sections in order, >=1 '- [ ]' criterion, relative-path-only verify
commands inside the fence, 'Estimate: N min · Depends on:' line,
open questions exactly '- None', unfilled placeholders rejected).
CLI exits 0 iff valid; stdlib unittest suite covers parse, validate,
the real template file, and the CLI.

README: rewrite for the factory era (Linear tickets, worktrees, verify gates)

.env
__pycache__/
*.pyc
docs/v2/
__pycache__
IGNORED
[0m
[0m✱ [0mGrep "verify|tests|unittest"[90m in . · 49 matches[0m
[0m
[0m$ [0mpython3 - <<'EOF'
from pathlib import Path
import ticket_template as tt
tpl = Path("ticketTemplate.md").read_text()
# simulate a template-derived filled ticket that keeps the blockquote
filled = tpl.replace('{{TITLE}}', 'T').replace('<Describe the outcome in one or two sentences.>', 'S')
t = tt.parse(filled)
print(repr(t.depends_on))
print([p for p in tt.validate(t) if 'Depends on' in p][:2])
EOF
None
['\'Estimate & dependencies\' must read \'Estimate: N min · Depends on: <ticket IDs or "none">\'']
[0m
[0m$ [0mmkdir -p tests && git mv test_ticket_template.py tests/test_ticket_template.py && touch tests/__init__.py && ls tests && python3 - <<'EOF'
import ticket_template as tt
FILLED = open('tests/test_ticket_template.py').read()
# quick garble check on a filled ticket that keeps the template's blockquote
sec = "## Estimate & dependencies\n\nEstimate: 30 min · Depends on: none\n\n> Machine-checkable dependencies are Linear **blocks** relations — the loop\n> enforces only those.\n"
t = tt.parse("## Estimate & dependencies\n\nEstimate: 30 min · Depends on: none\n\n> quote line one\n> quote line two, more\n")
print(repr(t.depends_on))
EOF
__init__.py
test_ticket_template.py
['none > quote line one > quote line two', 'more']
[0m
[0m→ [0mRead tests/test_ticket_template.py[90m [limit=15][0m
[0m
[0m← [0mEdit tests/test_ticket_template.py
Index: /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/tests/test_ticket_template.py
===================================================================
--- /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/tests/test_ticket_template.py
+++ /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/tests/test_ticket_template.py
@@ -1,17 +1,19 @@
 """Tests for ticket_template: parse + validate ticketTemplate.md tickets.
 
-Run: python3 -m unittest test_ticket_template -v
+Run: python3 -m unittest discover tests -v
 """
 import subprocess
 import sys
 import tempfile
 import unittest
 from pathlib import Path
 
+sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
+
 import ticket_template as tt
 
-HERE = Path(__file__).parent
+ROOT = Path(__file__).resolve().parent.parent
 
 FILLED = """\
 # Add export endpoint
 

[0m
[0m← [0mEdit tests/test_ticket_template.py
Index: /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/tests/test_ticket_template.py
===================================================================
--- /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/tests/test_ticket_template.py
+++ /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/tests/test_ticket_template.py
@@ -60,9 +60,20 @@
 
 - None
 """
 
+BLOCKQUOTE = """\
+> Machine-checkable dependencies are Linear **blocks** relations — the loop
+> enforces only those. Anything outside Linear (DNS, human review, hardware)
+> gates via triage: the ticket stays in Backlog until resolved, then moves to
+> Todo. Keep this line in sync with the relations for human readers.
+"""
 
+FILLED_WITH_BLOCKQUOTE = FILLED.replace(
+    "Estimate: 30 min · Depends on: none\n",
+    "Estimate: 30 min · Depends on: none\n\n" + BLOCKQUOTE)
+
+
 class ParseTests(unittest.TestCase):
     def setUp(self):
         self.t = tt.parse(FILLED)
 

[0m
[0m← [0mEdit tests/test_ticket_template.py
Index: /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/tests/test_ticket_template.py
===================================================================
--- /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/tests/test_ticket_template.py
+++ /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/tests/test_ticket_template.py
@@ -113,9 +113,19 @@
         t = tt.parse(FILLED.replace(
             "Depends on: none", "Depends on: KO-1, KO-23"))
         self.assertEqual(t.depends_on, ["KO-1", "KO-23"])
 
+    def test_estimate_line_ignores_trailing_blockquote(self):
+        t = tt.parse(FILLED_WITH_BLOCKQUOTE)
+        self.assertEqual(t.estimate_min, 30)
+        self.assertEqual(t.depends_on, [])
 
+    def test_depends_on_ids_split_with_blockquote(self):
+        t = tt.parse(FILLED_WITH_BLOCKQUOTE.replace(
+            "Depends on: none", "Depends on: KO-1, KO-23"))
+        self.assertEqual(t.depends_on, ["KO-1", "KO-23"])
+
+
 class ValidateTests(unittest.TestCase):
     def assert_problems_contain(self, text, fragment):
         problems = tt.validate(tt.parse(text))
         self.assertTrue(any(fragment in p for p in problems),

[0m
[0m← [0mEdit tests/test_ticket_template.py
Index: /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/tests/test_ticket_template.py
===================================================================
--- /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/tests/test_ticket_template.py
+++ /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/tests/test_ticket_template.py
@@ -133,8 +133,11 @@
 
 def test_filled_ticket_is_valid(self):
     self.assertEqual(tt.validate(tt.parse(FILLED)), [])
 
+def test_filled_ticket_with_blockquote_is_valid(self):
+    self.assertEqual(tt.validate(tt.parse(FILLED_WITH_BLOCKQUOTE)), [])
+
 def test_missing_section(self):
     self.assert_problems_contain(
         FILLED.replace("## Out of scope\n\n- Excel-specific formatting\n", ""),
         "missing section '## Out of scope'")

[0m
[0m← [0mEdit tests/test_ticket_template.py
Index: /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/tests/test_ticket_template.py
===================================================================
--- /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/tests/test_ticket_template.py
+++ /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/tests/test_ticket_template.py
@@ -216,9 +216,9 @@
 class TemplateFileTests(unittest.TestCase):
     """The real ticketTemplate.md must parse; unfilled, it must NOT validate."""
 
     def setUp(self):
-        self.text = (HERE / "ticketTemplate.md").read_text()
+        self.text = (ROOT / "ticketTemplate.md").read_text()
         self.t = tt.parse(self.text)
 
     def test_all_sections_recognized_in_order(self):
         self.assertEqual([n for n in self.t.order], tt.SECTION_ORDER)

[0m
[0m← [0mEdit tests/test_ticket_template.py
Index: /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/tests/test_ticket_template.py
===================================================================
--- /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/tests/test_ticket_template.py
+++ /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/tests/test_ticket_template.py
@@ -236,9 +236,9 @@
 
 class CliTests(unittest.TestCase):
     def run_cli(self, *paths):
         return subprocess.run(
-            [sys.executable, str(HERE / "ticket_template.py"), *paths],
+            [sys.executable, str(ROOT / "ticket_template.py"), *paths],
             capture_output=True, text=True)
 
     def test_valid_file_exits_zero(self):
         with tempfile.NamedTemporaryFile("w", suffix=".md") as f:

[0m
[0m← [0mEdit tests/test_ticket_template.py
Index: /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/tests/test_ticket_template.py
===================================================================
--- /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/tests/test_ticket_template.py
+++ /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/tests/test_ticket_template.py
@@ -257,9 +257,9 @@
     self.assertIn("INVALID", r.stdout)
     self.assertIn("missing section", r.stdout)
 
 def test_real_template_file_exits_nonzero(self):
-    r = self.run_cli(str(HERE / "ticketTemplate.md"))
+    r = self.run_cli(str(ROOT / "ticketTemplate.md"))
     self.assertNotEqual(r.returncode, 0)
 
 def test_no_args_usage_exit_two(self):
     self.assertEqual(self.run_cli().returncode, 2)

[0m
[0m← [0mEdit ticket_template.py
Index: /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/ticket_template.py
===================================================================
--- /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/ticket_template.py
+++ /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/ticket_template.py
@@ -139,9 +139,14 @@
 t.acceptance = _checkboxes(ac, UNCHECKED_RE)
 t.acceptance_done = _checkboxes(ac, CHECKED_RE)
 t.verify_commands = _verify_commands(t.sections.get("Verify command(s)", ""))
 t.notes = _bullets(t.sections.get("Implementation notes", ""))
-est = ESTIMATE_RE.match(_clean(t.sections.get("Estimate & dependencies", "")))
+est = None
+for ln in t.sections.get("Estimate & dependencies", "").splitlines():
+    m = ESTIMATE_RE.match(ln.strip())
+    if m:
+        est = m
+        break
 t.estimate_min = int(est.group(1)) if est else None
 t.depends_on = _deps(est.group(2)) if est else None
 oq = COMMENT_RE.sub("", t.sections.get("Open questions", ""))
 t.open_questions_none = _clean(oq) == "- None"

[0m
[0m← [0mEdit README.md
Index: /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/README.md
===================================================================
--- /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/README.md
+++ /srv/dev/holophyte.worktrees/holophyte-ticket-template-pars/README.md
@@ -36,10 +36,10 @@
   estimate is the budget in minutes.
 - `ticket_template.py` — parser/validator for that shape;
   `python3 ticket_template.py TICKET.md [...]` exits 0 iff the ticket is
   pickable-ready.
-- `test_ticket_template.py` — stdlib unittest suite for it
-  (`python3 -m unittest test_ticket_template`).
+- `tests/test_ticket_template.py` — stdlib unittest suite for it
+  (`python3 -m unittest discover tests`).
 - `strman.py` — small string utilities.
 - `FINDINGS.md` (generated) — append-only review/merge ledger, mirrored
   to Linear ticket comments.
 

[0m
[0m$ [0mrm -rf __pycache__ tests/__pycache__ && python -m unittest discover tests 2>&1; echo "GATE EXIT: $?"; python3 -m unittest discover tests -v 2>&1 | tail -8; git status --short
..................................
----------------------------------------------------------------------
Ran 34 tests in 0.083s

OK
GATE EXIT: 0
test_relative_flag_argument_is_not_flagged (test_ticket_template.ValidateTests.test_relative_flag_argument_is_not_flagged) ... ok
test_unfilled_placeholders_fail (test_ticket_template.ValidateTests.test_unfilled_placeholders_fail) ... ok
test_unknown_section (test_ticket_template.ValidateTests.test_unknown_section) ... ok

----------------------------------------------------------------------
Ran 34 tests in 0.077s

OK
 M README.md
RM test_ticket_template.py -> tests/test_ticket_template.py
 M ticket_template.py
?? tests/__init__.py
[0m
[0m$ [0mpython3 - <<'EOF'
import re
from pathlib import Path
import ticket_template as tt
text = Path("ticketTemplate.md").read_text()
# minimal fill-in that keeps the template's blockquote verbatim
text = re.sub(r"\{\{[^{}]*\}\}|<[^<>\n][^<>\n]*>", lambda m: {
    "{{TITLE}}": "T", "<Describe the outcome in one or two sentences.>": "S"}.get(m.group(0), "X"), text)
t = tt.parse(text)
print("estimate:", t.estimate_min, "| depends_on:", t.depends_on)
probs = [p for p in tt.validate(t) if "Depends on" in p or "'Estimate & dependencies'" in p]
print("estimate/deps problems:", probs)
EOF
rg -n "HERE|unittest test_ticket_template\b" --glob '!*.md' ; rg -n "unittest" README.md
estimate: None | depends_on: None
estimate/deps problems: ['\'Estimate & dependencies\' must read \'Estimate: N min · Depends on: <ticket IDs or "none">\'']
linear_provider.py:15:HERE = Path(__file__).parent
linear_provider.py:23:    env_file = HERE / ".env"
40:- `tests/test_ticket_template.py` — stdlib unittest suite for it
41:  (`python3 -m unittest discover tests`).
[0m
[0m$ [0mpython3 - <<'EOF'
from pathlib import Path
import ticket_template as tt
text = Path("ticketTemplate.md").read_text()
for old, new in {"{{TITLE}}": "T", "Estimate: N min": "Estimate: 30 min",
                 '<Describe the outcome in one or two sentences.>': "S"}.items():
    text = text.replace(old, new)
import re
text = re.sub(r"<[^<>\n][^<>\n]*>", "X", text)
t = tt.parse(text)
print("estimate:", t.estimate_min, "| depends_on:", t.depends_on)
print("estimate/deps problems:", [p for p in tt.validate(t) if "Depends on" in p or "Estimate & dependencies" in p])
EOF
python3 -m compileall -q ticket_template.py tests && echo COMPILE-OK
estimate: 30 | depends_on: ['X']
estimate/deps problems: ['\'Depends on\' entry is not a ticket ID or "none": X']
COMPILE-OK
[0m
[0m$ [0mgit add README.md ticket_template.py tests/ && git commit -m "$(cat <<'EOF'
ticket_template: address review findings (tests/ layout, estimate blockquote)

Blocker 1 ADDRESSed: the task's verify gate runs `python -m unittest
discover tests`, but the suite sat at the repo root, so discovery
aborted at import and the mechanical gate could never pass. Move it to
tests/ (with __init__.py so discovery can import it) and update the
README + module docstring run lines to `python3 -m unittest discover
tests`; layout, docs, and gate now agree. The suite bootstraps sys.path
so `import ticket_template` keeps working from any cwd.

Blocker 2 ADDRESSed: parse() collapsed the whole 'Estimate &
dependencies' section before matching ESTIMATE_RE, so the template's own
explanatory blockquote glued onto the Depends-on value ('none >
Machine-checkable dependencies ...') and any template-derived ticket
failed validation with garbled dependency errors. Match ESTIMATE_RE
per-line instead; new tests cover a FILLED variant that retains the
blockquote for both parse fields and full validation.

FOLLOW_UPs (valid, out of scope here): validate() never enforces section
ORDER despite the docstring's "required sections in order" claim;
main() raises a raw traceback on a nonexistent TICKET.md path instead of
a usage-level error.
EOF
)" && git log --oneline -2 && git show --stat HEAD | tail -8
[task/holophyte-ticket-template-pars 97f1a98] ticket_template: address review findings (tests/ layout, estimate blockquote)
 4 files changed, 39 insertions(+), 8 deletions(-)
 create mode 100644 tests/__init__.py
 rename test_ticket_template.py => tests/test_ticket_template.py (85%)
97f1a98 ticket_template: address review findings (tests/ layout, estimate blockquote)
a4cf6dd ticket_template: parser/validator module for ticketTemplate.md tickets
    main() raises a raw traceback on a nonexistent TICKET.md path instead of
    a usage-level error.

 README.md                                          |  4 +--
 tests/__init__.py                                  |  0
 .../test_ticket_template.py                        | 36 +++++++++++++++++++---
 ticket_template.py                                 |  7 ++++-
 4 files changed, 39 insertions(+), 8 deletions(-)
[0m
[0m$ [0mgit status --short; python -m unittest discover tests 2>&1 | tail -3; echo "GATE EXIT: $?"
Ran 34 tests in 0.082s

OK
GATE EXIT: 0
[0m

## 2026-08-22T21:40:47Z — KO-107
FAILED after 2 review rounds; branch task/holophyte-ticket-template-pars preserved at 97f1a98bec1d06a6cee985e248ab222c1203c0ec

Last reviewer verdict:
Reviewed `ticket_template.py` (271 lines) and `tests/test_ticket_template.py` (269 lines) against the diff-vs-main scope. Note: I have no shell in this session, so my findings come from reading the code, not from executing it.

## Blockers

**1. The mechanical verification is vacuous — `Ran 0 tests ... OK`.**
The commit ships four test classes (~30 test methods) in `tests/test_ticket_template.py`, and the module's own docstring says to run `python3 -m unittest discover tests -v` (tests/test_ticket_template.py:3). The gate that "PASSED" collected **zero** tests, so nothing in this commit was actually verified — the ticket's verify command does not exercise the code it was written for (wrong start dir, wrong `-p` pattern, or a stale path from before commit 97f1a98 moved tests under `tests/`). In a repo whose premise is mechanical verify gates before review and merge, an all-green run that executes no tests is worse than a red one. Fix the ticket's verify command so it actually discovers `tests/test_ticket_template.py`, and re-run it.

**2. `validate()` does not check section order, but the module contract says it does.**
`ticket_template.py:6` promises "required sections in order". `validate()` (ticket_template.py:196-203) only counts presence, duplicates, and unknown names via `t.order.count(...)` and a set difference — a ticket with `## Open questions` before `## Summary` validates clean. `test_sections_in_template_order` (tests/test_ticket_template.py:83) only asserts parse-order on an already-ordered document, so it doesn't catch this. Either implement the order check (`t.order == SECTION_ORDER` modulo dupes) or drop "in order" from the docstring — as written, a caller relying on the stated contract gets silent false passes.

## Non-blocking, worth a follow-up

- **H1 detection ignores code fences** (ticket_template.py:108-114). A verify fence containing a shell comment such as `# run the suite` is counted in `stray_h1s` → spurious `"1 extra H1 heading(s)"`, and `_verify_commands` also emits it as a command. Plausible for real tickets.
- **Quoted absolute paths escape the relative-path rule** (ticket_template.py:179): `re.sub(r'"[^"]*"', '""', cmd)` blanks quoted spans before the scan, so `pytest "/srv/repo/tests"` passes while the unquoted form is correctly rejected.
- **`test_open_questions_none_with_trailing_comment`** (tests/test_ticket_template.py:109) is misnamed — `FILLED` has no trailing `<!-- -->` comment; the comment path is only covered indirectly by `TemplateFileTests`.

VERDICT: REQUEST_CHANGES

## 2026-08-22T23:55:00Z — KO-107 (manual close-out)
FAILED in loop after 2 review rounds (verify pattern mismatch: ticket said
test_ticket_validator*, file was test_ticket_template.py — vacuous 'Ran 0
tests OK'; plus validate() not enforcing the documented section order).
Human re-round at preserved branch 97f1a98: fixed verify command on KO-107,
implemented section-order check + 2 tests, 36/36 green, merged 7bcd707.
Blocker lesson for v2: verify commands must print diagnostics on failure —
compound && chains with silent greps give fixers nothing to act on; and a
green gate that runs zero tests must be treated as RED, not pass.

## 2026-08-28T21:41:58Z — KO-109
Round 1: REQUEST_CHANGES -> fix round
Reviewer findings:
- [factory.py:81](/workspace/factory.py:81) treats `&&` inside shell comments as an operator. `true # && false` is changed from success to failure, so verification semantics are not preserved.
- [factory.py:105](/workspace/factory.py:105) cannot diagnose a clause containing shell `exit`: `echo before && exit 7 && echo never` reports only the whole command, not clause 2 as required.
- The supplied verification discovered zero tests, which the repository policy explicitly treats as failure. The candidate’s four focused tests were not exercised by that gate.

VERDICT: REQUEST_CHANGES

Implementer response:
All three findings adjudicated **ADDRESS** and fixed in `95f8bfd`.

**1. `&&` inside a shell comment — ADDRESS.** Confirmed: `true # && false` split into `true #` / `false` and turned a passing command into a failure. `split_and_clauses` ([factory.py:78](factory.py#L78)) now stops scanning at an unquoted `#` in word position and keeps the remainder with the current clause; it bails to verbatim execution if a newline follows the comment or the comment opens inside `(...)`, where the end-of-line rule would swallow the closer.

**2. Clause containing shell `exit` — ADDRESS.** Confirmed: the `|| { ...; }` failure handler was never reached when the clause exited the shell itself, so `echo before && exit 7 && echo never` fell back to a whole-command report. `instrumented_script` now keeps a clause counter and reports from an EXIT trap, which fires on every exit path; each clause is just `{ ... } || exit $?`. Checked under `/bin/sh` (dash) that the trap preserves the original exit status. It now reports `clause 2 of 3 exited 7`.

**3. Zero-test discovery — ADDRESS.** This one was my error. KO-109 names `tests/test_verify_gate.py` and verifies with `-p 'test_verify_gate*'`; I committed the suite as `tests/test_factory_verify.py`, so the gate collected zero tests and reported green — the KO-107 lesson repeating. Renamed to the file the ticket names. Running the real gate then exposed an unmet acceptance criterion ("reports clause 1, clause 2, exit code 7, and the first clause output"): the report showed only the failing clause's output. `failure_report` now lists every executed clause with its output and names the short-circuited clauses as not executed.

Making `run_verify` itself treat a zero-test "OK" as RED is KO-114 (verify gate 2/3), which KO-109's Out of scope section excludes — noted in the commit message rather than implemented here.

Verification: the ticket's exact command `python3 -m unittest discover -s tests -p 'test_verify_gate*' -v` → 8 tests, exit 0; full suite 49 tests, OK. Four of the eight tests are RED against the previous commit (comment splitting, `exit` attribution, both report-shape criteria) — I ran them against `HEAD~1`'s `factory.py` to confirm.

## 2026-08-28T21:43:02Z — KO-109
FAILED after 2 review rounds; branch task/factory-verify-gate-1-3-fail-l preserved at 95f8bfddf81ce05f561cf5f5dd328db3a1fa12f6

Last reviewer verdict:
- [factory.py](/workspace/factory.py:120): Clause markers are not guaranteed to start on a new line. For `printf first && sh -c 'echo boom; exit 7' && printf never`, the report assigns `boom` to clause 1 and labels the failing clause 2 as silent. This violates the requirement to show each executed clause’s output accurately.

VERDICT: REQUEST_CHANGES

## 2026-08-28T22:20:00Z — KO-109 (manual close-out)
FAILED in loop after 2 review rounds. Round-1 findings (comment-`&&`, `exit`
attribution, wrong test filename -> vacuous gate) all fixed by implementer in
95f8bfd. Round-2 blocker: clause markers not newline-anchored — `printf first`
glued the next marker onto its output, misattributing the failing clause's
text to the previous clause.
Human re-round at preserved branch: parse_clause_output now splits markers
off mid-line, keeping the glued prefix with the clause that printed it.
RED->GREEN test on the reviewer's exact reproduction. Focused gate 9/9, full
suite 50/50. Merged dfc4ca8; worktree and branch removed.
Lesson (recurred from KO-107): implementer named the test file
test_factory_verify.py against the ticket's test_verify_gate* pattern —
claim-time filename/pattern cross-check belongs in v2's emitter contract.

## 2026-08-28T21:55:09Z — KO-114
MERGED to main (branch task/factory-verify-gate-2-3-reject deleted). Verify: passed. Rounds used: 1.

## 2026-08-28T22:05:38Z — KO-115
Round 1: REQUEST_CHANGES -> fix round
Reviewer findings:
- [factory.py:242](/workspace/factory.py:242) logs up to 2,000 characters of the checked file on mismatch and forwards that output to the reviewer. Configuration files may contain credentials, violating the repository rule never to log secrets. Report only the path and missing literal.

VERDICT: REQUEST_CHANGES

Implementer response:
Adjudication and fix are done — one finding, committed as `2bc20e1`.

**Finding: `factory.py:242` logs up to 2,000 chars of the checked file → ADDRESS.**

Valid on two counts. AGENTS.md:48 says "never commit or log secrets," and a contract declaration can legitimately point at a config file holding credentials — the drift report is forwarded to the reviewer, so that dump exfiltrates it. It also overshot the documented contract: both `ticketTemplate.md` and README:47 promise a failure "naming the path and the literal," nothing more.

What changed:
- `contract_report` now returns only the problem, relative path, and expected literal. The `text` variable is gone; the file is read solely for the substring test.
- Docstring records *why* the contents are never echoed, so it isn't re-added.
- New test `test_drift_report_does_not_echo_the_checked_file` plants a credential in a drifted file and asserts it never reaches the report. Verified RED against `ab44b63` (it printed `token: hunter2-do-not-log` in the report), green after.
- The existing drift test keeps its path/literal assertions; its `assertIn("8000", out)` line depended on the dump and was dropped.

Diagnostic cost: the report no longer shows what the value drifted *to*. That's the trade the reviewer asked for, and it's cheap — the failure names the file, and whoever reads the report has the checkout.

I left the sibling `vacuous_report` untouched: it quotes command output, which AGENTS.md:40-42 explicitly requires be made visible, and that's a different source than an arbitrary declared file.

Tests: verify gate 21/21, full suite 72/72. Nothing deferred or declined.

## 2026-08-28T22:06:51Z — KO-115
MERGED to main (branch task/factory-verify-gate-3-3-explic deleted). Verify: passed. Rounds used: 2.

## 2026-08-28T22:23:35Z — KO-116
MERGED to main (branch task/factory-loop-fix-round-for-rev deleted). Verify: passed. Rounds used: 1.

## 2026-08-28T22:34:02Z — KO-124
Round 1: REQUEST_CHANGES -> fix round
Reviewer findings:
- [ticket_template.py](/workspace/ticket_template.py:197): Acceptance criteria using plain or numbered bullets are neither counted nor rejected. Five checkbox criteria plus additional criteria validate successfully, bypassing the mechanical five-criterion cap. Count all criterion list items or reject unrecognized entries.

VERDICT: REQUEST_CHANGES

Implementer response:
Adjudication: **ADDRESS** — the one finding was a real bypass of the cap this branch introduced. Reproduced it first: 5 checkboxes + 3 non-checkbox entries → 8 criteria, `validate()` returned `[]`.

**Fix** (`ticket_template.py:57`, `:85`, `:348`):
- `LIST_ITEM_RE` matches any list entry, bulleted (`-`/`*`) or numbered (`1.`/`1)`).
- `_criteria()` classifies every entry in the section into `acceptance` / `acceptance_done` / `acceptance_other`, replacing `_checkboxes()` (which had no other caller).
- `validate()` does both remedies the finding offered: all three lists count toward `MAX_CRITERIA`, and each non-checkbox entry is rejected by name. Rejecting alone would have sufficed to close the bypass, but counting keeps the cap message honest about how many criteria the ticket actually carries — and a silent drop loses a criterion the implementer was meant to satisfy.

Boundary behavior is unchanged: 5 checkboxes still valid; a section of only plain bullets still reports `has no '- [ ] Given/when/then' items`.

**Verification** — 3 new tests (plain bullet over cap, numbered entries over cap, single non-checkbox entry rejected under the cap). Verified RED against the parent commit: 3 failures, the other 57 pass. Post-fix: `test_ticket_template*` 60/60, full suite 91/91.

No FOLLOW_UP or DECLINE items — the review left a single finding.

## 2026-08-28T22:37:25Z — KO-124
Round 2: REQUEST_CHANGES -> fix round
Reviewer findings:
- [ticket_template.py](/workspace/ticket_template.py:53): Scope caps remain bypassable. Standard Markdown `+` bullets are ignored in both sections, and numbered `In scope` entries are ignored. Five checkbox criteria plus a sixth `+` criterion, or three in-scope bullets plus a fourth `+`/numbered item, validate with no blockers.

VERDICT: REQUEST_CHANGES

Implementer response:
Adjudication and fix are done.

**Finding — scope caps bypassable via `+` bullets / numbered In-scope entries: ADDRESS.** I reproduced all three bypasses against the pre-fix code first; each validated with zero blockers.

Root cause was two separate gaps:
- `BULLET_RE`/`UNCHECKED_RE`/`CHECKED_RE`/`LIST_ITEM_RE` only matched `-` and `*`, so a `+` item was invisible to both caps.
- `In scope` (and `Out of scope`, `Implementation notes`) parsed through `_bullets()`, which never matched numbered items — the looser `LIST_ITEM_RE` was only wired into acceptance criteria.

Fix (`ticket_template.py:53`): a single `BULLET = r"[-*+]"` class feeds the checkbox regexes and `LIST_ITEM_RE`, and `_bullets()` became `_list_items()` matching `LIST_ITEM_RE`, so every list section reads the full list shape. Also added `"+ None"` to `OPEN_QUESTIONS_NONE` so the marker set stays uniform, and changed the cap wording from "bullets" to "entries" in the messages and the `ticketTemplate.md` scope-cap comment, since numbered items now count.

Verification — the three reported bypasses now block:
- `+ [ ]`/`+` sixth criterion → `'Acceptance criteria' has 6 items; the cap is 5`
- `+ Scope item 4` → `'In scope' has 4 entries; the cap is 3`
- `4. Scope item 4` → same blocker

New tests cover each marker (`*`, `+`, `1.`, `4)`) for both caps; a ticket written entirely in `+` bullets still validates clean. Full suite: 95 tests, all passing. Committed as `9c3cb55`.

## 2026-08-28T22:38:16Z — KO-124
Terminal adjudication after 2 review rounds: PASS

Adjudicator reply:
Candidate mechanically enforces all three scope caps, handles list-marker bypasses, and passes the specified 64-test verification.

VERDICT: PASS

## 2026-08-28T22:38:18Z — KO-124
MERGED to main (branch task/ticket-template-mechanical-sco deleted). Verify: passed. Rounds used: 2.

## 2026-08-28T22:41:28Z — KO-125
MERGED to main (branch task/factory-record-actual-ticket-d deleted). Verify: passed. Rounds used: 1.

## 2026-08-28T22:47:31Z — KO-117
MERGED to main (branch task/store-v2-1-7-schema-ddl-wal-bo deleted). Verify: passed.
actual: 5.5 min · estimate: 30 min · rounds: 1

## 2026-08-28T22:52:02Z — KO-118
MERGED to main (branch task/store-v2-2-7-claim-lease-trans deleted). Verify: passed.
actual: 4.5 min · estimate: 25 min · rounds: 1

## 2026-08-28T22:57:28Z — KO-119
Round 1: REQUEST_CHANGES -> fix round
Reviewer findings:
- `store.py:345`: `conn.commit()` is outside the rollback guard. A deferred constraint failure at commit leaves the transaction open and the delivery ID uncommitted but reserved; retrying on the same connection fails with `OperationalError: cannot start a transaction within a transaction`. Wrap commit failures with rollback so retries remain possible.

VERDICT: REQUEST_CHANGES

Implementer response:
Done — one finding, adjudicated **ADDRESS**, fixed and committed as `0cdfe02`.

**The finding was real, and I reproduced it exactly.** `tickets.activeRunId` is `DEFERRABLE INITIALLY DEFERRED` with `PRAGMA foreign_keys = ON`, so an effect writing a dangling run reference passes its `INSERT` and only fails at `COMMIT` — where SQLite leaves the transaction open. The RED test failed with the reviewer's precise error:

```
sqlite3.OperationalError: cannot start a transaction within a transaction
```

at `store.py:329` on the retry's `BEGIN IMMEDIATE`, with the delivery id reserved but uncommitted.

**Fix** (`store.py:341`): moved `conn.commit()` inside the existing `except BaseException: conn.rollback(); raise` guard, so a failed commit takes the same path as a failed effect — everything rolls back including the id, the exception propagates, and the connection stays usable for Linear's redelivery. Docstring bullet updated to cover it.

**Test** (`tests/test_store_delivery.py:137`): effect inserts a ticket with `activeRunId = 999999`; asserts `IntegrityError`, nothing committed, and that a retry of the same delivery id then processes and commits normally. `test_store*` 15/15, full suite 111/111.

**FOLLOW_UP named in the commit message:** `claim()` at `store.py:282` has the identical shape and is reachable the same way through the deferred `projects.activeRunId` FK it writes. It shipped in KO-118 with its own tests, so I left it for its own ticket rather than an unreviewed drive-by fix here — say the word if you'd rather I fold it in.

One thing I did not do: AGENTS.md asks for findings and adjudications in `FINDINGS.md` and the Linear ticket ledger. `FINDINGS.md` reads as factory-appended (timestamped `Reviewer findings:` / `Implementer response:` blocks), so I left both to the runner rather than hand-editing shared append-only evidence or posting to Linear myself. The adjudication text above is ready to record if you want me to write either one.

## 2026-08-28T22:58:25Z — KO-119
MERGED to main (branch task/store-v2-3-7-webhook-delivery deleted). Verify: passed.
actual: 6.4 min · estimate: 20 min · rounds: 2

## 2026-08-28T23:07:39Z — KO-120
Round 1: REQUEST_CHANGES -> fix round
Reviewer findings:
- [store.py:436](/workspace/store.py:436) and [store.py:503](/workspace/store.py:503): `transition()` and `mirror_ticket()` always open and commit their own transactions, so they cannot run as effects of `with_delivery()`, which already owns the transaction. Reproduced `with_delivery(..., lambda c: mirror_ticket(c, ...))` failing with `OperationalError: cannot start a transaction within a transaction`; consequently, ticket mirroring cannot be atomically recorded with its Linear delivery as required.

VERDICT: REQUEST_CHANGES

Implementer response:
Committed as `56dd288`.

## Adjudication: ADDRESS

The finding was real and reproduced exactly. Both `transition()` and `mirror_ticket()` opened their own `BEGIN IMMEDIATE`, so passing either as a `with_delivery()` effect raised `OperationalError: cannot start a transaction within a transaction`.

I treated it as in scope rather than a follow-up: mirroring a ticket and moving its status *are* what an inbound Linear delivery does, so writers that structurally can't be delivery effects defeat state-model §1 for its primary case. The fix stays inside `store.py`, the file this ticket owns, and adds no public API.

**Fix** — new `_transaction(conn)` context manager (`store.py:214`). It takes `BEGIN IMMEDIATE` and commits/rolls back only when it owns the transaction; when one is already open it joins and leaves both boundaries to the owner. `transition()` and `mirror_ticket()` now use it and are otherwise unchanged — same validation, same rollback-on-failure, same return values, same up-front serialization standalone.

**Tests** — three in `tests/test_store_status.py`, all RED against the parent commit with the reported error:
- a mirror and its delivery id commit as one
- a transition and its delivery id commit as one
- an effect that mirrors and *then* fails leaves neither the row nor the delivery id

Mutation-checked: making the joined block commit at its own boundary fails the third test; dropping the join restores all three original errors. My first version of the rollback test survived the commit-anyway mutation — it asserted only that the id was released, which the pre-existing `with_delivery` guard already did — so I replaced it with the write-then-fail case that actually pins atomicity.

`test_store*` 29/29 (+3), full suite 125/125.

**FOLLOW_UP, named in the commit message:** `claim()` (`store.py:280`) has the identical shape and is equally uncomposable. It shipped in KO-118 with its own tests and wasn't flagged, so I left it rather than make an unreviewed drive-by change to another ticket's code — `_transaction()` is in place for whoever takes it. Say the word if you'd rather I fold it in now.

I did not hand-edit `FINDINGS.md`; following the KO-119 precedent, it reads as factory-appended evidence. The adjudication text above is ready to record there or in the Linear ledger if you want me to.

## 2026-08-28T23:10:43Z — KO-120
Round 2: REQUEST_CHANGES -> fix round
Reviewer findings:
- `store.py:247`: `_transaction()` commits outside its exception guard. A deferred constraint failure during `commit()` leaves the transaction open with ticket writes pending, so retries can join contaminated state instead of starting cleanly. Move `commit()` inside the rollback guard.

VERDICT: REQUEST_CHANGES

Implementer response:
Committed as `fd8d4ed`.

**Adjudication**

- **`store.py:247` — ADDRESS.** Valid and reachable. The schema declares its `runs` foreign keys `DEFERRABLE INITIALLY DEFERRED` (`store.py:65,89,91`) with `PRAGMA foreign_keys = ON`, so a dangling reference passes the INSERT and only fails at COMMIT — and SQLite leaves the transaction open on that failure. Since `_transaction()` dispatches on `conn.in_transaction`, the *next* writer on that connection reads True, takes the "join the caller's transaction" path, and commits the rejected writes along with its own. `with_delivery()` already commits inside its guard with a comment naming this exact hazard, so the helper was the inconsistent one.

**Fix:** moved `conn.commit()` inside the `try`, with a comment pointing at the `with_delivery()` precedent, and updated the docstring's rollback claim to cover the commit itself.

**Test:** `test_a_failed_commit_leaves_the_connection_clean_for_the_retry` drives a dangling `activeRunId` through `_transaction()`, then asserts the connection is out of the transaction and a subsequent `mirror_ticket()` commits clean. Verified it fails on the old ordering (`AssertionError: True is not false` on `in_transaction`) and passes now. Full suite: 126 tests, OK.

**Noted as FOLLOW_UP in the commit message, not fixed:** `claim()` (`store.py:321`) hand-rolls the same `BEGIN IMMEDIATE`/try/commit shape with the identical defect. It predates this branch and is untouched by it; the right fix is routing it through `_transaction()`, which changes the claim path and wants its own tests — outside this task's scope. There's no ticket for it yet, so it needs one if you want it picked up.

## 2026-08-28T23:11:31Z — KO-120
Terminal adjudication after 2 review rounds: PASS

Adjudicator reply:
The candidate correctly implements the required status transitions and `needs_spec` routing, with atomic transaction behavior and passing focused verification.

VERDICT: PASS
