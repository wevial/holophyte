import copy
import subprocess
from pathlib import PurePosixPath
from typing import NamedTuple, get_args

import ticket_header
import ticket_template
from holophyte.loop.gates import sh
from holophyte.scout.scout_contract import FindingStatus
from store.enums import ScoutKind


class Rule(NamedTuple):
    check: object
    must: str
    shown: str
    choices: tuple = ()


TEXT = Rule(lambda value: isinstance(value, str) and bool(value.strip()),
            "non-empty text", "TEXT")
PATH = TEXT._replace(shown="PATH")
ANY_TEXT = Rule(lambda value: isinstance(value, str), "text", "TEXT")
LINE = Rule(lambda value: type(value) is int and value > 0,
            "a positive integer", "N")
URL = Rule(lambda value: isinstance(value, str)
           and value.startswith(("http://", "https://")),
           "a URL starting http:// or https://", "URL")


def one_of(*choices):
    return Rule(lambda value: value in choices,
                f"one of {', '.join(choices)}",
                " or ".join(f'"{choice}"' for choice in choices), choices)


class Each(NamedTuple):
    item: object
    nonempty: bool = False


class Either(NamedTuple):
    forms: dict


class Checked(NamedTuple):
    fields: dict
    extra: object


def needs_hypothesis(value, path):
    if (isinstance(value, dict) and value.get("level") == "not_reproduced"
            and value.get("hypotheses") == []):
        return [f"{path}.hypotheses: must hold at least one hypothesis at"
                " level not_reproduced"]
    return []


STATUSES = get_args(FindingStatus)
STORED_STATUSES = {"shown": "confirmed", "likely": "partial",
                   "unverified": "unconfirmed",
                   **{status: status for status in STATUSES}}


def read_status(status):
    return STORED_STATUSES.get(status, "unconfirmed")


KINDS = ("fix", "test", "investigate", "decide")
VERDICTS = ("holds", "fixed", "unchecked")
REVISE = "revise"
DEFECT = "defect"
EVIDENCE = Either({
    "file": {"file": PATH, "line": LINE},
    "command": {"command": TEXT, "output": ANY_TEXT},
    "url": {"url": URL},
})
FIELDS = {
    "summary": TEXT,
    "findings": Each({"title": TEXT,
                      "status": one_of(*STATUSES),
                      "evidence": Each(EVIDENCE, nonempty=True)}),
    "actions": Each({"title": TEXT, "why": TEXT,
                     "kind": one_of(*KINDS, REVISE), "body": TEXT}),
}
PRIOR = Each({"scout": LINE, "finding": LINE, "verdict": one_of(*VERDICTS),
              "why": TEXT})
REPRODUCTION = Checked({
    "level": one_of("test", "real_tool", "not_reproduced"),
    "hypotheses": Each({"hypothesis": TEXT, "separates": TEXT,
                        "verdict": one_of("supported", "ruled_out", "open")}),
}, needs_hypothesis)

HEADER = {"ui_change": '"none"', "estimate": "30", "depends_on": "[]"}
PLACEHOLDERS = {
    "Summary": "<the outcome in one or two sentences>",
    "What / Why / How": "**What:** <the behavior delivered>\n\n"
                        "**Why:** <the problem it solves>\n\n"
                        "**How:** <the technical direction>",
    "In scope": "- <a behavior, surface or file the ticket changes>",
    "Out of scope": "- <adjacent work left out>",
    "Acceptance criteria": "- [ ] Given <...>, when <...>, then <...>.",
    "Verify command(s)": "```\n<a command that exits 0 when the ticket"
                         " holds>\n```",
    "Implementation notes": "- <a constraint or code landmark>",
    "Open questions": "- <each open question, or None alone>",
}


def ticket_skeleton():
    header = [f"{field} = {HEADER[field]}" for field in ticket_header.FIELDS
              if field in HEADER]
    sections = [f"## {name}\n\n{PLACEHOLDERS.get(name, '<...>')}"
                for name in ticket_template.SECTION_ORDER
                if name != ticket_template.ESTIMATE]
    fence = ticket_header.FENCE
    return "\n\n".join([f"{fence}\n" + "\n".join(header) + f"\n{fence}\n"
                          "# <title>", *sections]) + "\n"


def walk_fields(value, rule, path):
    if not isinstance(value, dict):
        return [f"{path}: must be an object"]
    return [problem for key, field in rule.items()
            for problem in walk(value.get(key), field,
                                f"{path}.{key}" if path else key)]


