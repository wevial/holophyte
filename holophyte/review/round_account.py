import json
import re
from dataclasses import dataclass

import store
import store.read
from holophyte.review.json_reply import NO_REASON, UNNAMED_WITNESS, kept_line

CRITERION_FINDING = re.compile(r"^CRITERION C(\d+): (.+?) — (.*)")


@dataclass(frozen=True)
class RoundAccount:
    verdict: str | None
    criteria: list
    findings: list


def round_account(recorded):
    if recorded.reply is None:
        findings = json.loads(recorded.findings)
        return RoundAccount(None, text_criteria(findings), findings)
    reply = json.loads(recorded.reply)
    return RoundAccount(reply["verdict"],
                        [entry for entry in map(reply_criterion, reply["criteria"])
                         if entry is not None],
                        reply["findings"])


def recorded_account(conn, run_id, rnd):
    if conn is None or run_id is None:
        return None
    for recorded in store.read.rounds_of(conn, run_id):
        if recorded.round == rnd:
            return round_account(recorded)
    return None


def text_criteria(findings):
    criteria = []
    for finding in findings:
        found = (CRITERION_FINDING.match(finding.get("message", ""))
                 if finding.get("path") == store.CRITERIA_PATH else None)
        if found is not None:
            n = int(found[1])
            criteria.append({"id": f"C{n}", "n": n,
                             "status": found[2].replace(" ", "_"),
                             "note": found[3]})
    return criteria


def reply_criterion(criterion):
    number = re.search(r"\d+", criterion["id"])
    if number is None:
        return None
    n = int(number[0])
    status = criterion["status"]
    reason, witness = kept_line(criterion["reason"]), kept_line(criterion["witness"])
    if status == "met" and not (reason or witness):
        status, reason = "unwitnessed", UNNAMED_WITNESS
    if status != "met" and not reason:
        reason = NO_REASON
    note = "; ".join(filter(None, (reason, witness and f"witness: {witness}")))
    return {"id": f"C{n}", "n": n, "status": status, "note": note}
