from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from time import time
from urllib.parse import parse_qs

import store.read
from holophyte.babysit.thread_findings import normalize_thread
from holophyte.babysit.thread_mentions import bot_author
from holophyte.cli.report import ended_rows, host_label
from holophyte.config.config import budget_scale
from holophyte.config.config_tables import MERGE_KEYS, merge_config
from holophyte.config.project import worktree_path
from holophyte.files import GIT_TIMEOUT, RangeError, git, touched_files
from holophyte.loop.pool_handoff import workers_on_previous_build  # noqa: F401
from holophyte.loop.runs import MAX_ROUNDS
from store.operator_notes import round_notes
from store.working import agent_work, effective_work, verify_work

# A `?`, `#`, `@`, `:` or space would ride into the link as a query,
# fragment or credential.
SEGMENT = r"[^/?#@:\s]+"
REMOTE_SHAPES = (
    re.compile(rf"^https://(?P<host>{SEGMENT})/(?P<owner>{SEGMENT})/"
               rf"(?P<repo>{SEGMENT}?)(?:\.git)?/?$"),
    re.compile(rf"^git@(?P<host>{SEGMENT}):(?P<owner>{SEGMENT})/"
               rf"(?P<repo>{SEGMENT}?)(?:\.git)?/?$"),
)
# Captured as typed, so a non-integer id is 400, not the static-file 404.
RUN_PATH = re.compile(r"^/runs/([^/]+)$")
RUN_FILES_PATH = re.compile(r"^/runs/([^/]+)/files$")
RUN_LEDGER_PATH = re.compile(r"^/runs/([^/]+)/ledger$")
# What `int()` accepts, less its leniencies: whitespace and underscores.
RUN_ID = re.compile(r"^([+-]?)0*(\d+)$")
SQLITE_MAX_INT = 2**63 - 1
# Checked before `int()`, which refuses a string past Python's digit limit.
SQLITE_MAX_DIGITS = len(str(SQLITE_MAX_INT))


def parse_run_id(text):
    match = RUN_ID.match(text)
    if match is None:
        raise ValueError(f"run id must be an integer, got {text!r}")
    sign, digits = match.groups()
    if len(digits) > SQLITE_MAX_DIGITS:
        return None
    run_id = int(sign + digits)
    return run_id if 0 <= run_id <= SQLITE_MAX_INT else None


def no_store(project):
    return {"error": "no store",
            "detail": f"{project.path} has no store yet; nothing has run"
                      " against it on this host",
            "project": str(project.path)}


INTEGER = re.compile(r"-?[0-9]+")


def parse_limit(query, default=None, cap=None):
    values = parse_qs(query, keep_blank_values=True).get("limit")
    if values is None:
        return default
    text = values[-1]
    if not text.isdigit() or int(text) < 1:
        raise ValueError(f"limit must be a positive integer, got {text!r}")
    limit = int(text)
    return limit if cap is None else min(limit, cap)


def parse_since(query):
    values = parse_qs(query, keep_blank_values=True).get("since")
    if values is None:
        raise ValueError("since is required (epoch milliseconds)")
    text = values[-1]
    if not INTEGER.fullmatch(text):
        raise ValueError(f"since must be an integer of epoch milliseconds,"
                         f" got {text!r}")
    return int(text)


def parse_filter(query, name, allowed=None):
    values = parse_qs(query, keep_blank_values=True).get(name)
    if values is None:
        return None
    text = values[-1]
    if allowed is not None and text not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(allowed)},"
                         f" got {text!r}")
    return text


def parse_before(query):
    values = parse_qs(query, keep_blank_values=True).get("before")
    if values is None:
        return None
    text = values[-1]
    if not INTEGER.fullmatch(text):
        raise ValueError(f"before must be an integer run id, got {text!r}")
    return int(text)


def json_host(project, host):
    return None if host is None else host_label(project, host)


def origin_web_url(project):
    try:
        code, out = git(project.path, "remote", "get-url", "origin")
    except (subprocess.TimeoutExpired, OSError):
        return None
    if code != 0:
        return None
    for shape in REMOTE_SHAPES:
        found = shape.match(out.strip())
        if found:
            return "https://{host}/{owner}/{repo}".format(**found.groupdict())
    return None


def commit_url(project, sha, origin):
    """A sha not on `origin/main` would link to a page that does not exist."""
    if not sha or not origin:
        return None
    try:
        code, _ = git(project.path, "merge-base", "--is-ancestor", sha,
                      "origin/main")
    except (subprocess.TimeoutExpired, OSError):
        return None
    return f"{origin}/commit/{sha}" if code == 0 else None


