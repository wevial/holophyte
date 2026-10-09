import re
import subprocess
from pathlib import Path
from typing import NamedTuple

import store
from holophyte.commit_hygiene import _unpublished
from holophyte.config.config_tables import merge_config
from holophyte.loop.gates import InfraFailure
from holophyte.redact import register_values

KEY = "[merge] private_patterns"
SHOWN = 5
HUNK = re.compile(r"^@@@* (?:-\S+ )+\+(\d+)")


class Leak(NamedTuple):
    location: str
    index: int
    path: str = ""
    line: int = 0

    def describe(self):
        return f"{self.location} ({KEY} #{self.index})"


class PrivateMatch(InfraFailure):
    def __init__(self, surface, leaks, preserved):
        self.leaks = tuple(leaks)
        named = "; ".join(leak.describe() for leak in self.leaks[:SHOWN])
        if len(self.leaks) > SHOWN:
            named += f"; and {len(self.leaks) - SHOWN} more"
        super().__init__(f"{surface} holds text the project does not publish:"
                         f" {named}; {preserved}")


def patterns(project):
    return [re.compile(p) for p in merge_config(project).private_patterns]


def _search(compiled, line):
    for index, pattern in enumerate(compiled):
        match = pattern.search(line)
        if match:
            register_values([match.group()])
            yield index


def scan_text(compiled, text):
    for number, line in enumerate(text.split("\n"), 1):
        for index in _search(compiled, line):
            yield number, index


def _git(wt, *args):
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotePath=false", "-c", "log.showSignature=false",
             *args], cwd=wt,
            capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InfraFailure(f"private pattern scan: git {args[0]}: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise InfraFailure(f"private pattern scan: git {args[0]}: {detail}")
    return result.stdout.decode("utf-8", errors="replace")


def _added_lines(diff, parents):
    """Yield path, line number and text of each line new against every parent."""
    width = max(parents, 1)
    path, number, header = None, 0, False
    for line in diff.split("\n"):
        if line.startswith("diff "):
            path, header = None, True
        elif header and line.startswith("+++ "):
            name = line[4:].rstrip("\t")
            path = None if name == "/dev/null" else name
        elif HUNK.match(line):
            number, header = int(HUNK.match(line).group(1)), False
        elif header or path is None or "-" in line[:width] or line[:1] == "\\":
            continue
        else:
            if line[:width] == "+" * width:
                yield path, number, line[width:]
            number += 1


def scan_commits(compiled, wt, commits):
    for sha in commits:
        short, *parents = _git(wt, "log", "-1", "--format=%h %P", sha).split()
        message = _git(wt, "log", "-1", "--format=%B", sha)
        for number, index in scan_text(compiled, message):
            yield Leak(f"commit {short} message line {number}", index)
        diff = _git(wt, "show", "--format=", "--cc", "-M", "--unified=0",
                    "--no-color", "--no-ext-diff", "--no-textconv",
                    "--no-prefix", sha)
        for path, number, text in _added_lines(diff, len(parents)):
            for index in _search(compiled, text):
                yield Leak(f"{path}:{number}", index, path, number)


def branch_leaks(project, wt, tip):
    compiled = patterns(project)
    if not compiled:
        return []
    return list(scan_commits(compiled, wt, _unpublished(wt, tip)))


def refuse_private_history(project, branch):
    leaks = branch_leaks(project, project.path, f"refs/heads/{branch}")
    if leaks:
        raise PrivateMatch(f"branch {branch}", leaks,
                           "branch preserved, nothing pushed")


def text_leaks(project, field, text):
    return [Leak(f"{field} line {number}", index)
            for number, index in scan_text(patterns(project), text)]


def refuse_private_text(project, **fields):
    leaks = [leak for field, text in fields.items()
             for leak in text_leaks(project, field.replace("_", " "), text)]
    if leaks:
        raise PrivateMatch("the pull request text", leaks,
                           "nothing sent to GitHub")


def review_findings(leaks):
    return [{"path": leak.path or leak.location, "line": leak.line or None,
             "severity": "p1", "title": f"private text at {leak.describe()}",
             "message": (f"{leak.location} holds text the project does not"
                         f" publish ({KEY} #{leak.index}); "
                         + ("replace it with a neutral placeholder in the commit"
                            " that adds it" if leak.path
                            else "reword that commit without it")
                         + ", as a push publishes every commit")}
            for leak in leaks]


def with_findings(verdict, findings):
    if not findings:
        return verdict
    return (f"{verdict}\n\nThe factory refuses to publish these lines:\n"
            + "\n".join(f"- {finding['message']}" for finding in findings))


def ticket_problems(repo, text):
    from holophyte.config.project import Project
    from ticket_template import H1_RE, H2_RE
    try:
        compiled = patterns(Project.locate(Path(repo).resolve(), adopt=False))
    except SystemExit as refused:
        return [str(refused)]
    problems, section = [], "the preamble"
    for number, line in enumerate(text.split("\n"), 1):
        heading = H2_RE.match(line)
        if heading:
            section = heading.group(1).strip()
        elif H1_RE.match(line):
            section = "the title"
        problems += [f"section {section!r}, line {number}, holds text the"
                     f" project does not publish ({KEY} #{index})"
                     for index in _search(compiled, line)]
    return problems


def record(conn, run_id, leaks):
    if conn is None or run_id is None or not leaks:
        return
    store.record_event(conn, run_id, "private_match", "private text refused: "
                       + "; ".join(leak.describe() for leak in leaks))


def record_refusal(conn, run_id, failure):
    if isinstance(failure, PrivateMatch):
        record(conn, run_id, failure.leaks)
