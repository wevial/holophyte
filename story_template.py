#!/usr/bin/env python3
import copy
import itertools
import posixpath
import re
import sys
from pathlib import Path
from typing import NamedTuple

import ticket_template as tt

STORY_ORDER = [
    "Summary", "Goal", "Witnesses", "Witness commands", "Standing orders",
    "Out of scope", "Open questions",
]
BODY = "story.md"
WITNESSES = "witnesses"
CHILDREN = "children"
MAX_CHILDREN = 10
MAX_WITNESSES = 10
ADVISED_WITNESSES = 5
MAX_STANDING_ORDERS = 5
WITNESS_RE = re.compile(
    r"^\[[ xX]\]\s*(W\d+):\s*(.+?)\s*\(a test in (\S+) witnesses [^()]+\)$")
COMMAND_RE = re.compile(r"^(W\d+):\s*(\S.*)$")
KEY_RE = re.compile(r"^\[[ xX]\]\s*(W\d+):")
CHILD_NAME_RE = re.compile(r"^\d{2}-(.+)\.md$")
HEADER_RE = re.compile(r"^Ticket:[ \t]*\S+[ \t]*\n")
ROLE_RE = re.compile(r"^Role:\s*(?:(scaffolding)|(completes|advances)\s+"
                     r"(W\d+(?:\s*,\s*W\d+)*))$")
ROLE_FORMS = ("'Role: completes Wn', 'Role: advances Wn, ...' or "
              "'Role: scaffolding'")
SKIP_CALLS = ("skipTest(", "unittest.skip", "pytest.skip", "pytest.mark.skip")
ONE_PASS_WORDS = ("one pass", "first pass", "immediately")
USAGE = "usage: python3 ticket_template.py [--repo PATH] --story DIR"


class Witness(NamedTuple):
    key: str
    outcome: str
    file: str
    command: str | None
    line: str


class Child(NamedTuple):
    name: str
    slug: str
    ticket: tt.Ticket
    role: str | None
    witnesses: tuple
    depends_on: list


class Story(NamedTuple):
    order: list
    sections: dict
    witness_lines: list
    witnesses: list
    standing_orders: list
    open_questions_none: bool


def _items(story, section):
    return tt._list_items(tt.COMMENT_RE.sub("", story.sections.get(section, "")))


def parse_story(text):
    ticket = tt.parse(text)
    commands = {}
    for line in tt._fenced_lines(
            tt.COMMENT_RE.sub("", ticket.sections.get("Witness commands", ""))):
        match = COMMAND_RE.match(line)
        if match:
            commands.setdefault(match.group(1), match.group(2).strip())
    story = Story(ticket.order, ticket.sections, [], [], [],
                  ticket.open_questions_none)
    story.witness_lines.extend(_items(story, "Witnesses"))
    for line in story.witness_lines:
        match = WITNESS_RE.match(line)
        if match:
            key, outcome, file = match.groups()
            story.witnesses.append(
                Witness(key, outcome, file, commands.get(key), line))
    story.standing_orders.extend(_items(story, "Standing orders"))
    return story


def _body_problems(story):
    problems = [f"missing section '## {name}'"
                for name in STORY_ORDER if name not in story.order]
    count = len(story.witness_lines)
    if not count:
        problems.append("'Witnesses' has no witness lines")
    elif count > MAX_WITNESSES:
        problems.append(f"'Witnesses' has {count} witnesses; the cap is "
                        f"{MAX_WITNESSES} — split the story")
    elif count > ADVISED_WITNESSES:
        problems.append(f"{tt.ADVISORY_PREFIX}'Witnesses' has {count} "
                        f"witnesses; more than {ADVISED_WITNESSES} is a large "
                        f"story — consider splitting it")
    for line in story.witness_lines:
        if not WITNESS_RE.match(line):
            problems.append("witness line is not in the criterion form "
                            "'- [ ] Wn: OUTCOME (a test in FILE witnesses X)': "
                            f"{line}")
    owners = {}
    for witness in story.witnesses:
        if witness.command is None:
            problems.append(f"witness {witness.key} has no line in "
                            "'Witness commands'")
        file = posixpath.normpath(witness.file)
        if file in owners:
            problems.append(f"witnesses {owners[file]} and "
                            f"{witness.key} name one file: {witness.file}")
        owners.setdefault(file, witness.key)
    if len(story.standing_orders) > MAX_STANDING_ORDERS:
        problems.append(f"'Standing orders' has {len(story.standing_orders)} "
                        f"lines; the limit is {MAX_STANDING_ORDERS}, since "
                        "every child's instructions carry them")
    if not story.open_questions_none:
        problems.append("'Open questions' must read exactly '- None'")
    return problems


