"""A story's guards: a child runs from its frontier, no board Done closes it."""
import json
import re
import subprocess

from holophyte.config_tables import story_config
from holophyte.redact import safe_print as print
from store.notes import record_note
from store.stories import (
    _frontier_refusal,
    abandon_story,
    park_story,
    story,
    witness_ledger,
)
from store.tickets import walk_ticket

CLOSED_STATES = ("closed", "abandoned")
STANDING_ORDERS = "## Story standing orders"
UPSTREAM = "## Story upstream context: merged dependencies"
WITNESS = "## Story witness this ticket completes"
UPSTREAM_BYTES = 2048
NOTE_ROOM = 96
GIT_TIMEOUT = 30
DRIFT_OPTIONS = ("restore the approved edges",
                 "re-approve the plan as it stands")


def refusal(project, conn, ticket_id):
    """Why the claim must not take `ticket_id` now; None lets it go."""
    found = story(conn, ticket_id)
    if found is None:
        return None
    if found.ticketId == ticket_id:
        return None if found.state == "approved" else (
            f"its story {_identifier(conn, ticket_id)} is {found.state}")
    refused = _frontier_refusal(conn, found, ticket_id,
                                story_config(project).max_parallel)
    if refused is None:
        return None
    kind, reason = refused
    if kind == "drift":
        name = _identifier(conn, ticket_id)
        park_story(conn, found.ticketId, "plan_drift",
                   f"{name}: {reason}", DRIFT_OPTIONS, DRIFT_OPTIONS[0],
                   ticket_id=ticket_id)
        print(f"[holo2] story {_identifier(conn, found.ticketId)} parked on"
              f" plan drift at {name}: {reason}")
    return reason


def open_story(conn, ticket_id):
    found = story(conn, ticket_id)
    if found is None or found.ticketId != ticket_id \
            or found.state in CLOSED_STATES:
        return None
    return found


def not_green(conn, found):
    rows = witness_ledger(conn, found.ticketId)
    sha = rows[-1].mainSha if rows else None
    green = {row.witnessKey for row in witness_ledger(conn, found.ticketId, sha)
             if row.verdict == "green"} if sha else set()
    return sha, [w.key for w in found.witnesses if w.key not in green]


def held_open(conn, ticket_id, identifier, state):
    found = open_story(conn, ticket_id)
    if found is None or state != "completed":
        return False
    sha, keys = not_green(conn, found)
    at = f"at {sha}" if sha else "with no ledger yet"
    text = (f"Linear holds {identifier} completed, but story {identifier}"
            f" closes on its witnesses at main's tip; not yet green {at}:"
            f" {', '.join(keys) or 'none'}")
    record_note(conn, ticket_id, "reconcile", text,
                f"story:{ticket_id}:completed")
    print(f"[holo2] reconcile left story parent {identifier} open: {text}")
    return True


def walk_closed(conn, ticket_id, identifier, to_status):
    if to_status != "abandoned" or open_story(conn, ticket_id) is None:
        walk_ticket(conn, ticket_id, to_status)
        return
    abandon_story(conn, ticket_id, f"Story {identifier} was abandoned: Linear"
                  " holds its parent canceled.", "factory")


def story_brief(project, conn, ticket_id):
    """The story block a child's turns read; empty for a ticket in no story."""
    found = story(conn, ticket_id)
    if found is None or found.ticketId == ticket_id:
        return ""
    sections = []
    if found.standingOrders:
        sections.append("\n".join(
            [STANDING_ORDERS, "", *(f"- {order}" for order in
                                    found.standingOrders)]))
    upstream = _upstream(project, conn, ticket_id)
    if upstream:
        sections.append(upstream)
    sections.extend(_witness(witness) for witness in found.witnesses
                    if witness.completedBy == ticket_id)
    return "".join(f"\n\n{section}" for section in sections)


def _witness(witness):
    fence = "`" * max([3, *(len(run) + 1 for run in
                            re.findall(r"`+", witness.source))])
    ends = witness.source.endswith("\n")
    last = ("ends with a final newline" if ends else
            "ends without a final newline after its last line")
    return (f"{WITNESS}\n\n{witness.key}: {witness.criterion}\n"
            f"Land this approved source at `{witness.file}` byte for byte;"
            f" a changed witness is a review finding. The file {last}.\n\n"
            f"{fence}\n{witness.source}{'' if ends else chr(10)}{fence}")


def _upstream(project, conn, ticket_id):
    lines, files_out, deps_out = [UPSTREAM, ""], 0, 0
    used = len(UPSTREAM.encode()) + 1
    for identifier, title, sha in _merged_dependencies(conn, ticket_id):
        header = f"- {identifier} {title}: merge commit " + (
            sha or "not recorded")
        if used + len(header.encode()) + 1 > UPSTREAM_BYTES - NOTE_ROOM:
            deps_out += 1
            files_out += len(_changed_files(project, sha))
            continue
        lines.append(header)
        used += len(header.encode()) + 1
        for name in _changed_files(project, sha):
            line = f"  - {name}"
            if used + len(line.encode()) + 1 > UPSTREAM_BYTES - NOTE_ROOM:
                files_out += 1
                continue
            lines.append(line)
            used += len(line.encode()) + 1
    if len(lines) == 2 and not (files_out or deps_out):
        return ""
    if files_out or deps_out:
        dropped = f" and {deps_out} dependencies" if deps_out else ""
        lines.append(f"({files_out} changed files{dropped} left out to keep"
                     f" this under {UPSTREAM_BYTES} bytes)")
    return "\n".join(lines)


def _merged_dependencies(conn, ticket_id):
    project_id, depends = conn.execute(
        "SELECT projectId, dependsOn FROM tickets WHERE id = ?",
        (ticket_id,)).fetchone()
    for issue_id in json.loads(depends):
        row = conn.execute(
            "SELECT t.linearIdentifier, t.title, (SELECT r.mergeSha FROM runs r"
            " WHERE r.ticketId = t.id AND r.outcome = 'merged'"
            " ORDER BY r.id DESC LIMIT 1) FROM tickets t"
            " WHERE t.linearIssueId = ? AND t.projectId = ?"
            " AND t.status = 'merged'", (issue_id, project_id)).fetchone()
        if row is not None:
            yield row


def _changed_files(project, sha):
    if not sha:
        return []
    try:
        listed = subprocess.run(
            ["git", "-C", str(project.path), "diff", "--name-only",
             f"{sha}^1", sha], capture_output=True, text=True,
            timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return ["(changed files unknown: git timed out)"]
    if listed.returncode != 0:
        return ["(changed files unknown: git could not read the merge)"]
    return listed.stdout.splitlines()


def _identifier(conn, ticket_id):
    return conn.execute("SELECT linearIdentifier FROM tickets WHERE id = ?",
                        (ticket_id,)).fetchone()[0]
