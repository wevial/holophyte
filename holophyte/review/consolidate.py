"""One findings list for the fix turn: exact merge, consolidator pass, concern cap."""
import json
import re
import subprocess

import store
from holophyte.board.projection import ledger
from holophyte.loop.gates import InfraFailure, verify_timed_out
from holophyte.redact import safe_print as print
from holophyte.review.blast_radius import continued_runs, high_path
from holophyte.review.json_reply import SchemaBrief
from holophyte.review.reply_parsing import (
    FINDING_CATEGORIES,
    UNPARSED_PATH,
    parse_findings,
    stale_approvals,
)

SEVERITIES = ("p0", "p1", "p2", "nit")
EVIDENCE = ("reproduced", "traced", "review", "concern")
REVIEWERS = ("primary", "adversary")
CONCERN_CAP = 3
TIMEOUT = 600
DONE = "CONSOLIDATED"
RAISED = "Concern on a high-blast-radius path:"
VERDICT_RE = re.compile(r"^\s*VERDICT:.*(?:\n|$)", re.I | re.M)
EVIDENCE_RE = re.compile(r"^[\s>*`_-]*EVIDENCE[*`_]*:", re.I | re.M)
BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
SEVERITY_TAG_RE = re.compile(
    r"^[\s:]*[\[(]\s*(?:p0|p1|p2|nit|blocker)\s*[\])]\s*", re.I)
MERGE_RE = re.compile(
    r"^MERGE:\s*F(\d+)\s+INTO\s+F(\d+)\b\s*(?:[—–:-]+\s*)?(.*)$", re.I)
ORDER_RE = re.compile(r"^ORDER:\s*(.*)$", re.I)
ID_RE = re.compile(r"F(\d+)", re.I)


def _note(finding):
    found = EVIDENCE_RE.search(finding["message"])
    return finding["message"][found.start():].strip() if found else ""


def _body(finding):
    text = EVIDENCE_RE.split(finding["message"], maxsplit=1)[0]
    text = BULLET_RE.sub("", VERDICT_RE.sub("", text), count=1).lstrip("`*_")
    line = finding.get("line")
    for place in (f"{finding['path']}:{line}" if line else None, finding["path"]):
        if place and text.startswith(place):
            text = text[len(place):].lstrip("`*_")
            break
    text = SEVERITY_TAG_RE.sub("", text, count=1)
    return text.lstrip(" \t:—–-").rstrip() or finding["message"].strip()


def said(message):
    return " ".join(message.split()).casefold()


def _item(finding, found_by, evidence):
    severity = finding.get("severity")
    return {"path": finding["path"], "line": finding.get("line") or None,
            "severity": severity if severity in SEVERITIES else "p2",
            "evidence": evidence if evidence in EVIDENCE else "concern",
            "found_by": [found_by], "messages": [_body(finding)],
            "places": [(finding["path"], finding.get("line") or None)],
            "notes": [note] if (note := VERDICT_RE.sub("", _note(finding))) else [],
            "category": finding.get("category")}


def items(primary, adversary):
    return ([_item(finding, "primary", "review") for finding in primary]
            + [_item(finding, "adversary", finding.get("evidence"))
               for finding in adversary])


def concern(item):
    return item["evidence"] == "concern"


def _place(item):
    return item["path"], item["line"]


def _absorb(into, other, messages):
    into["severity"] = min(into["severity"], other["severity"],
                           key=SEVERITIES.index)
    into["evidence"] = min(into["evidence"], other["evidence"],
                           key=EVIDENCE.index)
    into["found_by"] = [who for who in REVIEWERS
                        if who in into["found_by"] + other["found_by"]]
    into["notes"] = into["notes"] + [note for note in other["notes"]
                                     if note not in into["notes"]]
    into["category"] = into["category"] or other["category"]
    if messages:
        into["messages"] = into["messages"] + other["messages"]
        into["places"] = into["places"] + other["places"]


def _rank(item):
    return SEVERITIES.index(item["severity"]), "primary" not in item["found_by"]


def ordered(merged):
    out, placed = [], set()
    for blocking in (True, False):
        part = sorted((item for item in merged if concern(item) != blocking),
                      key=_rank)
        for item in part:
            together = [other for other in part if id(other) not in placed
                        and _place(other) == _place(item)]
            placed.update(id(other) for other in together)
            out += together
    return out