def runs(project, query=""):
    try:
        limit = parse_limit(query)
    except ValueError as bad:
        return 400, {"error": str(bad)}
    if not project.store_path.exists():
        return 503, no_store(project)
    conn = store.read.open_readonly(project.store_path)
    try:
        rows = ended_rows(conn)
        ticket_urls = dict(conn.execute(
            "SELECT linearIdentifier, url FROM tickets"))
    finally:
        conn.close()
    if limit is not None:
        rows = rows[:limit]
    return 200, {
        "rows": [{"ticket": ticket, "ticket_url": ticket_urls.get(ticket),
                  "actual_min": actual, "agent_min": agent,
                  "verify_min": verify,
                  "estimate_min": estimate, "ratio": ratio,
                  "rounds": rounds, "outcome": outcome,
                  "host": json_host(project, host), "ended_ms": ended_at,
                  "merge_sha": merge_sha, "wall_min": wall_min}
                 for ticket, actual, agent, verify, estimate, ratio, rounds,
                 outcome, host, ended_at, merge_sha, wall_min in rows],
        "limit": limit,
    }


SHIPPED_LIMIT = 50
SHIPPED_CAP = 200


def shipped(project, query=""):
    try:
        limit = parse_limit(query, default=SHIPPED_LIMIT, cap=SHIPPED_CAP)
        before = parse_before(query)
        outcome = parse_filter(query, "outcome", ("merged", "all"))
    except ValueError as bad:
        return 400, {"error": str(bad)}
    if not project.store_path.exists():
        return 503, no_store(project)
    conn = store.read.open_readonly(project.store_path)
    try:
        # One past the page tells whether there is a next one.
        runs = store.read.finished_runs(
            conn, limit + 1, before,
            outcomes=None if outcome == "all" else ("merged",))
    finally:
        conn.close()
    more = len(runs) > limit
    runs = runs[:limit]
    origin = origin_web_url(project)
    return 200, {
        "rows": [{"id": run.id, "ticket": run.linearIdentifier,
                  "ticket_url": run.ticketUrl,
                  "title": run.title, "rounds": run.reviewRoundCount,
                  "findings": run.findingCount,
                  "started_ms": run.startedAt, "ended_ms": run.endedAt,
                  "actual_min": (effective_work(run, run.endedAt) / 60000
                                 if run.workingMs is not None else None),
                  "working_ms": effective_work(run, run.endedAt),
                  "agent_ms": agent_work(run, run.endedAt),
                  "verify_ms": verify_work(run, run.endedAt),
                  "wall_min": (run.endedAt - run.startedAt) / 60000,
                  "estimate_min": (run.timeBoxMs / 60000
                                   if run.timeBoxMs else None),
                  "merge_sha": run.mergeSha,
                  "commit_url": commit_url(project, run.mergeSha, origin),
                  "outcome": run.outcome,
                  "outcome_reason": (run.outcomeReason[:400]
                                     if run.outcomeReason is not None else None),
                  "pr_url": run.prUrl,
                  "host": json_host(project, run.host)}
                 for run in runs],
        "next_before": runs[-1].id if more else None,
        "limit": limit,
    }


def locate_run(project, text):
    try:
        run_id = parse_run_id(text)
    except ValueError as error:
        return (400, {"error": str(error)}), None
    if not project.store_path.exists():
        return (503, no_store(project)), None
    # Binding an id past SQLite's INTEGER would raise OverflowError.
    if run_id is None:
        return (404, {"error": "no such run", "run": text}), None
    conn = store.read.open_readonly(project.store_path)
    try:
        run = store.read.run_detail(conn, run_id)
    finally:
        conn.close()
    if run is None:
        return (404, {"error": "no such run", "run": run_id}), None
    return None, run


def run_detail(project, run_id, now=None):
    now = int(time() * 1000) if now is None else now
    failed, run = locate_run(project, run_id)
    if failed is not None:
        return failed
    conn = store.read.open_readonly(project.store_path)
    try:
        rounds = store.read.rounds_of(conn, run.id)
        notes = {r.round: round_notes(conn, run.id, r.round) for r in rounds}
        events = store.read.narrative_events(
            conn, run.id,
            detail_kinds=("implementer_output", "operator_note_consumed"))
    finally:
        conn.close()
    merge = merge_config(project)
    live = run.endedAt is None
    scale = budget_scale(project)
    clock = run.endedAt if not live else now
    return 200, {
        "run": {"id": run.id, "ticket": run.linearIdentifier,
                "ticket_url": run.ticketUrl,
                "title": run.title, "phase": run.phase,
                "attempt": run.attempt, "started_ms": run.startedAt,
                "ended_ms": run.endedAt, "outcome": run.outcome,
                "elapsed_ms": clock - run.startedAt,
                "working_ms": effective_work(run, clock),
                "agent_ms": agent_work(run, clock),
                "verify_ms": verify_work(run, clock),
                "time_box_ms": (int(run.timeBoxMs * scale)
                                if run.timeBoxMs else run.timeBoxMs),
                "branch": run.branch, "host": json_host(project, run.host),
                "heartbeat_age_ms": now - run.lastHeartbeat if live else None,
                "merge_sha": run.mergeSha,
                "commit_url": commit_url(project, run.mergeSha,
                                         origin_web_url(project)),
                "pr_url": run.prUrl, "work_started_ms": run.workStartedAt,
                "verify_started_ms": run.verifyStartedAt,
                "approved_at": run.approvedAt, "approved_by": run.approvedBy,
                "max_rounds": run.reviewRoundCap or MAX_ROUNDS},
        "rounds": [{"round": r.round, "started_ms": r.startedAt,
                    "ended_ms": r.endedAt, "verdict": r.verdict,
                    "reviewer_model": r.reviewerModel,
                    **split_instructions(json.loads(r.findings),
                                         merge.bot_authors + merge.bot_logins),
                    "operator_notes": notes[r.round]}
                   for r in rounds],
        "findings": [{"tone": "advisory", "message": e.summary}
                     for e in events if e.kind == "bot_finding"],
        "events": [{"at": e.at, "kind": e.kind, "summary": e.summary}
                   for e in events],
    }


