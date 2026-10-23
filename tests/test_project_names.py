"""The value carrying a project's paths is spelled `project`, never `target`.

`holophyte.config.project.Project` names the type; a module group whose ticket has
landed keeps the old spellings out. The guard reads source with `ast` and
skips keyword arguments: a keyword follows the callee's signature, which the
callee's own group renames, and `threading.Thread(target=...)` must stay.
"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OLD_NAMES = frozenset({"target", "tgt"})


def old_name_sites(paths, names=OLD_NAMES):
    """`path:line kind name` for every parameter, name or attribute spelled
    one of `names` (by default `target` or `tgt`) in the given files."""
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
            if name in names:
                sites.append(f"{path.relative_to(ROOT)}:{node.lineno} {kind} {name}")
    return sorted(sites)


def _bound(target, value):
    """(target, value) pairs an assignment binds, unpacking a tuple or list
    target against an equally long literal; otherwise every target element
    is paired with the whole value."""
    if isinstance(target, ast.Starred):
        yield from _bound(target.value, value)
    elif isinstance(target, (ast.Tuple, ast.List)):
        if (isinstance(value, (ast.Tuple, ast.List))
                and len(value.elts) == len(target.elts)
                and not any(isinstance(e, ast.Starred) for e in value.elts)):
            for each, part in zip(target.elts, value.elts):
                yield from _bound(each, part)
        else:
            for each in target.elts:
                yield from _bound(each, value)
    else:
        yield target, value


def _registers_project(value):
    return any(
        isinstance(node, ast.Call)
        and getattr(node.func, "attr", getattr(node.func, "id", None))
        in ("ensure_project", "register_project")
        for node in ast.walk(value))


def store_id_as_project_lines(tree):
    """Lines where an attribute `project` is assigned, plainly, annotated or
    by unpacking, a value from `ensure_project` or `register_project`."""
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        else:
            continue
        for target in targets:
            for each, value in _bound(target, node.value):
                if (isinstance(each, ast.Attribute) and each.attr == "project"
                        and _registers_project(value)):
                    lines.append(node.lineno)
    return lines


def store_id_as_project_sites(paths):
    """`path:line` for every attribute `project` assigned the store id a call
    to `ensure_project` or `register_project` returns."""
    sites = []
    for path in paths:
        tree = ast.parse(path.read_text(), filename=str(path))
        sites += [f"{path.relative_to(ROOT)}:{line}"
                  for line in store_id_as_project_lines(tree)]
    return sorted(sites)


class ProjectNamesTest(unittest.TestCase):
    def test_the_tests_hold_the_project_as_project_and_its_id_as_project_id(self):
        paths = sorted((ROOT / "tests").glob("*.py"))
        with self.subTest(spelling="tgt"):
            sites = old_name_sites(paths, frozenset({"tgt"}))
            self.assertEqual(sites, [], "\n" + "\n".join(sites))
        with self.subTest(store_id="project"):
            sites = store_id_as_project_sites(paths)
            self.assertEqual(sites, [], "\n" + "\n".join(sites))

    def test_the_store_id_guard_sees_annotated_and_unpacked_assignments(self):
        source = "\n".join([
            "self.project = ensure_project(conn, path)",
            "self.project: int = store.ensure_project(conn, path)",
            "self.project, self.db = register_project(conn, path), db",
            "self.db, [self.project] = db, [ensure_project(conn, path)]",
            "self.project_id: int = ensure_project(conn, path)",
            "self.project, self.project_id = locate(path), ensure_project(conn, path)",
            "self.project: Project",
        ])
        self.assertEqual(store_id_as_project_lines(ast.parse(source)), [1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main()
