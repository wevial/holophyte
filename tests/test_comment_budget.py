"""The comment budget: a ratchet on comment and docstring lines.

Each module the factory runs, under `holophyte/` and `store/` and at the
repository root, has a pinned count of comment and docstring lines in
`PINNED` and of lines citing a ticket id in `CITED`. Neither may grow, a
lowered count lowers its entry in the same commit, a new module holds at
most one such line per 20 and cites no ticket. History belongs in git log
and the store, not in code.

Run: python3 -m unittest discover -s tests -p 'test_comment_budget.py' -v
"""

from __future__ import annotations

import ast
import io
import re
import subprocess
import tempfile
import tokenize
import unittest
from pathlib import Path
from typing import NamedTuple

from tests.test_file_sizes import line_count

ROOT = Path(__file__).resolve().parent.parent

SCOPE = ("holophyte/", "store/")
CITATION = re.compile(r"\b(KO|HOLO|REL|LOTUS|CROTON)-[0-9]+\b")
ALLOWANCE = 20
TARGET = 0.05
TARGET_ENFORCED = False

PINNED = {
    "factory.py": 0,

    "holophyte/__init__.py": 1,
    "holophyte/admission.py": 1,

    "holophyte/agents/__init__.py": 0,
    "holophyte/agents/agent_output.py": 0,
    "holophyte/agents/agent_routes.py": 6,
    "holophyte/agents/agent_turns.py": 1,
    "holophyte/agents/fallback.py": 4,
    "holophyte/agents/fix_session.py": 1,
    "holophyte/agents/harness.py": 10,
    "holophyte/agents/probes.py": 2,
    "holophyte/agents/review_workspace.py": 6,
    "holophyte/agents/roles.py": 8,
    "holophyte/agents/session_arms.py": 0,
    "holophyte/agents/transcript_config.py": 0,
    "holophyte/agents/transcripts.py": 4,

    "holophyte/babysit/__init__.py": 0,
    "holophyte/babysit/babysit_steps.py": 0,
    "holophyte/babysit/babysitter.py": 21,
    "holophyte/babysit/bot_threads.py": 0,
    "holophyte/babysit/check_fix.py": 1,
    "holophyte/babysit/conversation_comments.py": 0,
    "holophyte/babysit/main_checkout.py": 2,
    "holophyte/babysit/maintainer_notes.py": 1,
    "holophyte/babysit/plain_text.py": 0,
    "holophyte/babysit/thread_answers.py": 3,
    "holophyte/babysit/thread_findings.py": 2,
    "holophyte/babysit/thread_mentions.py": 3,
    "holophyte/babysit/thread_text.py": 0,

    "holophyte/board/__init__.py": 0,
    "holophyte/board/board_diff.py": 2,
    "holophyte/board/board_import.py": 2,
    "holophyte/board/board_sync.py": 10,
    "holophyte/board/native_board.py": 4,
    "holophyte/board/projection.py": 15,

    "holophyte/capture_playwright.py": 16,

    "holophyte/cli/__init__.py": 0,
    "holophyte/cli/arguments.py": 0,
    "holophyte/cli/board_verbs.py": 0,
    "holophyte/cli/cli_project.py": 0,
    "holophyte/cli/cli_story.py": 0,
    "holophyte/cli/entry.py": 2,
    "holophyte/cli/host_modes.py": 2,
    "holophyte/cli/operator.py": 4,
    "holophyte/cli/report.py": 1,
    "holophyte/cli/status.py": 4,
    "holophyte/cli/store_import.py": 3,
    "holophyte/cli/store_verbs.py": 0,

    "holophyte/commit_hygiene.py": 4,

    "holophyte/config/__init__.py": 0,
    "holophyte/config/agent_settings.py": 1,
    "holophyte/config/checks.py": 2,
    "holophyte/config/config_tables.py": 4,
    "holophyte/config/locks.py": 1,
    "holophyte/config/project.py": 6,
    "holophyte/config/reader.py": 3,
    "holophyte/config/serve_settings.py": 2,
    "holophyte/config/worktree_settings.py": 1,

    "holophyte/deadline.py": 4,
    "holophyte/environment_git.py": 2,
    "holophyte/failure_reason.py": 0,
    "holophyte/files.py": 8,

    "holophyte/holo/__init__.py": 0,
    "holophyte/holo/__main__.py": 0,
    "holophyte/holo/cli.py": 1,

    "holophyte/host/__init__.py": 0,
    "holophyte/host/ci_wake.py": 0,
    "holophyte/host/reconcile.py": 13,
    "holophyte/host/registry.py": 8,
    "holophyte/host/startup.py": 0,
    "holophyte/host/supervisor.py": 19,
    "holophyte/host/supervisor_lock.py": 9,
    "holophyte/host/sweep_host.py": 10,
    "holophyte/host/sweep_report.py": 3,

    "holophyte/isolation/__init__.py": 0,
    "holophyte/isolation/isolation_clone.py": 5,
    "holophyte/isolation/isolation_git.py": 4,
    "holophyte/isolation/isolation_return.py": 2,
    "holophyte/isolation/launcher.py": 1,

    "holophyte/loop/__init__.py": 0,
    "holophyte/loop/adjudicate.py": 2,
    "holophyte/loop/branch_sync.py": 2,
    "holophyte/loop/claim.py": 12,
    "holophyte/loop/claim_store.py": 3,
    "holophyte/loop/dispatch.py": 6,
    "holophyte/loop/gates.py": 20,
    "holophyte/loop/implement.py": 8,
    "holophyte/loop/merge_gate.py": 10,
    "holophyte/loop/merge_lock.py": 0,
    "holophyte/loop/pause_notice.py": 1,
    "holophyte/loop/pipeline.py": 5,
    "holophyte/loop/pool.py": 6,
    "holophyte/loop/pool_handoff.py": 4,
    "holophyte/loop/reexec.py": 3,
    "holophyte/loop/review_round.py": 5,
    "holophyte/loop/run.py": 1,
    "holophyte/loop/runs.py": 11,
    "holophyte/loop/stop.py": 5,
    "holophyte/loop/task_worktree.py": 5,

    "holophyte/media_store.py": 1,

    "holophyte/pr/__init__.py": 0,
    "holophyte/pr/github.py": 18,
    "holophyte/pr/merge_queue.py": 4,
    "holophyte/pr/missing_checks.py": 2,
    "holophyte/pr/pr_activity.py": 5,
    "holophyte/pr/pr_contexts.py": 0,
    "holophyte/pr/pr_head.py": 2,
    "holophyte/pr/pr_media.py": 13,
    "holophyte/pr/pr_status.py": 13,
    "holophyte/pr/pullrequest.py": 10,

    "holophyte/questions.py": 1,
    "holophyte/redact.py": 14,

    "holophyte/review/__init__.py": 0,
    "holophyte/review/briefs.py": 4,
    "holophyte/review/findings.py": 4,
    "holophyte/review/freshness.py": 2,
    "holophyte/review/reply_parsing.py": 11,
    "holophyte/review/reproduce.py": 1,
    "holophyte/review/skipped_tests.py": 0,
    "holophyte/review/review_session.py": 2,
    "holophyte/review/stale_approval.py": 0,

    "holophyte/serve/__init__.py": 0,
    "holophyte/serve/console_build.py": 2,
    "holophyte/serve/serve_actions.py": 4,
    "holophyte/serve/serve_board.py": 3,
    "holophyte/serve/serve_config.py": 5,
    "holophyte/serve/serve_host.py": 7,
    "holophyte/serve/serve_levers.py": 2,
    "holophyte/serve/serve_runs.py": 9,
    "holophyte/serve/serve_watch.py": 5,
    "holophyte/serve/server.py": 14,
    "holophyte/serve/views.py": 6,

    "holophyte/story/__init__.py": 0,
    "holophyte/story/story_approval.py": 4,
    "holophyte/story/story_claim.py": 5,
    "holophyte/story/story_close.py": 1,
    "holophyte/story/story_drift.py": 6,
    "holophyte/story/story_filing.py": 7,
    "holophyte/story/story_views.py": 1,
    "holophyte/story/witness.py": 1,

    "linear_provider.py": 20,
    "provider.py": 12,
    "review_runner.py": 13,

    "store/__init__.py": 16,
    "store/agent_routes.py": 1,
    "store/board.py": 7,
    "store/ddl.py": 5,
    "store/enums.py": 7,
    "store/failure_kinds.py": 1,
    "store/gap_layers.py": 1,
    "store/instructions.py": 1,
    "store/launch_backoff.py": 2,
    "store/notes.py": 1,
    "store/operate.py": 20,
    "store/operator_notes.py": 3,
    "store/project_paths.py": 1,
    "store/read.py": 7,
    "store/repair.py": 3,
    "store/revisions.py": 1,
    "store/run_reads.py": 8,
    "store/schema.py": 24,
    "store/stories.py": 2,
    "store/tickets.py": 14,
    "store/working.py": 3,
    "store/writes.py": 2,

    "story_template.py": 1,
    "ticket_template.py": 24,
}