def exact(found):
    merged = {}
    for item in found:
        kept = merged.setdefault((*_place(item), said(item["messages"][0])), [])
        twin = next((other for other in kept if not _distinct(other, item)),
                    None)
        if twin is None:
            kept.append(dict(item))
        else:
            _absorb(twin, item, messages=False)
    return ordered([item for kept in merged.values() for item in kept])


def _distinct(kept, item):
    return (kept["found_by"] == item["found_by"] == ["primary"]
            and kept["severity"] != item["severity"])


def _where(path, line):
    if path.startswith(UNPARSED_PATH):
        return "(no location)"
    return f"{path}:{line}" if line else path


def location(item):
    return _where(item["path"], item["line"])


def _texts(item):
    return [message if place == _place(item)
            else f"{_where(*place)}: {message}"
            for message, place in zip(item["messages"], item["places"])]


def _entry(n, item, prefix):
    head = (f"{prefix}{n}. {location(item)} [{item['severity']}] evidence "
            f"{item['evidence']}, found by {' and '.join(item['found_by'])}")
    if concern(item) and not prefix:
        head += (" -- a concern: answer it ADDRESS, FOLLOW_UP or DECLINE; "
                 "it never blocks")
    lines = [head]
    for message in _texts(item) + item["notes"]:
        lines += ["    " + line for line in message.splitlines() if line.strip()]
    return "\n".join(lines)


def brief(merged):
    listed = "\n".join(_entry(n, item, "F") for n, item in enumerate(merged, 1))
    return (
        "You are a READ-ONLY consolidator. Two reviewers, the primary and an "
        "adversary, reported the findings below on one candidate. Merge the "
        "items that report the same problem in different words, and order "
        "the list by what the fix should answer first. You cannot drop or "
        "downgrade a finding: every item stays, and a merge keeps the higher "
        "severity, the stronger evidence and both messages. Do not modify "
        "anything.\n\n"
        f"{listed}\n\n"
        "Reply with one line per merge, `MERGE: Fa INTO Fb — reason`, then "
        "one line `ORDER: Fx, Fy, ...` naming items in the order the fix "
        "should take them, and end your reply with exactly one line:\n"
        f"{DONE}")


def _bare(line):
    return line.strip().lstrip("-*+> ").strip("`*_ ")


def _root(into, name):
    while name in into:
        name = into[name]
    return name


def _merge(found, ids, into, merged):
    source, target = (f"F{int(found[n])}" for n in (1, 2))
    if source not in ids or target not in ids or source in into:
        return False
    root = _root(into, target)
    if root == source:
        return False
    into[source] = root
    _absorb(merged[root], merged[source], messages=True)
    return {"from": source, "into": target, "reason": found[3].strip()}


def _order(found, ids, into):
    named = [name.strip() for name in found[1].split(",") if name.strip()]
    wanted = [f"F{int(match[1])}" for name in named
              if (match := ID_RE.fullmatch(name))]
    if not named or len(wanted) != len(named) or any(n not in ids for n in wanted):
        return None
    return list(dict.fromkeys(_root(into, name) for name in wanted))


def apply(merged, reply):
    lines = [_bare(line) for line in str(reply).splitlines() if line.strip()]
    if not lines or lines[-1] != DONE:
        return merged, {"pass2": "malformed", "merges": [], "ignored": []}
    ids = [f"F{n}" for n in range(1, len(merged) + 1)]
    copies = {name: dict(item) for name, item in zip(ids, merged)}
    into, merges, ignored, order = {}, [], [], None
    for line in lines[:-1]:
        merge, ordering = MERGE_RE.match(line), ORDER_RE.match(line)
        done = merge and _merge(merge, ids, into, copies)
        wanted = ordering and order is None and _order(ordering, ids, into)
        if done:
            merges.append(done)
        elif wanted:
            order = wanted
        elif ordering or line.upper().startswith("MERGE"):
            ignored.append(line)
    kept = [name for name in ids if name not in into]
    for name in kept:
        copies[name]["category"] = next(
            (item["category"] for other, item in zip(ids, merged)
             if _root(into, other) == name and item["category"]), None)
    final = [name for name in order or () if name in kept]
    final += [name for name in kept if name not in final]
    return [copies[name] for name in final], {
        "pass2": "merged" if merges else "unchanged", "merges": merges,
        "ignored": ignored}


