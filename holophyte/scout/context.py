import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime, timezone
from typing import NamedTuple

import store.read
from holophyte.host.supervisor import SWEEP_EVENT
from holophyte.redact import known_secrets, outbound
from holophyte.review.findings import decode_findings
from holophyte.serve.serve_findings import (
    CONTRADICTION,
    requeue_note,
    stop_kind,
    stopped_criteria,
)
from holophyte.serve.serve_runs import run_detail, run_files, run_ledger
from holophyte.serve.views import ticket_detail

PACK_CAP = 24_000
CUT_LINE = f"(context cut at {PACK_CAP} characters)"
LEDGER_SHOWN = 20
ENTRY_CHARS = 600
TAIL_CHARS = 2_000
MESSAGE_CHARS = 300
FINDINGS_SHOWN = 20
BODY_CHARS = 12_000


class ContextPack(NamedTuple):
    text: str
    paths: list
    summary: str


class Pack:
    def __init__(self, project):
        self.project = project
        self.lines, self.notes, self.files, self.found = [], [], [], []

    def add(self, *lines):
        self.lines.extend(lines)

    def note(self, part):
        self.notes.append(part)

    def paths(self):
        return list(dict.fromkeys(path for path in self.files + self.found
                                  if path))


def answered(pack, what, read, *args):
    try:
        code, body = read(pack.project, *args)
    except (OSError, RuntimeError, sqlite3.Error, subprocess.SubprocessError,
            KeyError, TypeError, ValueError, AttributeError):
        code = None
    if code == 200:
        return body
    pack.note(f"{what} unreadable")
    return None


def readonly(project):
    return closing(store.read.open_readonly(project.store_path))