CITED = {
}

# "path::function" to the ticket that retires its `noqa: C901`, or None.
COMPLEXITY_EXEMPT = {
    "holophyte/loop/gates.py::split_and_clauses": None,
    "ticket_template.py::validate": None,
}

BODIED = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


class Module(NamedTuple):
    size: int
    count: int
    cited: tuple


def in_scope(root=ROOT):
    names = subprocess.check_output(
        ["git", "ls-files", "*.py"], cwd=root, text=True).splitlines()
    return sorted(n for n in names if n.startswith(SCOPE) or "/" not in n)


def is_docstring(stmt):
    return (isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str))


def comments(text):
    """Line number to comment token, a line-1 shebang left out."""
    found = {}
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type != tokenize.COMMENT:
            continue
        if tok.start[0] == 1 and tok.string.startswith("#!"):
            continue
        found[tok.start[0]] = tok.string
    return found


def counted_lines(text):
    lines = set(comments(text))
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, BODIED) and node.body and is_docstring(node.body[0]):
            first = node.body[0]
            lines.update(range(first.lineno, first.end_lineno + 1))
    return lines


def measure(root=ROOT):
    modules = {}
    for name in in_scope(root):
        path = root / name
        text = path.read_text()
        physical = text.splitlines()
        lines = counted_lines(text)
        cited = tuple(sorted(n for n in lines if CITATION.search(physical[n - 1])))
        modules[name] = Module(line_count(path), len(lines), cited)
    return modules


