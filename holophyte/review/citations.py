import ast
import re
from pathlib import PurePosixPath


class DottedReference(tuple):
    __slots__ = ()


def defining_classes(source, name):
    stack, classes = [], []
    for line in source.splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        while stack and stack[-1][0] >= indent:
            stack.pop()
        match = re.match(r"\s*class (\w+)\b", line)
        if match:
            stack.append((indent, match.group(1)))
        elif stack and re.match(rf"\s*def {name}\(", line):
            classes.append(stack[-1][1])
    return sorted(set(classes))


def dotted_module(reference):
    path, cls, name = reference
    if not isinstance(reference, DottedReference) or cls is not None:
        return None
    return str(PurePosixPath(path).with_suffix("") / f"{name}.py")


def test_unchanged(approved, candidate, cls, name):
    try:
        trees = [ast.parse(source) for source in (approved, candidate)]
    except (SyntaxError, TypeError, ValueError):
        return False
    if not all(_cited(tree, cls, name) for tree in trees):
        return False
    bound = _module_bindings(trees[0])
    before, after = (_strip(tree, cls, name) for tree in trees)
    return (ast.dump(trees[0]) == ast.dump(trees[1]) and before.keys() <= after.keys()
            and _binds_new_names_only(
                [node for key, node in after.items() if key not in before],
                bound, before.values()))


def _cited(tree, cls, name):
    body = tree.body
    if cls is not None:
        body = next((node.body for node in ast.walk(tree)
                     if isinstance(node, ast.ClassDef) and node.name == cls), [])
    return any(_is_def(node) and node.name == name for node in body)


def _is_def(node):
    return isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)


def _strip(tree, cls, name):
    imports = {ast.dump(node): node for node in tree.body
               if isinstance(node, ast.Import | ast.ImportFrom)}
    tree.body = [node for node in tree.body
                 if not isinstance(node, ast.Import | ast.ImportFrom)]
    _drop_other_tests(tree, None, cls, name)
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    tree.body = [node for node in tree.body
                 if not (isinstance(node, ast.ClassDef) and not node.body
                         and node.name not in used)]
    return imports


def _drop_other_tests(node, owner, cls, name):
    kept = []
    for child in node.body:
        if (_is_def(child) and child.name.startswith("test")
                and (owner, child.name) != (cls, name)):
            continue
        if isinstance(child, ast.ClassDef):
            _drop_other_tests(child, child.name, cls, name)
        kept.append(child)
    node.body = kept


def _binds_new_names_only(added, bound, approved):
    roots = {alias.name.split(".")[0] for node in approved
             if isinstance(node, ast.Import)
             for alias in node.names if alias.asname is None}
    for node in added:
        for alias in node.names:
            plain = isinstance(node, ast.Import) and alias.asname is None
            binding = alias.asname or (alias.name.split(".")[0] if plain
                                       else alias.name)
            shared_root = plain and "." in alias.name and binding in roots
            if binding == "*" or binding in bound and not shared_root:
                return False
    return True


def _module_bindings(tree):
    bound = set()
    for child in _module_level(tree):
        if _is_def(child) or isinstance(child, ast.ClassDef):
            bound.add(child.name)
        elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
            bound.add(child.id)
        elif isinstance(child, ast.alias):
            bound.add(child.asname or child.name.split(".")[0])
    return bound


def _module_level(node):
    yield node
    if _is_def(node) or isinstance(node, ast.ClassDef | ast.Lambda):
        return
    for child in ast.iter_child_nodes(node):
        yield from _module_level(child)
