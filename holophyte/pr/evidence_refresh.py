from holophyte import note_privacy
from holophyte.board.projection import ledger
from holophyte.config.config_tables import merge_config
from holophyte.loop.runs import heartbeat_while
from holophyte.pr import github, image_fetch, pr_media
from holophyte.stages import record
from holophyte.tickets import ticket_template


def captured(conn, run_id, wt, capture, sha=None, report=None, settle=None):
    report = {} if report is None else report
    taken = capture(report)
    if conn is None or run_id is None:
        return taken
    sha = sha or record.head(conn, run_id, wt)
    if "skipped" in report:
        skipped(conn, run_id, sha, report["skipped"])
        return taken
    found = record.guarded(conn, run_id, "stage_read", lambda: _outcome(
        conn, run_id, report.get("receipt")))
    if found in (None, record.REFUSED):
        return taken
    outcome, detail, times = found

    def close(edge):
        record.span(conn, run_id, "capture", sha, outcome, edge, detail, times)
    if outcome == "fail" and settle is not None:
        settle.append(close)
    else:
        close("next")
    return taken


def skipped(conn, run_id, sha, reason):
    if conn is None or run_id is None:
        return
    if not record.repeats(conn, run_id, "capture", sha, "skipped", "next",
                          {"reason": reason}):
        record.skip(conn, run_id, "capture", reason, sha)


def _outcome(conn, run_id, receipt):
    if receipt is None or not receipt.exists() or record.recorded(
            conn, run_id, "capture", "receipt", receipt.name):
        return None
    failure, times = pr_media.failure_at(receipt), pr_media.taken_at(receipt)
    if failure is None:
        return "pass", {"receipt": receipt.name}, times
    return "fail", {"receipt": receipt.name, "kind": failure.kind,
                    "failure": " ".join(failure.sentence.split()),
                    "missing": list(failure.missing)}, times


def settled(settle, edge):
    for close in settle:
        close(edge)
    settle.clear()


def publish_evidence(project, conn, run_id, task_id, wt, pull, ticket, sha,
                     beat_s, settle=None):
    if not merge_config(project).ui_paths:
        skipped(conn, run_id, sha, pr_media.UI_PATHS_UNSET)
        return None
    pending = []
    try:
        failure = _publish(project, conn, run_id, task_id, wt, pull, ticket,
                           sha, beat_s, pending)
    except BaseException:
        settled(pending, "next")
        raise
    if settle is None:
        settled(pending, "next")
    else:
        settle.extend(pending)
    return failure


def _publish(project, conn, run_id, task_id, wt, pull, ticket, sha, beat_s,
             pending):
    parsed = ticket_template.parse(ticket)
    states, files = parsed.evidence_states, parsed.evidence_files
    endpoint = f"repos/{pull.repo}/pulls/{pull.number}"
    with heartbeat_while(conn, run_id, beat_s):
        body = github.rest(project, pull, "GET", endpoint)["body"] or ""
        evidence = github.split_pr_body(body)[2]
        told = {}
        section = captured(conn, run_id, wt, lambda report: pr_media.refresh(
            project, wt, task_id, evidence, evidence_states=states,
            evidence_files=files, ui_change=parsed.ui_change,
            record_note=lambda text: ledger(conn, run_id, task_id, "note",
                                            text, None), report=report),
            sha, told, pending)
        failure = (pr_media.failure_at(told["receipt"]) if "receipt" in told
                   else pr_media.capture_failure(project, wt, task_id, states,
                                                 files,
                                                 ui_change=parsed.ui_change))
        if section is None or section.rstrip() == evidence.rstrip():
            return failure
        body = github.rest(project, pull, "GET", endpoint)["body"] or ""
        body = note_privacy.withhold(github.replace_pr_evidence(body, section),
                                     note_privacy.private_texts(conn, run_id))
        github.edit_pr_body(project, pull, body)
        notes = image_fetch.check(project, pull, body)
    for note in notes:
        ledger(conn, run_id, task_id, "note", note, None)
    if failure:
        stamped = pr_media.CAPTURED.search(evidence)
        old = stamped[1] if stamped else "an earlier commit"
        text = (f"Evidence on {pull.url}: kept the capture at {old}, marked stale;"
                f" capturing {sha[:12]} failed: {' '.join(failure.sentence.split())}")
    else:
        shown = pr_media.CAPTURED.search(section)
        text = f"Evidence on {pull.url}: captured at {shown[1] if shown else sha[:12]}"
    ledger(conn, run_id, task_id, "note", text, None)
    return failure