def _file_problems(witness, directory, repo):
    tree = Path(directory) / WITNESSES
    if repo is not None and tt._outside(Path(repo), witness.file):
        return [f"witness file is outside the repository in {witness.key}: "
                f"{witness.file}"]
    problems = []
    path = tree / witness.file
    if tt._outside(tree, witness.file) or not path.is_file():
        problems.append(f"witness file is missing from {tree} in "
                        f"{witness.key}: {witness.file}")
        return problems
    text = path.read_text()
    skip = next((call for call in SKIP_CALLS if call in text), None)
    if skip:
        problems.append(f"{tt.ADVISORY_PREFIX}witness file for {witness.key} "
                        f"calls {skip}; no witness is skipped: {witness.file}")
    return problems + _one_pass_advisories(witness, text)


def _ignore_problems(witnesses, repo):
    problems, unchecked = [], False
    for witness in witnesses:
        if tt._outside(Path(repo), witness.file):
            continue
        ignored = tt._gitignored(repo, witness.file)
        if ignored:
            problems.append(f"gitignored witness file in {witness.key}: "
                            f"{witness.file}")
        elif ignored is None:
            unchecked = True
    if unchecked:
        problems.append(f"{tt.ADVISORY_PREFIX}could not check paths against "
                        f"{repo}: git check-ignore failed there (not a "
                        f"repository?)")
    return problems


def _one_pass_advisories(witness, file_text):
    for where, text in (("criterion", witness.line), ("file", file_text)):
        word = next((w for w in ONE_PASS_WORDS if w in text.lower()), None)
        if word:
            return [f"{tt.ADVISORY_PREFIX}witness {witness.key}'s {where} "
                    f"says '{word}'; a witness asserts a settled outcome, "
                    "not the state after one pass"]
    return []


def _role(ticket):
    story = tt.COMMENT_RE.sub("", ticket.sections.get("Story", ""))
    for line in story.splitlines():
        match = ROLE_RE.match(line.strip())
        if match and match.group(1):
            return match.group(1), ()
        if match:
            return match.group(2), tuple(
                key.strip() for key in match.group(3).split(","))
    return None, ()


def parse_children(directory):
    folder = Path(directory) / CHILDREN
    children = []
    for path in sorted(folder.glob("*.md")) if folder.is_dir() else ():
        match = CHILD_NAME_RE.match(path.name)
        ticket = tt.parse(HEADER_RE.sub("", path.read_text(), count=1))
        role, keys = _role(ticket)
        children.append(Child(path.stem, match.group(1) if match else path.stem,
                              ticket, role, keys, ticket.depends_on or []))
    return children


def _child_problems(children, repo):
    placeholders = {child.slug: f"STORY-{index}"
                    for index, child in enumerate(children, 1)}
    problems = []
    for child in children:
        ticket = copy.copy(child.ticket)
        if ticket.depends_on is not None:
            ticket.depends_on = [placeholders.get(dep, dep)
                                 for dep in ticket.depends_on
                                 if dep in placeholders
                                 or tt.LINEAR_ID_RE.match(dep)]
        found = tt.validate(ticket, repo)
        blockers = tt.blocking(found)
        if blockers:
            problems.append(f"child {child.name} is not a valid ticket: "
                            + "; ".join(blockers))
        problems.extend(f"{tt.ADVISORY_PREFIX}child {child.name}: "
                        f"{problem.removeprefix(tt.ADVISORY_PREFIX)}"
                        for problem in found if problem not in blockers)
        if "Story" not in child.ticket.order:
            problems.append(f"child {child.name} has no '## Story' section "
                            "naming its role")
        elif child.role is None:
            problems.append(f"child {child.name}'s '## Story' has no "
                            f"{ROLE_FORMS} line")
    return problems


def _declares_new(ticket, file):
    files, directories = tt._new_paths(ticket)
    normalized = str(Path(file))
    return normalized in files or any(
        normalized.startswith(directory + "/") for directory in directories)


def _role_problems(children, story):
    keys = {match.group(1) for match in map(KEY_RE.match, story.witness_lines)
            if match}
    problems = [f"child {child.name}'s role names {key}, which is no "
                "witness of the story"
                for child in children for key in child.witnesses
                if key not in keys]
    for witness in story.witnesses:
        completing = [child for child in children if child.role == "completes"
                      and witness.key in child.witnesses]
        if not completing:
            problems.append(f"witness {witness.key} has no completing child")
        elif len(completing) > 1:
            problems.append(f"witness {witness.key} is completed by "
                            f"{len(completing)} children: "
                            + ", ".join(child.name for child in completing))
        elif not _declares_new(completing[0].ticket, witness.file):
            problems.append(f"child {completing[0].name} completes "
                            f"{witness.key} but does not call its witness "
                            f"file new: {witness.file}")
    return problems


def _cycle(edges):
    state = {}

    def visit(path):
        state[path[-1]] = "open"
        for dep in edges[path[-1]]:
            if state.get(dep) == "open":
                return path[path.index(dep):] + [dep]
            found = None if dep in state else visit(path + [dep])
            if found:
                return found
        state[path[-1]] = "done"
        return None

    return next(filter(None, (visit([slug]) for slug in edges
                              if slug not in state)), None)


