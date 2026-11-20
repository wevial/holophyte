from holophyte import note_privacy
from holophyte.board.projection import ledger
from holophyte.config.config_tables import merge_config
from holophyte.loop.runs import heartbeat_while
from holophyte.pr import github, image_fetch, pr_media
from holophyte.stages import record
from holophyte.tickets import ticket_template


def captured(conn, run_id, wt, capture, sha=None, fixable=False):
    if conn is None or run_id is None:
        return capture(None)
    sha = sha or record.head(conn, run_id, wt)
    seen = {}
    with record.Deferred(conn, run_id, "capture", sha, {}, wt) as attempt:
        taken = capture(seen)
    if "skipped" in seen:
        if not record.repeats(conn, run_id, "capture", sha, "skipped", "next",
                              {"reason": seen["skipped"]}):
            record.skip(conn, run_id, "capture", seen["skipped"], sha)
    elif seen.get("fresh"):
        found = record.guarded(conn, run_id, "stage_read", lambda: _outcome(
            seen["receipt"], fixable))
        if found is not record.REFUSED:
            attempt.opened().close(found[0], found[1], sha, **found[2])
    return taken


def _outcome(receipt, fixable):
    failure = pr_media.failure_at(receipt)
    if failure is None:
        return "pass", "next", {}
    edge = "on_fail" if fixable and failure.kind == "capture" else "next"
    return "fail", edge, {"kind": failure.kind,
                          "failure": " ".join(failure.sentence.split()),
                          "missing": list(failure.missing)}


def publish_evidence(project, conn, run_id, task_id, wt, pull, ticket, sha,
                     beat_s, fixable=False):
    if not merge_config(project).ui_paths:
        return None
    parsed = ticket_template.parse(ticket)
    states, files = parsed.evidence_states, parsed.evidence_files
    endpoint = f"repos/{pull.repo}/pulls/{pull.number}"
    with heartbeat_while(conn, run_id, beat_s):
        body = github.rest(project, pull, "GET", endpoint)["body"] or ""
        evidence = github.split_pr_body(body)[2]
        section = captured(conn, run_id, wt, lambda seen: pr_media.refresh(
            project, wt, task_id, evidence, evidence_states=states,
            evidence_files=files, ui_change=parsed.ui_change,
            record_note=lambda text: ledger(conn, run_id, task_id, "note",
                                            text, None), seen=seen),
            sha, fixable)
        failure = pr_media.capture_failure(project, wt, task_id, states, files,
                                           ui_change=parsed.ui_change)
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
