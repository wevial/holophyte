import json
import subprocess
from pathlib import Path

import store.read
from holophyte.config.config_tables import report_config
from holophyte.loop.gates import sh
from holophyte.review.reply_parsing import BLOCK_BREAK_RE

FINDINGS_WINDOW = 25
# Everything above the marker is frozen pre-store history, reproduced unchanged.
FINDINGS_MARKER = "<!-- store-rendered below -->"
FINDINGS_ARCHIVE = "[{n} earlier entries in holophyte.db — query runs/reviewRounds]"
FINDING_LINE_CHARS = 160


STAMP_UNREADABLE = "(unreadable timestamp)"


def _ms(value):
    import math
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return value


def _stamp(ms):
    from datetime import datetime, timezone
    ms = _ms(ms)
    if ms is None:
        return STAMP_UNREADABLE
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return STAMP_UNREADABLE


def _entry(at, ticket, lines):
    return "\n".join([f"## {_stamp(at)} — {ticket}", *lines])


def _gist(text):
    gist = " ".join(str(text).split())
    if len(gist) > FINDING_LINE_CHARS:
        gist = gist[:FINDING_LINE_CHARS].rstrip() + "…"
    return gist


def decode_findings(text):
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return None
    return decoded if isinstance(decoded, list) else None


def finding_line(finding):
    if (not isinstance(finding, dict) or "path" not in finding
            or "severity" not in finding):
        return f"- (malformed finding) {_gist(repr(finding))}"
    where = str(finding["path"])
    if finding.get("line"):
        where = f"{where}:{finding['line']}"
    gist = BLOCK_BREAK_RE.sub("", _gist(finding.get("message", "")), count=1)
    return f"- {where} [{finding['severity']}] {gist}".rstrip()


def round_at(row):
    return row.endedAt if row.endedAt is not None else row.startedAt


def round_entry(row):
    at, ticket, number = round_at(row), row.linearIdentifier, row.round
    verdict, model = row.verdict, row.reviewerModel
    results, findings = row.verificationResults, row.findings
    results = decode_findings(results)
    verify = ""
    if results is None or not all(isinstance(r, dict) for r in results):
        verify = " · verify unreadable"
    elif results:
        verify = (" · verify "
                  + ("passed" if all(r.get("exitCode") == 0 for r in results)
                     else "failed"))
    raw_findings, findings = findings, decode_findings(findings)
    lines = [f"Round {number}: {verdict} · reviewer {model}{verify}"]
    if findings is None:
        lines.append(f"Findings: unparseable — {_gist(raw_findings)}")
    elif findings:
        lines.append(f"Findings ({len(findings)}):")
        lines.extend(finding_line(finding) for finding in findings)
    return _entry(at, ticket, lines)


def run_entry(row):
    at, ticket, outcome = row.endedAt, row.linearIdentifier, row.outcome
    reason, branch, started = row.outcomeReason, row.branch, row.startedAt
    time_box, rounds = row.timeBoxMs, row.reviewRoundCount
    if outcome == "merged":
        sha = row.mergeSha
        head = ("MERGED to main"
                + (f" as {sha[:7]}" if sha else "")
                + (f" (branch {branch} deleted)" if branch else "") + ".")
    else:
        head = (outcome or "ended").upper()
        head += f": {' '.join(reason.split())}" if reason else "."
    # Byte-stable: a burndown script greps this line.
    estimate = f"{time_box // 60000} min" if _ms(time_box) else "n/a"
    ended, started = _ms(at), _ms(started)
    actual = ("n/a" if ended is None or started is None
              else f"{(ended - started) / 60000:.1f} min")
    return _entry(at, ticket, [
        head,
        f"actual: {actual} · estimate: {estimate} · rounds: {rounds}",
    ])


def findings_entries(conn):
    rounds = store.read.review_rounds(conn)
    runs = store.read.ended_runs(conn)
    entries = [(round_at(row), "round", row.id, round_entry(row))
               for row in rounds]
    entries += [(row.endedAt, "run", row.id, run_entry(row)) for row in runs]
    # An unreadable stamp sorts ahead of every dated one, never compared with one.
    def _order(entry):
        at = _ms(entry[0])
        return (at is not None, at or 0, entry[1], entry[2])
    entries.sort(key=_order)
    return [entry[3] for entry in entries]


def render_findings(conn, preamble=""):
    entries = findings_entries(conn)
    hidden = max(0, len(entries) - FINDINGS_WINDOW)
    blocks = [FINDINGS_ARCHIVE.format(n=hidden)] if hidden else []
    blocks += entries[hidden:]
    # Padding the preamble keeps a re-render idempotent.
    if preamble and not preamble.endswith("\n\n"):
        preamble += "\n" if preamble.endswith("\n") else "\n\n"
    body = "\n\n".join(blocks)
    return preamble + FINDINGS_MARKER + "\n" + (f"\n{body}\n" if body else "")


def frozen_preamble(text):
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip() == FINDINGS_MARKER:
            return "".join(lines[:i])
    return text


def write_findings(target, conn, path=None):
    path = Path(path) if path else target.path / "FINDINGS.md"
    existing = path.read_text() if path.exists() else ""
    path.write_text(render_findings(conn, frozen_preamble(existing)))
    return path


def findings_off(target):
    return report_config(target).findings != "repo"


def commit_findings(target, message):
    if findings_off(target):
        return False
    r = subprocess.run(["git", "status", "--porcelain", "FINDINGS.md"],
                       cwd=target.path, capture_output=True, text=True)
    if not r.stdout.strip():
        return False
    sh(["git", "add", "FINDINGS.md"], target.path)
    sh(["git", "commit", "-m", message], target.path)
    return True


def refresh_findings(target, conn):
    if conn is None or findings_off(target):
        return
    write_findings(target, conn)