def budget_violations(modules, pinned=PINNED):
    bad = []
    for name, module in modules.items():
        pin = pinned.get(name)
        if pin is None:
            allowed = module.size // ALLOWANCE
            if module.count > allowed:
                bad.append(f"{name}: {module.count} comment and docstring "
                           f"lines in {module.size}; an unlisted module is "
                           f"allowed {allowed}")
        elif module.count > pin:
            bad.append(f"{name}: {module.count} comment and docstring lines "
                       f"grew past its pin of {pin}")
        elif module.count < pin:
            bad.append(f"{name}: {module.count} comment and docstring lines "
                       f"is under its pin of {pin}; lower the pin")
    for name in sorted(set(pinned) - set(modules)):
        bad.append(f"{name}: not a tracked in-scope file; the PINNED entry "
                   f"is stale")
    return bad


def citation_violations(modules, cited=CITED):
    bad = []
    for name, module in modules.items():
        entry = cited.get(name)
        found = len(module.cited)
        if entry is None:
            bad.extend(f"{name}:{line}: cites a ticket id with no CITED entry"
                       for line in module.cited)
        elif entry == 0:
            bad.append(f"{name}: a CITED entry of 0 is stale")
        elif found > entry:
            bad.append(f"{name}: {found} lines cite a ticket id, grew past "
                       f"its CITED entry of {entry}")
        elif found < entry:
            bad.append(f"{name}: {found} lines cite a ticket id, under its "
                       f"CITED entry of {entry}; lower the entry")
    for name in sorted(set(cited) - set(modules)):
        bad.append(f"{name}: not a tracked in-scope file; the CITED entry "
                   f"is stale")
    return bad


