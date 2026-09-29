#!/usr/bin/env python3
"""story_template: validator for a storyTemplate.md-shaped story directory.

A story directory holds the story body (story.md), a witnesses directory
with each witness file at its repository path, and a children directory.

CLI: python3 ticket_template.py [--repo PATH] --story DIR  ->  exit 0 iff valid.
"""
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
MAX_WITNESSES = 10
ADVISED_WITNESSES = 5
MAX_STANDING_ORDERS = 5
WITNESS_RE = re.compile(
    r"^\[[ xX]\]\s*(W\d+):\s*(.+?)\s*\(a test in (\S+) witnesses [^()]+\)$")
COMMAND_RE = re.compile(r"^(W\d+):\s*(\S.*)$")
SKIP_CALLS = ("skipTest(", "unittest.skip", "pytest.skip", "pytest.mark.skip")
ONE_PASS_WORDS = ("one pass", "first pass", "immediately")
USAGE = "usage: python3 ticket_template.py [--repo PATH] --story DIR"


class Witness(NamedTuple):
    key: str
    outcome: str
    file: str
    command: str | None
    line: str


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


def validate_story(directory, repo=None):
    """Problem lines for the story in `directory`, advisories prefixed with
    ADVISORY_PREFIX; `repo`, when given, is checked for each witness file."""
    body = Path(directory) / BODY
    if not body.is_file():
        return [f"missing story body: {body}"]
    story = parse_story(body.read_text())
    problems = _body_problems(story)
    if repo is not None:
        problems.extend(_ignore_problems(story.witnesses, repo))
    for witness in story.witnesses:
        problems.extend(_file_problems(witness, directory, repo))
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
