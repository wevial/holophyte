import json
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import NamedTuple

from holophyte.loop.gates import sh

SHOWN = 3
ASK_CHARS = 200
FINDINGS_SHOWN = 8
DONE = "done"
EXTENSION = re.compile(r"\.[A-Za-z0-9]+$")
LINE_TAIL = re.compile(r":[0-9]+(?:[-:][0-9]+)?$")
EDGES = "`'\"()[]{}<>,;!?*"
INSTRUCTION = (
    "Earlier scouts of this project read the same paths. Their evidence is "
    "at their own base commit, so a finding may no longer hold. Judge each "
    "finding listed below at this scout's base commit, and record each "
    "judgement in the report's \"prior\" list as {\"scout\": N, \"finding\": "
    "I, \"verdict\": \"holds\", \"fixed\" or \"unchecked\", \"why\": TEXT}, "
    "N being the scout's number and I the finding's.")


class Prior(NamedTuple):
    scout: int
    kind: str
    ask: str
    base_sha: str
    at: int
    report: dict
    matched: list


def cleaned(path):
    if not isinstance(path, str):
        return None
    path = path.strip()
    while path.startswith("./"):
        path = path[2:]
    path = path.rstrip("/")
    parts = PurePosixPath(path)
    if not path or path == "." or parts.is_absolute() or ".." in parts.parts:
        return None
    return str(parts)


def word_path(word):
    word = LINE_TAIL.sub("", word.strip(EDGES).rstrip(".:"))
    if "/" not in word and not EXTENSION.search(word):
        return None
    return cleaned(word)


def exists(repo, base_sha, path):
    try:
        sh(["git", "cat-file", "-e", f"{base_sha}:{path}"], cwd=repo)
    except RuntimeError:
        return False
    return True


def scout_paths(repo, scout, pack_paths):
    words = " ".join([scout.ask, *scout.links]).split()
    named = [path for path in map(word_path, words) if path is not None]
    named = [path for path in dict.fromkeys(named)
             if scout.base_sha and exists(repo, scout.base_sha, path)]
    packed = [path for path in map(cleaned, pack_paths or ()) if path]
    return list(dict.fromkeys(named + packed))


def overlap(theirs, ours):
    a, b = PurePosixPath(theirs).parts, PurePosixPath(ours).parts
    if a[:len(b)] == b:
        return theirs
    if b[:len(a)] == a:
        return ours
    return None


def cited_paths(conn, scout_id, report):
    cited = [item.get("file") for finding in report.get("findings") or ()
             for item in finding.get("evidence") or () if "file" in item]
    for (paths,) in conn.execute("SELECT paths FROM scoutTasks"
                                 " WHERE scoutId = ? AND pass = 1",
                                 (scout_id,)):
        cited.extend(json.loads(paths))
    return [path for path in map(cleaned, cited) if path]


def matched_paths(cited, paths):
    found = (overlap(theirs, ours) for theirs in cited for ours in paths)
    return list(dict.fromkeys(path for path in found if path is not None))


def prior_reports(conn, repo, scout, paths):
    rows = conn.execute(
        "SELECT s.id, s.kind, s.ask, s.baseSha, r.at, r.report FROM scouts s"
        " JOIN scoutReports r ON r.scoutId = s.id AND r.pass = 1"
        " WHERE s.projectId = ? AND s.id < ? AND s.phase = ?"
        " ORDER BY r.at DESC, s.id DESC",
        (scout.project_id, scout.id, DONE))
    priors = []
    for scout_id, kind, ask, base_sha, at, report in rows:
        report = json.loads(report)
        matched = matched_paths(cited_paths(conn, scout_id, report), paths)
        if matched:
            priors.append(Prior(scout_id, kind, ask, base_sha or "", at,
                                report, matched))
        if len(priors) == SHOWN:
            break
    return priors


def is_commit(repo, sha):
    try:
        sh(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo)
    except RuntimeError:
        return False
    return bool(sha)


def changed_line(repo, prior, base_sha):
    if not is_commit(repo, prior.base_sha):
        return (f"Changed since its base: its base {prior.base_sha[:12]} is"
                " not in this repository")
    changed = sh(["git", "diff", "--name-only", prior.base_sha, base_sha,
                  "--", *prior.matched], cwd=repo).split()
    return ("Changed since its base: "
            + (", ".join(changed) or "none of its paths changed"))


def evidence_text(item):
    where = f"{item['file']}:{item['line']}"
    if item.get("verified") is False:
        where += f" (unresolved: {item.get('why')})"
    return where


def finding_line(number, finding):
    cited = [evidence_text(item) for item in finding.get("evidence") or ()
             if "file" in item]
    return (f"{number}. [{finding.get('status')}] {finding.get('title')}"
            + (f" at {', '.join(cited)}" if cited else ""))


def entry(repo, prior, base_sha):
    day = datetime.fromtimestamp(prior.at / 1000, timezone.utc)
    findings = prior.report.get("findings") or []
    lines = [f"Scout {prior.scout} ({prior.kind}, base {prior.base_sha[:12]},"
             f" {day:%Y-%m-%d}): {' '.join(prior.ask.split())[:ASK_CHARS]}",
             f"Summary: {prior.report.get('summary')}",
             *(finding_line(number, finding) for number, finding
               in enumerate(findings[:FINDINGS_SHOWN], 1))]
    if not findings:
        lines.append("No findings.")
    elif len(findings) > FINDINGS_SHOWN:
        lines.append(f"({len(findings) - FINDINGS_SHOWN} more findings not"
                     " shown)")
    return "\n".join(lines + [changed_line(repo, prior, base_sha)])


def offered_findings(priors):
    return {prior.scout: min(len(prior.report.get("findings") or ()),
                             FINDINGS_SHOWN) for prior in priors}


def prior_section(repo, priors, base_sha):
    if not priors:
        return ""
    entries = "\n\n".join(entry(repo, prior, base_sha) for prior in priors)
    return f"## Prior reports\n\n{INSTRUCTION}\n\n{entries}\n\n"
