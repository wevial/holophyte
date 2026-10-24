"""The scout's GitHub reads: GET alone, on an allow-list of its own repository."""
import io
import json
import re
import sqlite3
import zipfile
import zlib
from contextlib import closing
from urllib.parse import unquote
from xml.etree import ElementTree

import store
from holophyte.loop.gates import InfraFailure
from holophyte.pr import github
from store.scouts import ScoutRefused, record_scout_event

GITHUB_ANSWER_CHARS = 40000
GITHUB_ARTIFACT_BYTES = 50 * 1024 * 1024
GITHUB_TEST_ROWS = 300
OK, FAILED, REFUSED = "ok", "failed", "refused"
ALLOWED = re.compile(
    r"(actions/runs(/\d+(/jobs|/attempts/\d+/jobs|/timing|/artifacts)?)?"
    r"|actions/jobs/\d+|actions/workflows(/[^/]+/runs)?"
    r"|pulls(/\d+(/files)?)?|commits/[^/]+/(check-runs|status)"
    r"|issues/\d+(/comments)?|contents/.+)")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
UNPRINTABLE = re.compile(r"[\x00-\x20\x7f]")
PLAYWRIGHT_STATUS = {"expected": "passed", "unexpected": "failed",
                     "flaky": "flaky", "skipped": "skipped"}


class Refused(Exception):
    pass


class Answer:
    def __init__(self, text, status=OK):
        self.text, self.status = text, status

    @property
    def error(self):
        return self.status != OK

    @property
    def size(self):
        return len(self.text.encode())


def origin_repo(project):
    pull = github._origin_pull(project)
    if pull is None or not pull.owner or not pull.name:
        return None
    return pull


def guarded(repo, path, method="GET"):
    """The path GitHub is asked for, or `Refused` naming the rule it breaks."""
    if method != "GET":
        raise Refused(f"only GET is allowed, not {method}")
    path = path.strip()
    if path.startswith("//"):
        raise Refused("a path may not hold a scheme or a host")
    path = path.lstrip("/")
    route = path.partition("?")[0]
    if UNPRINTABLE.search(path):
        raise Refused("a path may not hold a space or a control character")
    if ".." in unquote(path):
        raise Refused("a path may not hold '..'")
    if re.search(r"%(2f|5c)", route, re.IGNORECASE):
        raise Refused("a path may not hold an encoded slash")
    if SCHEME.match(path) or "//" in route or "." in route.split("/")[0]:
        raise Refused("a path may not hold a scheme or a host")
    if route.split("/")[0] == "graphql":
        raise Refused("graphql is not allowed; only REST reads are")
    prefix = f"repos/{repo.owner}/{repo.name}/"
    if not route.lower().startswith(prefix.lower()):
        raise Refused(f"the path must be under {prefix}")
    if not ALLOWED.fullmatch(route[len(prefix):].rstrip("/")):
        raise Refused(f"{route} is not on the allow-list of reads")
    return path


def trimmed(value):
    if isinstance(value, dict):
        return {key: trimmed(each) for key, each in value.items()
                if key not in ("node_id", "url")
                and (key == "html_url" or not key.endswith("_url"))}
    if isinstance(value, list):
        return [trimmed(each) for each in value]
    return value


def capped(text):
    if len(text) <= GITHUB_ANSWER_CHARS:
        return text
    return (f"{text[:GITHUB_ANSWER_CHARS]}\n[holo2] truncated: "
            f"{GITHUB_ANSWER_CHARS} of {len(text)} characters shown")


def github_get(project, repo, path, method="GET"):
    try:
        path = guarded(repo, path, method)
    except Refused as refused:
        return Answer(f"[holo2] github_get refused: {refused}", REFUSED)
    try:
        answer = github._call(project, repo.host, "GET", path, None)
    except (InfraFailure, OSError) as failure:
        return Answer(f"[holo2] github_get failed: {failure}", FAILED)
    return Answer(capped(json.dumps(trimmed(answer), ensure_ascii=False)))


