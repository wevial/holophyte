import copy
import subprocess
from pathlib import PurePosixPath
from typing import NamedTuple

from holophyte.loop.gates import sh
from store.enums import ScoutKind

TEXT = (lambda value: isinstance(value, str) and bool(value.strip()),
        "non-empty text")
ANY_TEXT = (lambda value: isinstance(value, str), "text")
LINE = (lambda value: type(value) is int and value > 0, "a positive integer")
URL = (lambda value: isinstance(value, str)
       and value.startswith(("http://", "https://")),
       "a URL starting http:// or https://")


def one_of(*choices):
    return (lambda value: value in choices, f"one of {', '.join(choices)}")


class Each(NamedTuple):
    item: object
    nonempty: bool = False


EVIDENCE_SHAPES = {
    "file": {"file": TEXT, "line": LINE},
    "command": {"command": TEXT, "output": ANY_TEXT},
    "url": {"url": URL},
}


def evidence_problems(item, path):
    if not isinstance(item, dict):
        return [f"{path}: must be an object"]
    kinds = [kind for kind in EVIDENCE_SHAPES if kind in item]
    if len(kinds) != 1:
        return [f"{path}: must hold exactly one of file, command and url"]
    return walk(item, EVIDENCE_SHAPES[kinds[0]], path)


HYPOTHESIS = {"hypothesis": TEXT, "separates": TEXT,
              "verdict": one_of("supported", "ruled_out", "open")}
FIELDS = {
    "summary": TEXT,
    "findings": Each({"title": TEXT,
                      "status": one_of("shown", "likely", "unconfirmed"),
                      "evidence": Each(evidence_problems, nonempty=True)}),
    "actions": Each({"title": TEXT, "why": TEXT, "body": TEXT,
                     "kind": one_of("fix", "test", "investigate", "decide")}),
}


def reproduction_problems(value, path):
    problems = walk(value, {"level": one_of("test", "real_tool",
                                            "not_reproduced"),
                            "hypotheses": Each(HYPOTHESIS)}, path)
    if (isinstance(value, dict) and value.get("level") == "not_reproduced"
            and value.get("hypotheses") == []):
        problems.append(f"{path}.hypotheses: must hold at least one"
                        " hypothesis at level not_reproduced")
    return problems


def walk(value, rule, path):
    if callable(rule):
        return rule(value, path)
    if isinstance(rule, dict):
        if not isinstance(value, dict):
            return [f"{path}: must be an object"]
        return [problem for key, field in rule.items()
                for problem in walk(value.get(key), field,
                                    f"{path}.{key}" if path else key)]
    if isinstance(rule, Each):
        if not isinstance(value, list) or (rule.nonempty and not value):
            return [f"{path}: must be a {'non-empty ' * rule.nonempty}list"]
        return [problem for index, item in enumerate(value)
                for problem in walk(item, rule.item, f"{path}[{index}]")]
    check, must = rule
    return [] if check(value) else [f"{path}: must be {must}"]


def report_problems(report, kind):
    """Each problem opens with the field's path; none means the report passes."""
    fields = dict(FIELDS)
    if kind == ScoutKind.BUG.value or "reproduction" in report:
        fields["reproduction"] = reproduction_problems
    return walk(report, fields, "")


def line_count(repo, spec):
    try:
        if sh(["git", "cat-file", "-t", spec], cwd=repo) != "blob":
            return None
    except RuntimeError:
        return None
    content = subprocess.run(["git", "show", spec], cwd=repo,
                             capture_output=True, check=True).stdout
    lines = content.count(b"\n")
    return lines + 1 if content and not content.endswith(b"\n") else lines


def resolution(repo, base_sha, path, line, counts):
    parts = PurePosixPath(path)
    if parts.is_absolute() or ".." in parts.parts:
        return {"verified": False, "why": "outside the repository"}
    if parts not in counts:
        counts[parts] = line_count(repo, f"{base_sha}:{parts}")
    if counts[parts] is None:
        return {"verified": False,
                "why": f"not in the base commit {base_sha[:12]}"}
    if line > counts[parts]:
        return {"verified": False,
                "why": f"past the end of the file ({counts[parts]} lines)"}
    return {"verified": True}


def resolve_evidence(repo, base_sha, report):
    resolved, counts = copy.deepcopy(report), {}
    for finding in resolved.get("findings", ()):
        for item in finding.get("evidence", ()):
            if "file" in item:
                item.update(resolution(repo, base_sha, item["file"],
                                       item["line"], counts))
    return resolved
