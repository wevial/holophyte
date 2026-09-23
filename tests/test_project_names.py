"""The value carrying a project's paths is spelled `project`, never `target`.

`holophyte.project.Project` names the type; a module group whose ticket has
landed keeps the old spellings out. The guard reads source with `ast` and
skips keyword arguments: a keyword follows the callee's signature, which the
callee's own group renames, and `threading.Thread(target=...)` must stay.
"""
import ast
import dataclasses
import inspect
import unittest
from pathlib import Path

import holophyte.claim
import holophyte.run
import holophyte.serve_runs

ROOT = Path(__file__).resolve().parent.parent
OLD_NAMES = frozenset({"target", "tgt"})


def old_name_sites(paths):
    """`path:line kind name` for every parameter, name or attribute spelled
    `target` or `tgt` in the given files."""
    sites = []
    for path in paths:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.arg):
                kind, name = "parameter", node.arg
            elif isinstance(node, ast.Name):
                kind, name = "name", node.id
            elif isinstance(node, ast.Attribute):
                kind, name = "attribute", node.attr
            else:
                continue
            if name in OLD_NAMES:
                sites.append(f"{path.relative_to(ROOT)}:{node.lineno} {kind} {name}")
    return sorted(sites)


class ProjectNamesTest(unittest.TestCase):
    def assert_no_old_names(self, *modules):
        sites = old_name_sites([ROOT / m for m in modules])
        self.assertEqual(sites, [], "\n" + "\n".join(sites))

    def test_the_daemon_modules_spell_the_project_project(self):
        self.assert_no_old_names(
            "holophyte/serve.py", "holophyte/serve_runs.py",
            "holophyte/serve_config.py", "holophyte/serve_actions.py")

    def test_serve_runs_names_a_path_and_an_object_apart(self):
        def params(fn):
            return list(inspect.signature(fn).parameters)

        self.assertEqual(params(holophyte.serve_runs.migration_rows),
                         ["conn", "since", "limit", "project_path"])
        for fn in (holophyte.serve_runs.no_store, holophyte.serve_runs.runs,
                   holophyte.serve_runs.ledger):
            with self.subTest(fn=fn.__name__):
                self.assertEqual(params(fn)[0], "project")

    def test_the_run_modules_spell_the_project_project(self):
        self.assert_no_old_names(
            "holophyte/run.py", "holophyte/loop.py", "holophyte/claim.py",
            "holophyte/gates.py", "holophyte/merge_gate.py",
            "holophyte/babysitter.py", "holophyte/pullrequest.py")

    def test_the_run_carries_a_project_and_the_claim_a_project_id(self):
        def params(fn):
            return list(inspect.signature(fn).parameters)

        names = [f.name for f in dataclasses.fields(holophyte.run.Run)]
        self.assertEqual(names[0], "project")
        self.assertNotIn("target", names)
        for fn in (holophyte.claim._claim_next, holophyte.claim._admit_ticket,
                   holophyte.claim._claim_run):
            with self.subTest(fn=fn.__name__):
                self.assertEqual(params(fn)[0], "project")
                self.assertEqual(params(fn)[2], "project_id")
        self.assertEqual(params(holophyte.claim._park_unlisted),
                         ["conn", "project_id", "listed"])


if __name__ == "__main__":
    unittest.main()