def functions(tree, prefix=""):
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, FUNCTIONS):
            yield prefix + node.name, node
            yield from functions(node, f"{prefix}{node.name}.")
        elif isinstance(node, ast.ClassDef):
            yield from functions(node, f"{prefix}{node.name}.")
        else:
            yield from functions(node, prefix)


def complexity_exempt(root=ROOT):
    found = set()
    for name in in_scope(root):
        text = (root / name).read_text()
        marks = comments(text)
        for qualname, node in functions(ast.parse(text)):
            if "noqa: C901" in marks.get(node.lineno, ""):
                found.add(f"{name}::{qualname}")
    return found


def complexity_violations(found, exempt=COMPLEXITY_EXEMPT):
    return ([f"{key}: carries noqa: C901 with no COMPLEXITY_EXEMPT entry"
             for key in sorted(found - set(exempt))]
            + [f"{key}: carries no noqa: C901; the COMPLEXITY_EXEMPT entry "
               f"is stale" for key in sorted(set(exempt) - found)])


def is_empty(stmt):
    return isinstance(stmt, ast.Pass) or (
        isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
        and stmt.value.value is Ellipsis)


def bare(tree):
    """The tree with every docstring dropped and an empty body read as pass."""
    for node in ast.walk(tree):
        if not isinstance(node, BODIED):
            continue
        body = node.body[1:] if node.body and is_docstring(node.body[0]) \
            else node.body
        if not isinstance(node, ast.Module) and all(map(is_empty, body)):
            body = [ast.Pass()]
        node.body = body
    return tree


def statement_name(stmt):
    if isinstance(stmt, (*FUNCTIONS, ast.ClassDef)):
        return stmt.name
    targets = stmt.targets if isinstance(stmt, ast.Assign) else \
        [stmt.target] if isinstance(stmt, ast.AnnAssign) else []
    if len(targets) == 1 and isinstance(targets[0], ast.Name):
        return targets[0].id
    return None


def statements(text):
    dumps = {}
    for position, stmt in enumerate(bare(ast.parse(text)).body):
        key = statement_name(stmt) or f"#{position}"
        if key in dumps:
            key = f"{key}#{position}"
        dumps[key] = ast.dump(stmt)
    return dumps


def code_changes(base, paths, root=ROOT):
    """`path: name` for each top-level statement whose code differs between
    `base` and the working tree once comments and docstrings are gone."""
    changed = []
    for path in paths:
        old = statements(subprocess.check_output(
            ["git", "show", f"{base}:{path}"], cwd=root, text=True))
        new = statements((root / path).read_text())
        changed.extend(f"{path}: {key}" for key in {**new, **old}
                       if old.get(key) != new.get(key))
    return changed


class CommentBudget(unittest.TestCase):

    def test_every_in_scope_module_holds_its_pins(self):
        modules = measure()
        self.assertEqual(budget_violations(modules), [])
        self.assertEqual(citation_violations(modules), [])

    def test_the_tables_are_exactly_what_the_measure_reads(self):
        modules = measure()
        self.assertEqual(PINNED, {n: m.count for n, m in modules.items()})
        self.assertEqual(CITED, {n: len(m.cited)
                                 for n, m in modules.items() if m.cited})
        self.assertLessEqual(
            {"holophyte/board/projection.py", "store/__init__.py", "factory.py"},
            set(PINNED))

    def test_the_in_scope_ratio_against_the_target(self):
        modules = measure().values()
        count = sum(m.count for m in modules)
        size = sum(m.size for m in modules)
        message = (f"in-scope ratio {count / size:.1%}: {count} comment and "
                   f"docstring lines in {size}; target {TARGET:.0%}")
        if not TARGET_ENFORCED:
            self.skipTest(message)
        self.assertLessEqual(count / size, TARGET, message)

    def test_every_noqa_c901_has_its_exemption_entry(self):
        self.assertEqual(complexity_violations(complexity_exempt()), [])