def walk_each(value, rule, path):
    if not isinstance(value, list) or (rule.nonempty and not value):
        return [f"{path}: must be a {'non-empty ' * rule.nonempty}list"]
    return [problem for index, item in enumerate(value)
            for problem in walk(item, rule.item, f"{path}[{index}]")]


def walk_either(value, rule, path):
    if not isinstance(value, dict):
        return [f"{path}: must be an object"]
    keys = [key for key in rule.forms if key in value]
    if len(keys) != 1:
        return [f"{path}: must hold exactly one of {', '.join(rule.forms)}"]
    return walk(value, rule.forms[keys[0]], path)


def walk_checked(value, rule, path):
    return walk(value, rule.fields, path) + rule.extra(value, path)


def walk_rule(value, rule, path):
    return [] if rule.check(value) else [f"{path}: must be {rule.must}"]


WALKERS = {dict: walk_fields, Each: walk_each, Either: walk_either,
           Checked: walk_checked, Rule: walk_rule}
SHOWN = {
    dict: lambda rule: "{" + ", ".join(f'"{key}": {shown(field)}'
                                       for key, field in rule.items()) + "}",
    Each: lambda rule: f"[{shown(rule.item)}, ...]",
    Either: lambda rule: " or ".join(map(shown, rule.forms.values())),
    Checked: lambda rule: shown(rule.fields),
    Rule: lambda rule: rule.shown,
}


def walk(value, rule, path):
    return WALKERS[type(rule)](value, rule, path)


def shown(rule):
    return SHOWN[type(rule)](rule)


def fields_shape(fields):
    return ",\n".join(f' "{key}": {shown(field)}' for key, field in fields.items())


def offers_revise(subject):
    return subject is not None and subject[0] == DEFECT


def offered_fields(subject):
    kinds = KINDS + ((REVISE,) if offers_revise(subject) else ())
    action = dict(FIELDS["actions"].item, kind=one_of(*kinds))
    return dict(FIELDS, actions=Each(action))


def untitled(body):
    if not isinstance(body, str) or not body.strip():
        return False
    return not next(line for line in body.splitlines()
                    if line.strip()).startswith("# ")


def revise_problems(actions, subject):
    revising = [(index, action) for index, action in enumerate(actions)
                if isinstance(action, dict) and action.get("kind") == REVISE]
    problems = []
    for count, (index, action) in enumerate(revising):
        if not offers_revise(subject):
            problems.append(f"actions[{index}].kind: revise needs a"
                            " ticket-defect subject")
        if count:
            problems.append(f"actions[{index}].kind: at most one revise item")
        if untitled(action.get("body")):
            problems.append(f'actions[{index}].body: a revision starts with'
                            ' its "# " title line')
    return problems


def judgement_problems(index, judgement, offered):
    path = f"prior[{index}]"
    if not isinstance(judgement, dict):
        return [f"{path}: must be an object"]
    number, finding = judgement.get("scout"), judgement.get("finding")
    problems = []
    if type(number) is not int or number not in offered:
        problems.append(f"{path}.scout: scout {number} was not offered")
    elif type(finding) is not int or not 1 <= finding <= offered[number]:
        problems.append(f"{path}.finding: scout {number} has no finding"
                        f" {finding}")
    return problems + walk(judgement, {"verdict": one_of(*VERDICTS),
                                       "why": TEXT}, path)


def prior_problems(prior, offered):
    if not isinstance(prior, list):
        return ["prior: must be a list"]
    return [problem for index, judgement in enumerate(prior)
            for problem in judgement_problems(index, judgement, offered or {})]


def report_problems(report, kind, subject=None, *, offered=None):
    """Each problem opens with the field's path; none means the report passes."""
    fields = dict(FIELDS)
    if kind == ScoutKind.BUG.value or "reproduction" in report:
        fields["reproduction"] = REPRODUCTION
    problems = walk(report, fields, "")
    actions = report.get("actions") if isinstance(report, dict) else None
    if isinstance(actions, list):
        problems += revise_problems(actions, subject)
    if isinstance(report, dict) and report.get("prior") is not None:
        problems += prior_problems(report["prior"], offered)
    return problems


def line_count(repo, spec):
    try:
        if sh(["git", "cat-file", "-t", spec], cwd=repo) != "blob":
            return None
    except (RuntimeError, ValueError):
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
                item.pop("why", None)
                item.update(resolution(repo, base_sha, item["file"],
                                       item["line"], counts))
    return resolved
