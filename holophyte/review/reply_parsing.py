import hashlib
import json
import re
import subprocess
from pathlib import Path

import review_runner
from holophyte.review.briefs import REFUTED_CLOSE, REFUTED_OPEN, _changed_files
from store import CRITERIA_PATH

# A CSI opens with ESC-[ or with the single C1 byte \x9b.
ANSI_CSI_RE = re.compile(r"(?:\x1b\[|\x9b)[0-9;?]*[ -/]*[@-~]")


CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


SETEXT_RE = re.compile(r"^ {0,3}(?![-*+>#\s])(.+?)[ \t]*\n {0,3}(?:=+|-+)[ \t]*$", re.M)


ATX_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t#]*$", re.M)


# Caps the kept verdict line, so it cannot carry an entry past its budget.
MAX_VERDICT_CHARS = 200


TRUNCATION_MARKER = "[… truncated]"


def _trailing_verdict(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not lines[-1].startswith("VERDICT:"):
        return None
    verdict = lines[-1]
    if len(verdict) > MAX_VERDICT_CHARS:
        verdict = (verdict[:MAX_VERDICT_CHARS - len(TRUNCATION_MARKER)]
                   + TRUNCATION_MARKER)
    return verdict


def sanitize_findings(text, limit):
    text = ANSI_CSI_RE.sub("", text)
    text = CONTROL_RE.sub("", text)
    text = SETEXT_RE.sub(r"**\1**", text)
    text = ATX_RE.sub(r"**\2**", text)
    if len(text) > limit:
        verdict = _trailing_verdict(text)
        tail = f"\n\n{TRUNCATION_MARKER}" + (f"\n\n{verdict}" if verdict else "")
        # max(): a negative bound would slice from the end.
        head = text[:max(0, limit - len(tail))]
        if text[len(head)] != "\n" and "\n" in head:
            head = head[:head.rindex("\n")]
        text = head.rstrip() + tail
    return text


# Two or more extension letters keep `e.g.` out; the lookbehind, a URL's `//host`.
FINDING_PATH_RE = re.compile(
    r"(?<![\w:/.\-])"
    r"([\w.\-]*/[\w.\-/]*\.\w+|[\w.\-/]*[\w\-]\.[A-Za-z]\w+)(?::(\d+))?")


# The link target, not its text, carries a citation's `:line`.
MD_LINK_TARGET_RE = re.compile(r"\[[^\]\n]*\]\(([^)\s]+)\)")


WORKSPACE_PREFIXES = ("/workspace/", "/home/reviewer/candidate/")


SEVERITY_RE = re.compile(
    r"[\[(]\s*(p0|p1|p2|nit|blocker)\s*[\])]"
    r"|^[\s\-*\d.)]*(p0|p1|p2|nit|blocker)\b\s*[:\-]", re.I | re.M)


DEFAULT_SEVERITY = "p2"


# A prefix: `unparsed_path()` keys each pathless finding apart.
UNPARSED_PATH = "(unparsed)"


MAX_FINDING_CHARS = 2000


BLOCK_BREAK_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def unparsed_path(message):
    # A rewrapped or recapitalized complaint keeps its key.
    normalized = " ".join(message.split()).lower()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"{UNPARSED_PATH}:{digest}"


def finding_message(text):
    return sanitize_findings(text, MAX_FINDING_CHARS).strip()


def raw_finding(reply):
    message = finding_message(reply)
    return {"path": unparsed_path(message), "severity": DEFAULT_SEVERITY,
            "message": message}


def finding_blocks(text):
    blocks, current = [], []
    for line in text.splitlines():
        if not line.strip() or BLOCK_BREAK_RE.match(line):
            if current:
                blocks.append("\n".join(current))
            current = [line] if line.strip() else []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


REFUTED_SECTION_RE = re.compile(
    rf"^[ \t]*{re.escape(REFUTED_OPEN)}[ \t\r]*$.*?"
    rf"^[ \t]*{re.escape(REFUTED_CLOSE)}[ \t\r]*$\n?", re.M | re.S)


def without_refuted(reply):
    return REFUTED_SECTION_RE.sub("", reply)


def parse_findings(reply):
    findings = []
    reply = without_refuted(reply)
    # CRITERION and SCOPE lines cite paths but are accounts, not complaints.
    accounts = SCOPE_LINE_RE.sub("", CRITERION_LINE_RE.sub("", reply))
    for block in finding_blocks(accounts):
        match = FINDING_PATH_RE.search(citation(block))
        listed = BLOCK_BREAK_RE.match(block) is not None
        if match is None and not listed:
            continue
        message = finding_message(block)
        if not message:
            continue
        path, line = ((match.group(1), match.group(2)) if match
                      else (unparsed_path(message), None))
        finding = {"path": path, "severity": finding_severity(block),
                   "message": message}
        if line and int(line) > 0:
            finding["line"] = int(line)
        findings.append(finding)
    return findings or [raw_finding(reply)]


def citation(block):
    link = MD_LINK_TARGET_RE.search(block)
    if link is None:
        return block
    target = link.group(1)
    for prefix in WORKSPACE_PREFIXES:
        if target.startswith(prefix):
            target = target[len(prefix):]
            break
    return target


def finding_severity(block):
    match = SEVERITY_RE.search(block)
    if match is None:
        return DEFAULT_SEVERITY
    marker = (match.group(1) or match.group(2)).lower()
    return "p0" if marker == "blocker" else marker


# The separator is loose; the status word is not: only `met` clears the gate.
CRITERION_LINE_RE = re.compile(
    r"^\s*CRITERION\s+(\d+)\s*:\s*(met|not met|unwitnessed)\b"
    r"\s*(?:[-\u2013\u2014:]+\s*)?(.*?)\s*$", re.I | re.M)


UNWITNESSED_NOTE = "no CRITERION line in the reply"


SCOPE_LINE_RE = re.compile(
    r"^[ \t]*SCOPE[ \t]+(?:`(?P<quoted>[^`\n]+)`|(?P<path>[^\n]+?))"
    r"[ \t]*:[ \t]*(?P<status>needed|tangent)\b"
    r"[ \t]*(?:[-\u2013\u2014:]+[ \t]*)?(?P<note>.*?)[ \t]*$", re.I | re.M)


def criteria_block(reply):
    block = {}
    for match in CRITERION_LINE_RE.finditer(reply):
        block[int(match.group(1))] = (match.group(2).lower(), match.group(3))
    return block


WITNESS_TEST_RE = re.compile(
    r"(?P<path>[\w./-]+\.py)::(?:(?P<cls>\w+)::)?(?P<name>test\w*)"
    r"|(?P<other>[\w./-]+(?:\.(?:test|spec)\.tsx?|\.test\.js|_test\.go))::"
    r"(?:\"(?P<title>(?:[^\"\\\n]|\\.)+)\"|(?P<ident>\w+))"
    r"|(?P<mod>tests(?:\.\w+)+)\.(?P<name2>test\w*)")


MISSING_WITNESS_NOTE = "named test not found: "


def test_references(witness):
    references = []
    for match in WITNESS_TEST_RE.finditer(witness or ""):
        if match.group("path"):
            references.append((match.group("path"), match.group("cls"),
                               match.group("name")))
            continue
        if match.group("other"):
            title = match.group("title")
            if title is not None:
                title = re.sub(r"\\(.)", r"\1", title)
            references.append((match.group("other"), None,
                               title or match.group("ident")))
            continue
        segments = match.group("mod").split(".")
        cls = None
        if len(segments) > 1 and segments[-1][0].isupper():
            cls = segments.pop()
        references.append(("/".join(segments) + ".py", cls,
                           match.group("name2")))
    return references


# One child resolves every class witness; no import reaches the loop's interpreter.
_WITNESS_RESOLVER = r"""
import contextlib
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path("tests").resolve()))
modules = {}
results = []
for path, cls, name in json.loads(sys.argv[1]):
    with contextlib.redirect_stdout(sys.stderr):
        if path not in modules:
            try:
                file = Path(path).resolve()
                module_name = ".".join(file.relative_to(Path("tests").resolve())
                                       .with_suffix("").parts)
                spec = importlib.util.spec_from_file_location(module_name, file)
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                modules[path] = module
            except (Exception, SystemExit):
                modules[path] = None
        module = modules[path]
        found = (callable(getattr(getattr(module, cls, None), name, None))
                 if module is not None else None)
    results.append(found)
print(json.dumps(results))
"""


def _import_witnesses(references, root):
    candidates = [ref for ref in references if ref[1] is not None
                  and (file := _inside(root, ref[0])) is not None
                  and file.is_file()]
    if not candidates:
        return {}
    try:
        result = subprocess.run(
            ["python3", "-c", _WITNESS_RESOLVER, json.dumps(candidates)],
            cwd=root, capture_output=True, text=True, timeout=30, check=True)
        return dict(zip(candidates, json.loads(result.stdout), strict=True))
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def _scan_witness(file, cls, name):
    if file.suffix == ".go" and "/" in name:
        return _scan_go_subtest(file.read_text(errors="replace"), name)
    if file.suffix != ".py":
        text = file.read_text(errors="replace")
        return (None if any(spelling in text for spelling in _quoted_spellings(name))
                else f'no test named "{name}"')
    lines = file.read_text(errors="replace").splitlines()
    if cls is None:
        return (None if any(re.match(rf"\s*def {name}\(", line) for line in lines)
                else f"no def {name}")
    found = _defines_in_class(lines, cls, name)
    if found is None:
        return f"no class {cls}"
    return None if found else f"no def {name} in class {cls}"


def _quoted_spellings(name):
    doubled = name.replace("\\", "\\\\")
    return [name, *(doubled.replace(quote, "\\" + quote) for quote in "\"'`")]


_GO_STRING = r'"((?:[^"\\\n]|\\.)*)"'


_GO_TOKEN = re.compile(
    _GO_STRING + r"|`[^`]*`|'(?:[^'\\\n]|\\.)*'|(//[^\n]*|/\*.*?\*/)", re.S)


def _scan_go_subtest(text, name):
    parent, *children = name.split("/")
    if not re.search(rf"\bfunc {re.escape(parent)}\(", text):
        return f"no func {parent}"
    runs = {re.sub(r"\s", "_", title) for title in
            re.findall(r"\.Run\(" + _GO_STRING, text)}
    runs |= _go_table_titles(text, parent)
    absent = [child for child in children if re.sub(r"\s", "_", child) not in runs]
    return f'no .Run("{absent[0]}") for {parent}' if absent else None


def _go_table_titles(text, parent):
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines)
                 if re.search(rf"\bfunc {re.escape(parent)}\(", line))
    end = next((i for i in range(start + 1, len(lines)) if lines[i] == "}"), len(lines))
    body = "\n".join(lines[start:end + 1])
    tokens = list(_GO_TOKEN.finditer(body))
    code = _GO_TOKEN.sub(lambda token: " " if token.group(2) else token.group(0), body)
    if not re.search(r'\.Run\((?!\s*")', code):
        return set()
    return {re.sub(r"\s", "_", token.group(1)) for token in tokens
            if token.group(1) is not None}