def _ancestors(slug, edges):
    seen, stack = set(), list(edges[slug])
    while stack:
        dep = stack.pop()
        if dep not in seen:
            seen.add(dep)
            stack.extend(edges[dep])
    return seen


def _graph_problems(children, ancestors, edges):
    by_slug = {child.slug: child for child in children}
    problems = [f"child {child.name} depends on {dep}, which is no sibling "
                "slug or ticket id"
                for child in children for dep in child.depends_on
                if dep not in by_slug and not tt.LINEAR_ID_RE.match(dep)]
    cycle = _cycle(edges)
    if cycle:
        names = [by_slug[slug].name for slug in cycle]
        problems.append(f"children {', '.join(sorted(set(names)))} depend on "
                        f"each other in a cycle: {' -> '.join(names)}")
    for child in children:
        if child.role != "completes":
            continue
        for key in child.witnesses:
            for other in children:
                if (other.role == "advances" and key in other.witnesses
                        and other.slug not in ancestors[child.slug]):
                    problems.append(f"child {child.name} completes {key} but "
                                    f"does not depend on {other.name}, "
                                    "which advances it")
    return problems


def _named_files(ticket):
    return {str(Path(path))
            for section in ("In scope", "Implementation notes")
            for _, path in tt._prose_paths(
                tt.COMMENT_RE.sub("", ticket.sections.get(section, "")))}


def _plan_advisories(children, ancestors):
    completing = [child for child in children if child.role == "completes"]
    advisories = [f"{tt.ADVISORY_PREFIX}scaffolding child {child.name} "
                  "precedes no completing child: none depends on it"
                  for child in children if child.role == "scaffolding"
                  and not any(child.slug in ancestors[other.slug]
                              for other in completing)]
    named = {child.slug: _named_files(child.ticket) for child in children}
    for one, two in itertools.combinations(children, 2):
        if one.slug in ancestors[two.slug] or two.slug in ancestors[one.slug]:
            continue
        advisories.extend(f"{tt.ADVISORY_PREFIX}children {one.name} and "
                          f"{two.name} both name {path} with no dependency "
                          "path between them"
                          for path in sorted(named[one.slug] & named[two.slug]))
    return advisories


def _children_problems(children, story, repo):
    problems = []
    if len(children) > MAX_CHILDREN:
        problems.append(f"the story has {len(children)} children; the cap is "
                        f"{MAX_CHILDREN} — split the story")
    problems.extend(_child_problems(children, repo))
    problems.extend(_role_problems(children, story))
    first = {}
    for child in children:
        other = first.setdefault(child.slug, child)
        if other is not child:
            problems.append(f"children {other.name} and {child.name} share "
                            f"the slug {child.slug}")
    if len(first) < len(children):
        return problems
    slugs = set(first)
    edges = {child.slug: [dep for dep in child.depends_on if dep in slugs]
             for child in children}
    ancestors = {slug: _ancestors(slug, edges) for slug in edges}
    problems.extend(_graph_problems(children, ancestors, edges))
    return problems + _plan_advisories(children, ancestors)


def validate_story(directory, repo=None):
    """Problem lines, advisories prefixed with ADVISORY_PREFIX."""
    body = Path(directory) / BODY
    if not body.is_file():
        return [f"missing story body: {body}"]
    story = parse_story(body.read_text())
    problems = _body_problems(story)
    if repo is not None:
        problems.extend(_ignore_problems(story.witnesses, repo))
    for witness in story.witnesses:
        problems.extend(_file_problems(witness, directory, repo))
    problems.extend(_children_problems(parse_children(directory), story, repo))
    return problems


def _parse_args(argv):
    repo, story, args = None, None, list(argv)
    while args:
        arg = args.pop(0)
        name, eq, value = arg.partition("=")
        if name not in ("--repo", "--story"):
            return None
        if not eq:
            if not args:
                return None
            value = args.pop(0)
        if name == "--repo":
            repo = value
        else:
            story = value
    if not story or repo == "":
        return None
    return repo, story


def main(argv):
    if any(arg in ("-h", "--help") for arg in argv):
        print(USAGE)
        return 0
    parsed = _parse_args(argv)
    if parsed is None:
        print(USAGE, file=sys.stderr)
        return 2
    repo, directory = parsed
    problems = validate_story(directory, repo)
    blockers = tt.blocking(problems)
    advisories = [a for a in problems if a.startswith(tt.ADVISORY_PREFIX)]
    if repo is None:
        advisories.append(f"{tt.ADVISORY_PREFIX}repository check skipped: pass "
                          "--repo PATH to check witness files against the "
                          "project repository")
    print(f"{directory}: {'INVALID' if blockers else 'OK'}")
    for problem in blockers + advisories:
        print(f"  - {problem}")
    return 1 if blockers else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