def git(root, *args):
    return subprocess.check_output(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", *args],
        cwd=root, text=True)


class Repo:
    """A temporary git repository whose files are added to the index."""

    def __init__(self, files):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        git(self.root, "init", "-q")
        self.write(files)

    def write(self, files):
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        git(self.root, "add", "-A")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.tmp.cleanup()


def lines(n, counted=0):
    """An n-line module whose first `counted` lines are comments."""
    return "# c\n" * counted + "x = 1\n" * (n - counted)


class MeasureSelfTests(unittest.TestCase):

    def test_comments_docstrings_and_noqa_count_and_a_shebang_does_not(self):
        text = ('#!/usr/bin/env python3\n'
                '"""Module\ndocstring."""\n'
                'import os  # noqa: F401\n'
                'NOTE = "not a docstring"\n'
                'class C:\n'
                '    """One line."""\n'
                '    def f(self):\n'
                '        # whole line\n'
                '        return "#"\n')
        self.assertEqual(sorted(counted_lines(text)), [2, 3, 4, 7, 9])

    def test_scope_is_the_packages_and_the_root_modules(self):
        with Repo({"factory.py": "", "holophyte/a.py": "", "store/b.py": "",
                   "tests/test_a.py": "", "contrib/c.py": "",
                   "scripts/d.py": ""}) as repo:
            self.assertEqual(sorted(measure(repo.root)),
                             ["factory.py", "holophyte/a.py", "store/b.py"])


class BudgetSelfTests(unittest.TestCase):

    def test_each_budget_failure_has_its_message(self):
        with Repo({"holophyte/over.py": lines(30, 4),
                   "holophyte/under.py": lines(30, 2),
                   "holophyte/new.py": lines(40, 3)}) as repo:
            (repo.root / "holophyte/loose.py").write_text(lines(5))
            bad = budget_violations(measure(repo.root), pinned={
                "holophyte/over.py": 3, "holophyte/under.py": 3,
                "holophyte/loose.py": 0})
        self.assertEqual(bad, [
            "holophyte/new.py: 3 comment and docstring lines in 40; "
            "an unlisted module is allowed 2",
            "holophyte/over.py: 4 comment and docstring lines grew past "
            "its pin of 3",
            "holophyte/under.py: 2 comment and docstring lines is under "
            "its pin of 3; lower the pin",
            "holophyte/loose.py: not a tracked in-scope file; the PINNED "
            "entry is stale",
        ])

    def test_an_unlisted_module_at_its_allowance_passes(self):
        with Repo({"store/new.py": lines(59, 2)}) as repo:
            self.assertEqual(budget_violations(measure(repo.root), pinned={}),
                             [])