def missing_witnesses(references, root):
    references = [tuple(ref) for ref in references]
    imported = _import_witnesses(references, root)
    missing = []
    for path, cls, name in references:
        spec = f"{path}::{cls}::{name}" if cls else f"{path}::{name}"
        file = _inside(root, path)
        if file is None:
            missing.append(f"{spec} (outside the worktree: {path})")
            continue
        if not file.is_file():
            missing.append(f"{spec} (no file {path})")
            continue
        found = imported.get((path, cls, name))
        if found is not None:
            if not found:
                missing.append(f"{spec} (import: no callable {name} on class {cls})")
            continue
        reason = _scan_witness(file, cls, name)
        if reason:
            route = "textual fallback" if cls else "textual scan"
            missing.append(f"{spec} ({route}: {reason})")
    return missing


# A witness names a test in the worktree, never an absolute path or a `..` escape.
def _inside(root, path):
    base = Path(root).resolve()
    file = (base / path).resolve()
    return file if file == base or base in file.parents else None


def _defines_in_class(lines, cls, name):
    seen = False
    for i, line in enumerate(lines):
        match = re.match(rf"(\s*)class {cls}\b", line)
        if match is None:
            continue
        seen = True
        depth = len(match.group(1))
        for inner in lines[i + 1:]:
            if inner.strip() and len(inner) - len(inner.lstrip()) <= depth:
                break
            if re.match(rf"\s*def {name}\(", inner):
                return True
    return False if seen else None