def stamp(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%SZ")


def head_line(run):
    kind = f" ({run['failure_kind']})" if run["failure_kind"] else ""
    reason = f": {run['outcome_reason']}" if run["outcome_reason"] else ""
    return (f"Context: run {run['id']} of {run['ticket']} \"{run['title']}\","
            f" {run['outcome'] or run['phase']}{kind}{reason}")


def ledger_part(pack, run_id):
    body = answered(pack, "ledger", run_ledger, str(run_id))
    if body is None:
        return
    entries = body["entries"]
    shown = entries[-LEDGER_SHOWN:]
    pack.note(f"ledger {len(shown)} of {len(entries)}")
    pack.add(f"Ledger ({len(shown)} of {len(entries)}):", *(
        f"- {stamp(e['at'])} {e['kind']}: {(e['text'] or '')[:ENTRY_CHARS]}"
        for e in shown))


def failing_verify(project, run_id):
    with readonly(project) as conn:
        rounds = conn.execute(
            "SELECT round, verificationResults FROM reviewRounds"
            " WHERE runId = ? ORDER BY round DESC", (run_id,)).fetchall()
    for number, results in rounds:
        for result in decode_findings(results) or []:
            if (isinstance(result, dict)
                    and result.get("exitCode") not in (0, None)):
                return number, result
    return None


def verify_part(pack, run_id):
    try:
        failing = failing_verify(pack.project, run_id)
    except (OSError, sqlite3.Error):
        pack.note("verify unreadable")
        return
    if failing is None:
        return
    number, result = failing
    pack.note("verify tail")
    pack.add(f"Verify tail (round {number}): `{result.get('command')}`"
             f" exited {result.get('exitCode')}",
             str(result.get("output") or "")[-TAIL_CHARS:])


def finding_line(finding):
    where = str(finding.get("path") or "(no file)")
    if finding.get("line") is not None:
        where += f":{finding['line']}"
    message = str(finding.get("message") or "")[:MESSAGE_CHARS]
    return f"- {where} [{finding.get('severity')}] {message}"


def findings_part(pack, rounds):
    found = [finding for each in reversed(rounds)
             for finding in each["findings"]][:FINDINGS_SHOWN]
    if not found:
        return
    pack.note(f"{len(found)} findings")
    pack.found.extend(str(f.get("path") or "") for f in found)
    pack.add("Review findings, newest round first:",
             *map(finding_line, found))


def criteria_part(pack, criteria):
    unmet = [c for c in criteria if c["status"] != "met"]
    if unmet:
        pack.add("Criteria the newest judgement left unmet:", *(
            f"- {c['id']} {c['status']}: {c['note']}" for c in unmet))


def files_part(pack, run_id):
    body = answered(pack, "files", run_files, str(run_id))
    if body is None:
        return
    pack.files.extend(f["path"] for f in body["files"])
    pack.note(f"{len(body['files'])} files")
    pack.add("Changed files:", *(f"- {f['path']}" for f in body["files"]))


def run_pack(pack, run_id):
    detail = answered(pack, f"run {run_id}", run_detail, str(run_id))
    if detail is None:
        pack.add(f"Context: run {run_id} could not be read")
        return None
    pack.note(f"run {run_id}")
    pack.add(head_line(detail["run"]))
    ledger_part(pack, run_id)
    verify_part(pack, run_id)
    findings_part(pack, detail["rounds"])
    criteria_part(pack, detail["criteria"])
    files_part(pack, run_id)
    return detail["run"]


def defect_facts(project, run_id):
    with readonly(project) as conn:
        stops = conn.execute(
            "SELECT kind, summary, payload FROM runEvents WHERE runId = ?"
            " AND kind IN (?, ?) ORDER BY seq DESC",
            (run_id, CONTRADICTION, SWEEP_EVENT)).fetchall()
        stop = next(((stop_kind(kind, summary), summary, payload)
                     for kind, summary, payload in stops
                     if stop_kind(kind, summary)), None)
        criteria = (None if stop is None else
                    stopped_criteria(conn, run_id, stop[0], stop[2]))
        claimed = conn.execute(
            "SELECT ticketId, revision FROM runs WHERE id = ?",
            (run_id,)).fetchone()
        bodies = {r.revision: r.body for r in store.read.ticket_revisions(
            conn, claimed[0])} if claimed else {}
        return stop, criteria, requeue_note(conn, run_id), claimed, bodies


def stop_lines(stop, criteria, note):
    if stop is None:
        return ["Ticket defect: no stop on the ticket's wording is recorded"]
    kind, reason, _ = stop
    lines = [f"Ticket defect: {kind} on {', '.join(criteria) or 'none named'}:"
             f" {reason}"]
    return lines + ([f"Requeue note: {note}"] if note else [])


def body_lines(pack, run, claimed, bodies):
    revision = claimed[1] if claimed else None
    if revision is None:
        pack.add("The run's claimed revision is not recorded: an older build"
                 " claimed it.")
    else:
        pack.add(f"Ticket body at claimed revision {revision}:",
                 (bodies.get(revision) or "")[:BODY_CHARS])
    ticket = answered(pack, "ticket", ticket_detail, run["ticket"])
    if ticket is None:
        return
    current = ticket["current"]
    if current and revision is not None and current["revision"] > revision:
        pack.add(f"Newer ticket body at revision {current['revision']}:",
                 (current["body"] or "")[:BODY_CHARS])


def defect_pack(pack, ref):
    pack.note(f"defect {ref}")
    run = run_pack(pack, int(ref))
    try:
        stop, criteria, note, claimed, bodies = defect_facts(pack.project,
                                                             int(ref))
    except (OSError, sqlite3.Error):
        pack.note("defect unreadable")
        return
    pack.add(*stop_lines(stop, criteria, note))
    if run is not None:
        body_lines(pack, run, claimed, bodies)


def ticket_pack(pack, key):
    ticket = answered(pack, f"ticket {key}", ticket_detail, key)
    if ticket is None:
        pack.add(f"Context: ticket {key} could not be read")
        return
    pack.note(f"ticket {key}")
    current = ticket["current"]["revision"] if ticket["current"] else "none"
    pack.add(f"Context: ticket {key} \"{ticket['title']}\", {ticket['status']},"
             f" revision {current}", ticket["body"] or "", "Runs:", *(
                 f"run {r['run']}: {r['outcome'] or r['phase']}"
                 for r in ticket["runs"]))
    ended = next((r["run"] for r in ticket["runs"]
                  if r["ended_ms"] is not None), None)
    if ended is not None:
        run_pack(pack, ended)


def pr_pack(pack, url):
    pack.add(f"Pull request: {url}")
    pack.note(f"pr {url}")
    try:
        with readonly(pack.project) as conn:
            row = conn.execute("SELECT id FROM runs WHERE prUrl = ?"
                               " ORDER BY id DESC LIMIT 1", (url,)).fetchone()
    except (OSError, sqlite3.Error):
        row = None
    if row is None:
        pack.note("no run")
        pack.add("No run of this project is recorded as opening it.")
        return
    run_pack(pack, row[0])


PACKS = {"run": lambda pack, ref: run_pack(pack, int(ref)),
         "defect": defect_pack, "ticket": ticket_pack, "pr": pr_pack}


def capped(text, pack, secrets):
    if len(outbound(text, secrets)) <= PACK_CAP:
        return text
    pack.note(f"cut at {PACK_CAP}")
    end = PACK_CAP - len(CUT_LINE) - 1
    while True:
        line_end = text.rfind("\n", 0, end + 1)
        head = text[:line_end if line_end > 0 else end]
        cut = f"{head}\n{CUT_LINE}"
        over = len(outbound(cut, secrets)) - PACK_CAP
        if over <= 0 or not head:
            return cut
        end = max(len(head) - over, 0)


def context_pack(project, scout):
    if scout.subject_kind is None:
        return None
    pack = Pack(project)
    PACKS[scout.subject_kind](pack, scout.subject_ref)
    text = capped("\n".join(pack.lines), pack,
                  known_secrets(project.config()))
    return ContextPack(text, pack.paths(), "context: " + " · ".join(pack.notes))
