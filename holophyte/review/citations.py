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
    return sorted(node.name for node in ast.walk(tree)
                  if isinstance(node, ast.ClassDef)
                  and any(_is_def(child) and child.name == name
                          for child in node.body))


def _scanned_classes(source, name):
    stack, classes = [], {}
    for number, line in enumerate(source.splitlines()):
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
            classes[stack[-1][3]] = stack[-1][2]
        stack.append((indent, match.group(1) == "class", match.group(2), number))
    return sorted(classes.values())


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
    before, after = ([(ast.dump(node), node) for node in tree.body
                      if isinstance(node, ast.Import | ast.ImportFrom)]
                     for tree in trees)
    added = _added_imports(before, after)
    if added is None:
        return False
    trees[1].body = [node for node in trees[1].body
                     if all(node is not other for other in added)]
    for tree in trees:
        _strip(tree, cls, name, bound)
    return (ast.dump(trees[0]) == ast.dump(trees[1])
            and _binds_new_names_only(added, bound, [node for _, node in before]))


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
    others = _other_tests(tree, None, cls, name)
    while called := [test for test in others
                     if test[1].name in _referenced(tree, others)]:
        others = [test for test in others if test not in called]
    for parent, test in others:
        parent.body = [child for child in parent.body if child is not test]
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    tree.body = [node for node in tree.body
                 if not (isinstance(node, ast.ClassDef) and not node.body
                         and node.name not in used | bound)]


def _other_tests(node, owner, cls, name):
    found = []
    for child in node.body:
        if (_is_def(child) and child.name.startswith("test")
                and (owner, child.name) != (cls, name)):
            found.append((node, child))
        elif isinstance(child, ast.ClassDef):
            found += _other_tests(child, child.name, cls, name)
    return found


def _referenced(tree, others):
    skipped = {id(test) for _, test in others}
    names, pending = set(), [tree]
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        pending += [child for child in ast.iter_child_nodes(node)
                    if id(child) not in skipped]
    return names


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
            if "*" in (binding, *bound) or binding in bound and not shared_root:
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
    for child in _evaluated_at_definition(node):
        if child is not None:
            yield from _module_level(child)


def _evaluated_at_definition(node):
    if _is_def(node) or isinstance(node, ast.Lambda):
        arguments = node.args
        signature = [*arguments.posonlyargs, *arguments.args, arguments.vararg,
                     *arguments.kwonlyargs, arguments.kwarg]
        return [*arguments.defaults, *arguments.kw_defaults,
                *getattr(node, "decorator_list", []), getattr(node, "returns", None),
                *(arg.annotation for arg in signature if arg is not None)]
    if isinstance(node, ast.ClassDef):
        return [*node.bases, *node.keywords, *node.decorator_list]
    if isinstance(node, ast.comprehension):
        return [node.iter, *node.ifs]
    return ast.iter_child_nodes(node)