def second_pass(merged, ask):
    try:
        reply = ask(brief(merged))
        failure = ("timed out" if getattr(reply, "timed_out", False) else
                   getattr(reply, "exit_code", 0) and
                   f"exited {reply.exit_code}")
    except (InfraFailure, subprocess.TimeoutExpired, OSError) as failed:
        failure = failed
    if failure:
        print(f"[holo2] consolidator route failed ({failure}); "
              "the fix turn gets the exact merge")
        return merged, {"pass2": "unavailable", "merges": [], "ignored": []}
    final, record = apply(merged, reply)
    if record["pass2"] == "malformed":
        print(f"[holo2] consolidator reply had no {DONE} line; "
              "the fix turn gets the exact merge")
    return final, record


HELD = "held"
HELD_SEVERITY_RE = re.compile(r"\[(p[0-2])\]")
HELD_BRIEF = (" A finding raised by held is a description finding an earlier "
              "round holds for the PR-text writer: refute it with the reason "
              "it no longer applies to withdraw it, or it stays held.")


def council_inputs(answered, adversary, held=()):
    raised = [(f"seat {seat.number}", finding, None, None)
              for seat in answered for finding in seat.value["findings"]]
    raised += [("adversary", _council_finding(item), item["evidence"], found)
               for item, found in zip(items([], adversary), adversary)]
    raised += [(HELD, {"path": None, "line": None,
                       "severity": (HELD_SEVERITY_RE.search(line) or
                                    [None, "p1"])[1],
                       "category": "docs", "target": "description",
                       "message": line}, None, None) for line in held]
    return {f"F{n}": entry for n, entry in enumerate(raised, 1)}


def _council_finding(item):
    return {"path": item["path"], "line": item["line"],
            "severity": "p2" if item["severity"] == "nit" else item["severity"],
            "category": (item["category"] if item["category"]
                         in FINDING_CATEGORIES else "correctness"),
            "target": "code",
            "message": "\n".join(item["messages"] + item["notes"])}


def _council_vote(seat):
    head = f"Seat {seat.number} ({seat.route})"
    if seat.status != "answered":
        return f"{head} did not vote, {seat.status}: {seat.reason}"
    return f"{head}:\n{json.dumps(seat.value, sort_keys=True)}"


def council_brief(seated, found, criteria, sha):
    listed = "\n".join(
        f"{name}, raised by {who}: {_where(finding['path'] or '', finding['line'])}"
        f" [{finding['severity']}]{f' evidence {evidence}' if evidence else ''}"
        f" {' '.join(finding['message'].split())}"
        for name, (who, finding, evidence, _) in found.items()) or "(none)"
    text = (
        "You are the READ-ONLY consolidator of a review council. Its seats "
        f"reviewed commit {sha} against the ticket's acceptance criteria:\n\n"
        + "\n".join(f"C{n}. {criterion}" for n, criterion in criteria)
        + "\n\nEach seat's reply follows as numbered JSON.\n\n"
        + "\n\n".join(map(_council_vote, seated))
        + "\n\nEvery finding the seats and the adversarial reviewer raised, "
        f"by id:\n{listed}\n\n"
        "Give the council's one authoritative round. Keep each finding that "
        "blocks the candidate, merge the findings that report one defect in "
        "different words into one, and list in its raised_by the id of every "
        "finding above it covers; a finding that repeats another is merged "
        "that way, never refuted. Drop a finding only as a refuted entry "
        "naming its ids in raised_by, with the reason it does not block. A "
        "finding two or more seats raised at the same path and line is kept "
        "even if you drop it, and so is one you neither keep nor drop, except "
        "an adversary finding with evidence concern, which blocks only if you "
        "keep it. A "
        "criterion a seat marks not_met or unwitnessed stays so unless you "
        "name the test or check that witnesses it. Before you reply, run at "
        "least one "
        "read-only command in the candidate checkout, such as `git show "
        "--stat HEAD`, to check what the seats claim. Do not modify anything."
        " Your reply "
        "is one JSON object that the turn's output schema checks: verdict, "
        "criteria, findings, follow_ups, scope and refuted, with the fields "
        "the seats' replies use, plus raised_by on each finding and refuted "
        "entry."
        + (HELD_BRIEF if any(who == HELD for who, *_ in found.values())
           else ""))
    return SchemaBrief(text, text, text)


