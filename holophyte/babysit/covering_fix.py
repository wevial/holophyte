import json
from dataclasses import dataclass

import store
import store.read
from holophyte.babysit.description_review import description_findings
from holophyte.board.projection import ledger
from holophyte.config.config_tables import merge_config, sweep_config
from holophyte.review.reply_parsing import UNPARSED_PATH

ROUND_KIND = "covering_fix_round"


@dataclass(frozen=True)
class Overlap:
    shared: float
    earlier: int | None


def fix_or_park(project, conn, run_id, provider, task_id, sha, pull, ticket,
                outcome, fix_note):
    """The goal of the next fix turn, or the reason to park instead."""
    verdict, decision, unwitnessed, rnd = outcome
    asked = f"the review of the fix at {sha[:12]} asked for changes"
    findings = f"; not merged. Reviewer findings:\n{verdict}"
    if not unwitnessed and decision != "REQUEST_CHANGES":
        return None, asked + findings
    if fix_note is not None:
        ledger(conn, run_id, task_id, "round",
               f"Babysit review fix allowance after round {rnd}: one fix and"
               " recorded re-review, even if reviewRoundCap is spent.", provider)
        return fix_goal(pull, ticket, verdict, "Operator babysit note",
                        fix_note), None
    cap = merge_config(project).covering_fix_rounds
    if not cap or conn is None or description_findings(verdict, decision,
                                                       unwitnessed):
        return None, asked + findings
    covering = _covering_findings(conn, run_id, rnd)
    threshold = sweep_config(project).review_overlap_threshold
    overlap = repeat_overlap(conn, run_id, rnd, covering)
    if overlap.shared >= threshold:
        return None, (f"the review of the fix at {sha[:12]} repeated its"
                      f" findings (round {rnd} shares {overlap.shared:.2f} of"
                      f" round {overlap.earlier}'s){findings}")
    made = sum(event.kind == ROUND_KIND
               for event in store.read.narrative_events(conn, run_id))
    if made >= cap:
        return None, (f"{asked} and the automatic fix rounds are used up"
                      f" ([merge] covering_fix_rounds = {cap}){findings}")
    store.record_event(conn, run_id, ROUND_KIND,
                       f"automatic fix round {made + 1} of {cap} after round"
                       f" {rnd}: its findings share at most"
                       f" {overlap.shared:.2f} of any earlier round's"
                       f" (threshold {threshold:.2f})")
    ledger(conn, run_id, task_id, "round",
           f"Automatic fix round {made + 1} of {cap} after round {rnd} on"
           f" {pull.url}: the review of the fix at {sha} asked for new"
           " changes", provider)
    return fix_goal(pull, ticket, verdict,
                    f"Fix note from the review of the fix at {sha[:12]}",
                    fix_note_of(covering)), None


def fix_goal(pull, ticket, verdict, label, note):
    return (f"Fix the pre-merge review findings on {pull.url}. The ticket"
            f" is the contract:\n\n{ticket}\n\nReviewer findings:\n{verdict}"
            f"\n\n{label}:\n{note}\n\n"
            "Fix the blockers on this branch and commit; keep the ticket's"
            " verify commands passing.")


def repeat_overlap(conn, run_id, rnd, covering):
    """The most the covering round's findings share with any earlier round's."""
    best = Overlap(0.0, None)
    for earlier in store.read.rounds_of(conn, run_id):
        if earlier.round >= rnd or earlier.endedAt is None:
            continue
        findings = json.loads(earlier.findings)
        if store.findings_fingerprint(findings) == store.EMPTY_FINGERPRINT:
            continue
        shared = store.findings_overlap(findings, covering)
        if shared is None:
            shared = 1.0 if _unmet(covering) <= _unmet(findings) else 0.0
        if shared >= best.shared:
            best = Overlap(shared, earlier.round)
    return best


def fix_note_of(findings):
    lines = [f"- {_change(finding)}" for finding in findings
             if finding.get("evidence_only") is not True]
    return "\n".join(lines + ["Fix each point above on this branch and keep"
                              " the ticket's verify commands passing."])


def _change(finding):
    message = " ".join(str(finding.get("message", "")).split())
    path = finding.get("path", "")
    if path == store.CRITERIA_PATH:
        return f"Meet and witness the acceptance criterion: {message}"
    if not path or path.startswith(UNPARSED_PATH) or path in message:
        return message
    line = finding.get("line")
    return f"In {path}{f':{line}' if line else ''}: {message}"


def _covering_findings(conn, run_id, rnd):
    for recorded in store.read.rounds_of(conn, run_id):
        if recorded.round == rnd:
            return json.loads(recorded.findings)
    return []


def _unmet(findings):
    return {finding.get("line") for finding in findings
            if finding.get("path") == store.CRITERIA_PATH}