def playwright_rows(report, suites=None, titles=()):
    for suite in report.get("suites", []) if suites is None else suites:
        title = suite.get("title", "")
        within = titles if title in ("", suite.get("file")) else (*titles,
                                                                    title)
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                results = test.get("results") or [{}]
                yield {"title": " › ".join((*within, spec.get("title", ""))),
                       "file": spec.get("file", suite.get("file", "")),
                       "duration_ms": results[-1].get("duration", 0),
                       "status": PLAYWRIGHT_STATUS.get(test.get("status"),
                                                       "failed"),
                       "retries": len(results) - 1}
        yield from playwright_rows(report, suite.get("suites", []), within)


def junit_status(case):
    if case.find("failure") is not None or case.find("error") is not None:
        return "failed"
    return "skipped" if case.find("skipped") is not None else "passed"


def junit_rows(root):
    for case in root.iter("testcase"):
        try:
            seconds = float(case.get("time") or 0)
        except ValueError:
            seconds = 0.0
        yield {"title": case.get("name", ""),
               "file": case.get("file") or case.get("classname", ""),
               "duration_ms": round(seconds * 1000),
               "status": junit_status(case), "retries": 0}


def member_rows(name, data):
    if name.endswith(".xml"):
        try:
            root = ElementTree.fromstring(data)
        except ElementTree.ParseError:
            return []
        if root.tag in ("testsuites", "testsuite"):
            return list(junit_rows(root))
    elif name.endswith(".json"):
        try:
            report = json.loads(data)
        except ValueError:
            return []
        if isinstance(report, dict) and isinstance(report.get("suites"), list):
            return list(playwright_rows(report))
    return []


def zip_rows(data):
    rows = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            if member.file_size > GITHUB_ARTIFACT_BYTES:
                continue
            try:
                data = archive.read(member)
            except (RuntimeError, NotImplementedError, zlib.error, EOFError,
                    zipfile.BadZipFile):
                continue
            rows += member_rows(member.filename, data)
    return rows


def listed_artifacts(project, repo, run):
    listed, page = [], 1
    while True:
        path = (f"repos/{repo.owner}/{repo.name}/actions/runs/{run}/artifacts"
                f"?per_page=100{f'&page={page}' if page > 1 else ''}")
        found = github._call(project, repo.host, "GET", guarded(repo, path),
                             None).get("artifacts") or []
        listed += found
        if len(found) < 100:
            return listed
        page += 1


def run_artifacts(project, repo, run, artifact):
    listed = listed_artifacts(project, repo, run)
    chosen = [each for each in listed if not each.get("expired")
              and (artifact is None or each.get("name") == artifact)]
    large = [each["name"] for each in chosen
             if each.get("size_in_bytes", 0) > GITHUB_ARTIFACT_BYTES]
    if artifact is not None and large:
        raise Refused(f"artifact {artifact} is larger than "
                      f"{GITHUB_ARTIFACT_BYTES} bytes")
    return [each for each in chosen if each["name"] not in large], large


def tests_answer(names, rows, large):
    rows.sort(key=lambda row: -row["duration_ms"])
    answer = {"artifacts": names, "total": len(rows),
              "failed": sum(row["status"] == "failed" for row in rows),
              "flaky": sum(row["status"] == "flaky" for row in rows),
              "rows": rows[:GITHUB_TEST_ROWS]}
    if large:
        answer["refused"] = large
    return capped(json.dumps(answer, ensure_ascii=False))


def github_tests(project, repo, run, artifact=None):
    try:
        artifacts, large = run_artifacts(project, repo, run, artifact)
        rows = []
        for each in artifacts:
            rows += zip_rows(github.artifact_zip(project, repo, each["id"]))
    except Refused as refused:
        return Answer(f"[holo2] github_tests refused: {refused}", REFUSED)
    except (InfraFailure, OSError, zipfile.BadZipFile, KeyError,
            AttributeError) as failure:
        return Answer(f"[holo2] github_tests failed: {failure}", FAILED)
    names = [each["name"] for each in artifacts]
    return Answer(tests_answer(names, rows, large))


def record_call(project, scout_id, tool, path, answer):
    if not project.store_path.exists():
        return
    with closing(store.open(str(project.store_path))) as conn:
        try:
            record_scout_event(
                conn, scout_id, "scout_github",
                f"github {tool} {path}: {answer.status}, "
                f"{answer.size} bytes", level="detail",
                payload=json.dumps({"tool": tool, "path": path,
                                    "status": answer.status,
                                    "bytes": answer.size}))
        except (ScoutRefused, ValueError, sqlite3.Error):
            pass
