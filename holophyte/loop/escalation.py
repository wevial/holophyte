import json
import re

import store
from holophyte.loop.implementer_profile import (
    ESCALATION_NOTE,
    escalated_from,
    latest_profile,
)
from holophyte.loop.steer_cap import ROUND_CAP_REASON
from holophyte.redact import safe_print as print

KINDS = ("verify", "fix_no_progress")
CHOICE_RE = re.compile(
    r"^[ \t>*`_-]*ESCALATION:[*`_ \t]*(build|restart)(?![\w|])[*`_ \t]*"
    r"(?:[-—–:][ \t]*)?(.*)$", re.IGNORECASE | re.MULTILINE)


def escalate(conn, run_id, ticket_id):
    row = latest_profile(conn, run_id)
    profile = row and row[1]["profile"]
    kind, reason = conn.execute(
        "SELECT failureKind, outcomeReason FROM runs WHERE id = ?",
        (run_id,)).fetchone()
    if profile is None or not (
            kind in KINDS or (reason or "").startswith(ROUND_CAP_REASON)):
        return
    why = held_back(conn, run_id, ticket_id)
    if why is None:
        note = (f"{ESCALATION_NOTE} implementer profile {profile} failed"
                f" ({reason}); requeued once on the default implementer")
        try:
            store.requeue(conn, ticket_id, note, source="factory")
            why = "requeued"
        except store.RequeueRefused as refused:
            why = f"requeue refused: {refused}"
    summary = f"{ESCALATION_NOTE} {profile} {kind}: {why}"
    store.record_event(conn, run_id, "escalation", summary, level="detail",
                       payload=json.dumps({
                           "profile": profile, "failure_kind": kind,
                           "requeued": why == "requeued", "why": why}))
    print(f"[holo2] {summary}")


def held_back(conn, run_id, ticket_id):
    if escalated_from(conn, run_id) is not None:
        return "already escalated"
    if conn.execute("SELECT 1 FROM runEvents WHERE runId = ?"
                    " AND kind = 'steer_cap_park'", (run_id,)).fetchone():
        return "parked for the operator"
    return None


def brief(conn, run_id, branch, head):
    earlier = escalated_from(conn, run_id)
    if earlier is None or not earlier["last"]:
        return ""
    return (
        f"The factory escalated this ticket to you: run {earlier['run']} on "
        f"the implementer profile {earlier['profile']} failed with: "
        f"{earlier['reason']}\n"
        f"The branch {branch} holds that run's commits at its head, "
        f"{head[:12]}. Decide whether to build on them or restart from main. "
        "To restart, first commit a change that returns the tree to main's, "
        "then do the new work on top; never reset, rebase or otherwise "
        "rewrite the branch's history, because the factory only "
        "fast-forwards it. End your reply with one line naming your choice:\n"
        "ESCALATION: build — reason\n"
        "or\n"
        "ESCALATION: restart — reason\n\n")


def record_choice(conn, run_id, reply):
    earlier = escalated_from(conn, run_id)
    if earlier is None or not earlier["last"]:
        return
    found = CHOICE_RE.findall(reply or "")
    choice, reason = ((found[-1][0].lower(), found[-1][1].strip()) if found
                      else ("undeclared", None))
    store.record_event(conn, run_id, "escalation_choice",
                       f"escalation choice: {choice}", level="detail",
                       payload=json.dumps({"choice": choice, "reason": reason}))