APPROVAL_RE = re.compile(r"\bapproval\s+at\s+([0-9a-f]{7,40})\b", re.I)


def cited_approval(reply, root, sha):
    cited = {subprocess.run(
        ["git", "rev-parse", "--verify", "-q", f"{value}^{{commit}}"],
        cwd=root, capture_output=True, text=True).stdout.strip()
        for value in APPROVAL_RE.findall(reply)}
    return (cited.pop(), sha) if len(cited) == 1 and "" not in cited else None


def _approval_problems(note, references, approved_range):
    if not approved_range or not re.search(r"\bapproval\s+at\b", note, re.I):
        return None
    hashes = APPROVAL_RE.findall(note)
    if not any(approved_range[0].lower().startswith(value.lower())
               for value in hashes):
        return ["prior approval must name the approved sha"]
    return [] if references else ["prior approval must name a test"]


def _approval_witnesses(note, references, root, approved_range):
    if _approval_problems(note, references, approved_range) != []:
        return []
    approved, sha = approved_range
    changed = {(Path(root) / path).resolve()
               for path in _changed_files(root, approved, sha)}
    return [f"{path} (changed since approval at {approved})"
            for path, _, _ in references if _inside(root, path) in changed]


def criteria_findings(reply, criteria, root=None, *, approved_range=None,
                      scope=()):
    block = criteria_block(reply)
    findings = []
    for n, criterion in enumerate(criteria or (), 1):
        status, note = block.get(n, ("unwitnessed", UNWITNESSED_NOTE))
        stale = []
        if status == "met" and note and root is not None:
            references = test_references(note)
            missing = missing_witnesses(references, root)
            missing += _approval_problems(note, references, approved_range) or []
            stale = _approval_witnesses(note, references, root, approved_range)
            if missing or stale:
                status, note = ("unwitnessed",
                                MISSING_WITNESS_NOTE + "; ".join(missing + stale))
                stale = [] if missing else stale
        if status == "met" and note:
            continue
        if status == "met":
            status, note = "unwitnessed", "met claimed but no test or check named"
        message = (f"CRITERION {n}: {status} \u2014 {note or '(no reason given)'}"
                   f"\n{criterion}")
        finding = {"path": CRITERIA_PATH, "line": n,
                   "severity": DEFAULT_SEVERITY,
                   "message": finding_message(message)}
        findings.append(dict(finding, stale_approval=approved_range[0])
                        if stale else finding)
    return findings + _scope_findings(reply, scope)


