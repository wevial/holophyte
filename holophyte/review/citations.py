import ast
import re
from pathlib import PurePosixPath


class DottedReference(tuple):
    __slots__ = ()


def defining_classes(source, name):
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return _scanned_classes(source, name)
    return sorted({node.name for node in ast.walk(tree)
                   if isinstance(node, ast.ClassDef)
                   and any(_is_def(child) and child.name == name
                           for child in node.body)})


def _scanned_classes(source, name):
    stack, classes = [], []
    for line in source.splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        while stack and stack[-1][0] >= indent:
            stack.pop()
        match = re.match(r"\s*(class|(?:async\s+)?def)\s+(\w+)\b", line)
        if match is None:
            continue
        if (match.group(1) != "class" and match.group(2) == name
                and stack and stack[-1][1]):
            classes.append(stack[-1][2])
        stack.append((indent, match.group(1) == "class", match.group(2)))
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
    before, after = (_strip(tree, cls, name, bound) for tree in trees)
    added = _added_imports(before, after)
    return (ast.dump(trees[0]) == ast.dump(trees[1]) and added is not None
            and _binds_new_names_only(added, bound,
                                      [node for _, node in before]))


def _added_imports(before, after):
    pending, added = [key for key, _ in before], []
    for key, node in after:
        if pending and key == pending[0]:
            pending.pop(0)
        else:
            added.append(node)
    return None if pending else added


def _cited(tree, cls, name):
    body = tree.body
    if cls is not None:
        body = next((node.body for node in ast.walk(tree)
                     if isinstance(node, ast.ClassDef) and node.name == cls), [])
    return any(_is_def(node) and node.name == name for node in body)


def _is_def(node):
    return isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)


def _strip(tree, cls, name, bound):
    imports = [(ast.dump(node), node) for node in tree.body
               if isinstance(node, ast.Import | ast.ImportFrom)]
    tree.body = [node for node in tree.body
                 if not isinstance(node, ast.Import | ast.ImportFrom)]
    _drop_other_tests(tree, None, cls, name)
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    tree.body = [node for node in tree.body
                 if not (isinstance(node, ast.ClassDef) and not node.body
                         and node.name not in used | bound)]
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
        elif isinstance(child, ast.MatchAs | ast.MatchStar | ast.ExceptHandler):
            bound.add(child.name)
        elif isinstance(child, ast.MatchMapping):
            bound.add(child.rest)
    bound.update(name for node in ast.walk(tree) if isinstance(node, ast.Global)
                 for name in node.names)
    return bound - {None}


def _module_level(node):
    yield node
    if _is_def(node) or isinstance(node, ast.ClassDef | ast.Lambda):
        return
    for child in ast.iter_child_nodes(node):
        yield from _module_level(child)