def split_instructions(findings, bot_logins=MERGE_KEYS["bot_authors"]):
    result = {"findings": [], "instructions": []}
    marker = " -- MENTIONED: ADDRESS: "
    for finding in findings:
        if finding.get("kind") == "instruction":
            if bot_author(finding.get("author", ""), bot_logins):
                finding = dict(finding)
                finding["kind"] = "finding"
                finding.setdefault("message", finding.get("request", ""))
                finding.setdefault("severity", "nit")
                result["findings"].append(finding)
            else:
                result["instructions"].append(finding)
        elif "kind" not in finding and marker in finding.get("message", ""):
            head, request = finding["message"].split(marker, 1)
            location = re.match(r"^- (.*?) @([^:]+):", head)
            path, line = finding.get("path", "(no file)"), finding.get("line")
            author = ""
            if location:
                path, author = location.groups()
                file_line = re.match(r"^(.*):(\d+)$", path)
                if file_line:
                    path, line = file_line[1], int(file_line[2])
            if bot_author(author, bot_logins):
                result["findings"].append(finding)
                continue
            result["instructions"].append(dict(
                kind="instruction", path=path, line=line, author=author,
                request=re.split(r"\nVERDICT:", request, maxsplit=1)[0].strip(),
                url=finding.get("url", "")))
        else:
            result["findings"].append(finding)
    result["findings"] = [normalize_thread(f, bot_logins) for f in result["findings"]]
    return result


def run_ledger(project, run_id):
    failed, run = locate_run(project, run_id)
    if failed is not None:
        return failed
    conn = store.read.open_readonly(project.store_path)
    try:
        entries = store.read.ledger(conn, run.id)
    finally:
        conn.close()
    return 200, {
        "run_id": run.id, "ticket": run.linearIdentifier, "ticket_url": run.ticketUrl,
        "entries": [ledger_entry(e, {}) for e in entries],
    }


def ledger_entry(entry, head):
    body = {**head, "at": entry.at, "kind": entry.kind,
            "source": entry.source, "text": entry.text}
    if entry.kind == "intervention":
        body["cleared"] = entry.cleared
        body["waited_ms"] = entry.waitedMs
    return body


LEDGER_LIMIT = 200
LEDGER_CAP = 1000


def ledger(project, query):
    try:
        since = parse_since(query)
        kind = parse_filter(query, "kind", allowed=store.LEDGER_KINDS)
        ticket = parse_filter(query, "ticket")
        limit = parse_limit(query, default=LEDGER_LIMIT, cap=LEDGER_CAP)
    except ValueError as bad:
        return 400, {"error": str(bad)}
    if not project.store_path.exists():
        return 503, no_store(project)
    conn = store.read.open_readonly(project.store_path)
    try:
        entries = store.read.ledger_since(conn, since, kind=kind,
                                          ticket=ticket, limit=limit,
                                          hide_launch_backoff=True)
        route_rows = (route_down_rows(conn)
                      if ticket is None and kind in (None, "intervention") else [])
        feed = [ledger_entry(e, {"run": e.runId, "ticket": e.ticket})
                for e in entries]
        if ticket is None and kind in (None, "intervention"):
            feed.extend(migration_rows(conn, since, limit, str(project.path)))
        feed.sort(key=lambda row: row["at"], reverse=True)
    finally:
        conn.close()
    return 200, {
        "entries": feed[:limit],
        "active_outages": route_rows, "since": since, "limit": limit,
    }