def stale_approvals(decision, findings):
    only_stale = all(finding.get("stale_approval") for finding in findings)
    return list(findings) if decision == "APPROVE" and only_stale else []


def _scope_findings(reply, scope):
    answers = {match["quoted"] or match["path"]:
               (match["status"].lower(), match["note"])
               for match in SCOPE_LINE_RE.finditer(reply)}
    findings = []
    for path in scope:
        status, note = answers.get(path, (None, ""))
        if status == "needed":
            continue
        message = (f"SCOPE {path}: tangent \u2014 {note or '(no reason given)'}"
                   if status == "tangent" else
                   f"SCOPE {path}: unaccounted \u2014 the ticket does not name "
                   "this file and the reply gave no SCOPE line for it")
        findings.append({"path": path, "severity": DEFAULT_SEVERITY,
                         "message": finding_message(message)})
    return findings


def _review_reply(target, prompt, wt, base_sha, sha, conn, run_id, *,
                  run_agent=None, review_round=None):
    from holophyte.agents.roles import agent
    from holophyte.board.projection import comment_body

    run_agent = run_agent or agent
    first_reply = ""
    session = {} if review_round is None else {"review_round": review_round}
    for attempt in range(2):
        reply = run_agent(target, "review", prompt, wt, base_sha=base_sha,
                          candidate_sha=sha, conn=conn, run_id=run_id, **session)
        try:
            decision = review_runner.terminal_verdict(reply)
        except review_runner.ReviewBoundaryError:
            decision = "MALFORMED"
        if decision != "MALFORMED":
            break
        from holophyte.loop.stop import stop_if_requested
        stop_if_requested(conn, run_id, "reviewing")
        if attempt == 0:
            first_reply = "first reply (no verdict):\n" + comment_body(reply)
            prompt += ("\n\nYour previous reply had no clean terminal verdict. "
                       "Your reply must end with exactly one line, "
                       "VERDICT: APPROVE or VERDICT: REQUEST_CHANGES, "
                       "and nothing after it.")
    return reply, decision, first_reply


def round_verdict(reply, verdicts):
    try:
        return {"APPROVE": "pass", "REQUEST_CHANGES": "changes_requested",
                "PASS": "pass", "FAIL": "changes_requested"}[
                    review_runner.terminal_verdict(reply, verdicts)]
    except review_runner.ReviewBoundaryError:
        return "error"