class CitationSelfTests(unittest.TestCase):

    def test_docstring_and_comment_citations_fail_and_a_string_does_not(self):
        text = ('"""Added for HOLO-12."""\n'
                '\n'
                '\n'
                'def f():  # see KO-3\n'
                '    raise ValueError("REL-4 is not a docstring")\n')
        with Repo({"holophyte/mod.py": text}) as repo:
            bad = citation_violations(measure(repo.root), cited={})
        self.assertEqual(bad, [
            "holophyte/mod.py:1: cites a ticket id with no CITED entry",
            "holophyte/mod.py:4: cites a ticket id with no CITED entry",
        ])

    def test_every_ticket_prefix_is_caught(self):
        for ticket in ("KO-1", "HOLO-1", "REL-1", "LOTUS-1", "CROTON-1"):
            with self.subTest(ticket=ticket), \
                    Repo({"store/mod.py": f"x = 1  # {ticket}\n"}) as repo:
                self.assertEqual(
                    citation_violations(measure(repo.root), cited={}),
                    ["store/mod.py:1: cites a ticket id with no CITED entry"])

    def test_a_citation_entry_is_exact_and_a_zero_or_untracked_entry_stale(self):
        with Repo({"a.py": "# KO-1\n# KO-2\n", "b.py": "# KO-1\n",
                   "c.py": "x = 1\n"}) as repo:
            bad = citation_violations(measure(repo.root), cited={
                "a.py": 1, "b.py": 2, "c.py": 0, "gone.py": 1})
        self.assertEqual(bad, [
            "a.py: 2 lines cite a ticket id, grew past its CITED entry of 1",
            "b.py: 1 lines cite a ticket id, under its CITED entry of 2; "
            "lower the entry",
            "c.py: a CITED entry of 0 is stale",
            "gone.py: not a tracked in-scope file; the CITED entry is stale",
        ])


class ComplexitySelfTests(unittest.TestCase):

    def test_an_unlisted_exemption_fails_naming_the_function(self):
        text = ("class C:\n"
                "    def busy(self):  # noqa: C901 -- a long switch\n"
                "        return 1\n")
        with Repo({"holophyte/mod.py": text}) as repo:
            found = complexity_exempt(repo.root)
        self.assertEqual(complexity_violations(found, exempt={}), [
            "holophyte/mod.py::C.busy: carries noqa: C901 with no "
            "COMPLEXITY_EXEMPT entry"])

    def test_a_function_under_control_flow_is_found(self):
        text = ("if True:\n"
                "    def busy():  # noqa: C901\n"
                "        return 1\n"
                "try:\n"
                "    pass\n"
                "except ImportError:\n"
                "    class C:\n"
                "        def f(self):  # noqa: C901\n"
                "            return 1\n")
        with Repo({"holophyte/mod.py": text}) as repo:
            found = complexity_exempt(repo.root)
        self.assertEqual(found, {"holophyte/mod.py::busy",
                                 "holophyte/mod.py::C.f"})

    def test_an_entry_with_no_noqa_is_stale(self):
        self.assertEqual(
            complexity_violations(set(), exempt={"a.py::f": None}),
            ["a.py::f: carries no noqa: C901; the COMPLEXITY_EXEMPT entry "
             "is stale"])

    def test_the_two_standing_exemptions_are_listed(self):
        found = complexity_exempt()
        for key in ("holophyte/loop/gates.py::split_and_clauses",
                    "ticket_template.py::validate"):
            self.assertIn(key, found)
            self.assertIn(key, COMPLEXITY_EXEMPT)


BEFORE = '''"""Module docstring,
over two lines."""
import os  # trailing


# whole line
def f():
    """Function docstring."""
    return 1  # trailing


class Empty:
    """Only a docstring."""


def g():
    """Another."""
    # inside
    return os.sep
'''

AFTER = '''import os


def f():
    return 1


class Empty:
    pass


def g():
    return os.sep
'''


class CodeChangesSelfTests(unittest.TestCase):

    def test_removing_comments_and_docstrings_is_no_code_change(self):
        with Repo({"holophyte/mod.py": BEFORE}) as repo:
            git(repo.root, "commit", "-qm", "first")
            repo.write({"holophyte/mod.py": AFTER})
            self.assertEqual(code_changes("HEAD", ["holophyte/mod.py"],
                                          root=repo.root), [])
            repo.write({"holophyte/mod.py": AFTER.replace("return 1",
                                                          "return 2")})
            self.assertEqual(code_changes("HEAD", ["holophyte/mod.py"],
                                          root=repo.root),
                             ["holophyte/mod.py: f"])


if __name__ == "__main__":
    unittest.main()