def migration_rows(conn, since, limit, project_path):
    from holophyte.cli.report import migration_line

    if "note" not in {r[1] for r in conn.execute("PRAGMA table_info(interventions)")}:
        return []
    rows = conn.execute(
        "SELECT note, at FROM interventions WHERE action = 'migrate'"
        " AND note IS NOT NULL"
        " AND at >= ? ORDER BY at DESC, id DESC LIMIT ?", (since, limit)).fetchall()
    return [{"at": at, "run": None, "ticket": None, "kind": "intervention",
             "source": "factory", "action": "migrate", "tone": "neutral",
             "text": migration_line(note, detail["to"]),
             "schema_to": detail["to"], "schema_from": detail["from"],
             "project": project_path,
             "cleared": None, "waited_ms": None} for note, at in rows
            for detail in [json.loads(note)]]


def route_down_rows(conn):
    from store import launch_backoff

    rows = []
    for (project_id,) in conn.execute(
            "SELECT id FROM projects WHERE launchBackoffReason IS NOT NULL"):
        state = launch_backoff.current(conn, project_id)
        started = datetime.fromtimestamp(
            state["since"] / 1000, timezone.utc).strftime("%H:%M")
        rows.append({
            "project": project_id, "run": None, "ticket": None,
            "kind": "route_down", "source": "loop", "at": state["since"],
            "reason": state["reason"],
            "text": f"implementer route down since {started} UTC: {state['reason']}",
        })
    return rows


def run_files(project, run_id):
    failed, run = locate_run(project, run_id)
    if failed is not None:
        return failed
    worktree = worktree_path(project, run.branch) if run.branch else None
    try:
        touched = touched_files(project.path, run.branch, run.mergeSha,
                                worktree=worktree)
    except RangeError as error:
        missing_branch = f"branch {run.branch} no longer exists in the repository"
        if (run.endedAt is None and worktree is not None
                and not worktree.is_dir() and str(error) == missing_branch):
            return 409, {"error": f"branch {run.branch} not cut yet",
                         "run": run.id, "pending": True}
        return 409, {"error": str(error), "run": run.id}
    except subprocess.TimeoutExpired:
        return 504, {"error": f"git did not answer within {GIT_TIMEOUT}s",
                     "run": run.id}
    return 200, {
        "run": run.id, "base": touched.base, "head": touched.head,
        "files": [{"path": f.path, "status": f.status,
                   "added": f.added, "deleted": f.deleted}
                  for f in touched.files],
        "total_added": touched.total_added,
        "total_deleted": touched.total_deleted,
        "truncated": touched.truncated,
    }


def active_routes(project):
    from holophyte.agents.agent_routes import active_fallbacks, safe_command
    from holophyte.config.config import AGENT_CONFIG_KEYS

    fallback = active_fallbacks(project)
    table = {key: safe_command(project, value)
             for key, value in (project.config().get("agents") or {}).items()
             if key in AGENT_CONFIG_KEYS.values() and isinstance(value, (str, dict))}
    return {seat: {"command": fallback.get(seat, table.get(seat)),
                   **({"fallback": fallback[seat]} if seat in fallback else {})}
            for seat in AGENT_CONFIG_KEYS.values() if seat != "critic"}


RUN_TURNS_PATH = re.compile(r"^/runs/([^/]+)/turns$")
RUN_TRANSCRIPT_PATH = re.compile(r"^/runs/([^/]+/turns/[^/]+)/transcript$")


def run_turns(project, text):
    from holophyte.agents.transcripts import turns
    from holophyte.redact import known_secrets, outbound
    failed, run = locate_run(project, text)
    if failed is not None:
        return failed
    conn = store.read.open_readonly(project.store_path)
    try:
        rows = conn.execute(
            "SELECT seq, kind, payload FROM runEvents WHERE runId=? "
            "AND kind IN ('agent_turn', 'agent_session') ORDER BY seq", (run.id,))
        body = json.dumps({"turns": turns(rows)})
        return 200, json.loads(outbound(body, known_secrets(project.config())))
    finally:
        conn.close()


def run_transcript(project, segment):
    from holophyte.agents.transcripts import locate, render
    from holophyte.config.config import serve_config
    from holophyte.redact import known_secrets, outbound
    roots = serve_config(project).transcripts
    missing = (404, {"error": "transcript unavailable"})
    if not roots:
        return missing
    run_id, _, turn_id = segment.split('/')
    code, body = run_turns(project, run_id)
    if code != 200:
        return code, body
    turn = next((t for t in body['turns'] if str(t['id']) == turn_id), None)
    if turn is None:
        return missing
    secrets = known_secrets(project.config())
    for root in roots:
        for kind in ('codex', 'devin'):
            try:
                path = locate(kind, turn['session_id'], root)
                if path is not None:
                    return 200, {"entries": [
                        {"speaker": speaker, "text": outbound(text, secrets)}
                        for speaker, text in render(path)]}
            except (OSError, UnicodeError, RuntimeError):
                continue
    return missing