def capped(final, fixing):
    concerns = [item for item in final if concern(item)]
    sent = {id(item) for item in concerns[:CONCERN_CAP]} if fixing else set()
    return ([item for item in final if not concern(item) or id(item) in sent],
            [item for item in concerns if id(item) not in sent])


def fix_list(sent):
    return ("The primary and adversarial reviewers' findings, merged into "
            "one list:\n\n"
            + "\n".join(_entry(n, item, "") for n, item in enumerate(sent, 1)))


def fixing(decision, findings, ok, out):
    if decision != "APPROVE":
        return True
    if not findings:
        return not ok and not verify_timed_out(out)
    return not (ok and stale_approvals(decision, findings))


def primary_findings(verdict, decision, unwitnessed):
    structured = getattr(verdict, "council_findings", None)
    asked = ((parse_findings(verdict) if structured is None else structured)
             if decision == "REQUEST_CHANGES" else [])
    return asked + unwitnessed


def adversary_findings(conn, run_id, rnd):
    row = conn.execute(
        "SELECT payload FROM runEvents WHERE runId = ? AND kind = 'adversary_round'"
        " ORDER BY seq DESC LIMIT 1", (run_id,)).fetchone()
    payload = json.loads(row[0]) if row else None
    if payload is None or payload["round"] != rnd:
        return None
    return payload["findings"] + payload["concerns"]


def _recorded(item, rnd):
    return {"path": item["path"], "line": item["line"],
            "severity": item["severity"], "evidence": item["evidence"],
            "found_by": item["found_by"], "message": "\n".join(_texts(item)),
            "notes": item["notes"], "round": rnd,
            **({"category": item["category"]} if item["category"] else {})}


def _events(conn, run_id, kind):
    return [json.loads(text) for run in continued_runs(conn, run_id)
            for (text,) in conn.execute(
                "SELECT payload FROM runEvents WHERE runId = ? AND kind = ?"
                " ORDER BY seq", (run, kind))]


def held_concerns(conn, run_id):
    if conn is None or run_id is None:
        return []
    distinct = {}
    for event in _events(conn, run_id, "consolidation"):
        for held in event["held_concerns"]:
            distinct.setdefault(
                (held["path"], held["line"], said(held["message"])), held)
    return list(distinct.values())


def bullet(held):
    return (f"{location(held)} [{held['severity']}] "
            f"{' '.join(held['message'].split())} (round {held['round']})")


def raise_concerns(project, conn, run_id, provider, task_id, rnd, found):
    raised = {(event["path"], said(event["message"]))
              for event in _events(conn, run_id, "concern_raised")}
    for item in filter(concern, found):
        record = _recorded(item, rnd)
        key = (item["path"], said(record["message"]))
        pattern = high_path(project, item["path"])
        if pattern is None or key in raised:
            continue
        raised.add(key)
        store.record_event(conn, run_id, "concern_raised",
                           f"round {rnd} concern on {item['path']}",
                           level="detail",
                           payload=json.dumps(dict(record, pattern=pattern)))
        ledger(conn, run_id, task_id, "note", f"{RAISED} {bullet(record)}",
               provider)


def handed_on(project, conn, run_id, provider, task_id, rnd, primary,
              adversary, fix, ask):
    found = items(primary, adversary)
    merged = exact(found)
    record = {"round": rnd, "pass1_in": len(found), "pass1_out": len(merged),
              "pass2": "skipped", "merges": [], "ignored": []}
    final = merged
    if fix and ask is not None and len(merged) >= 2:
        final, outcome = second_pass(merged, ask)
        record.update(outcome)
    sent, held = capped(final, fix)
    record["sent_concerns"] = [_recorded(item, rnd) for item in sent
                               if concern(item)]
    record["held_concerns"] = [_recorded(item, rnd) for item in held]
    store.record_event(conn, run_id, "consolidation",
                       f"round {rnd} consolidation: {len(found)} findings in, "
                       f"{len(sent)} to the fix turn, {len(held)} held",
                       level="detail", payload=json.dumps(record))
    if held:
        ledger(conn, run_id, task_id, "note",
               f"Round {rnd}: {len(held)} concerns held from the fix turn "
               "(non-blocking):\n"
               + "\n".join(f"- {bullet(item)}" for item in record["held_concerns"]),
               provider)
    raise_concerns(project, conn, run_id, provider, task_id, rnd, found)
    return fix_list(sent) if fix and sent else None
