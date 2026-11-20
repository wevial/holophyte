from holophyte import note_privacy
from holophyte.board.projection import ledger
from holophyte.config.config_tables import merge_config
from holophyte.loop.runs import heartbeat_while
from holophyte.pr import github, image_fetch, pr_media
from holophyte.stages import record
from holophyte.tickets import ticket_template


def captured(project, conn, run_id, wt, task_id, parsed, capture, sha=None,
             fixable=False):
    if conn is None or run_id is None:
        return capture()
    sha = sha or record.head(conn, run_id, wt)
    with record.Deferred(conn, run_id, "capture", sha, {}, wt) as attempt:
        taken = capture()
    found = record.guarded(conn, run_id, "stage_read", lambda: _capture_outcome(
        project, wt, task_id, parsed, fixable))
    if found in (None, record.REFUSED) or record.repeats(conn, run_id, "capture", sha,
                                                   *found):
        return taken
    outcome, edge, detail = found
    if outcome == "skipped":
        record.skip(conn, run_id, "capture", detail["reason"], sha)
    else:
        attempt.opened().close(outcome, edge, sha, **detail)
    return taken


def _capture_outcome(project, wt, task_id, parsed, fixable):
    cfg = merge_config(project)
    reason = ("[merge] ui_paths is unset" if not cfg.ui_paths
              else "[merge] ui_capture is unset" if not cfg.ui_capture.strip()
              else "the change touches no UI path"
              if not pr_media.matches(wt, cfg.ui_paths)
              else "the ticket asks for no UI capture"
              if not pr_media._wants_capture(cfg, wt, task_id, parsed.ui_change,
                                             parsed.evidence_states)
              else None)
    if reason is not None:
        return "skipped", "next", {"reason": reason}
    receipt = pr_media._receipt(project, wt, task_id, parsed.evidence_states,
                                parsed.evidence_files)[0]
    if not receipt.exists():
        return None
    failure = pr_media.capture_failure(
        project, wt, task_id, parsed.evidence_states, parsed.evidence_files,
        ui_change=parsed.ui_change)
    if failure is None:
        return "pass", "next", {}
    edge = "on_fail" if fixable and failure.kind == "capture" else "next"
    return "fail", edge, {"kind": failure.kind,
                          "failure": " ".join(failure.sentence.split()),
                          "missing": list(failure.missing)}


def publish_evidence(project, conn, run_id, task_id, wt, pull, ticket, sha,
                     beat_s, fixable=False):
    parsed = ticket_template.parse(ticket)
    return captured(project, conn, run_id, wt, task_id, parsed, lambda: _publish(
        project, conn, run_id, task_id, wt, pull, parsed, sha, beat_s), sha,
        fixable)


def _publish(project, conn, run_id, task_id, wt, pull, parsed, sha, beat_s):
    if not merge_config(project).ui_paths:
        return None
    states, files = parsed.evidence_states, parsed.evidence_files
    endpoint = f"repos/{pull.repo}/pulls/{pull.number}"
    with heartbeat_while(conn, run_id, beat_s):
        body = github.rest(project, pull, "GET", endpoint)["body"] or ""
        evidence = github.split_pr_body(body)[2]
        section = pr_media.refresh(
            project, wt, task_id, evidence, evidence_states=states,
            evidence_files=files, ui_change=parsed.ui_change,
            record_note=lambda text: ledger(conn, run_id, task_id, "note", text, None))
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
        captured = pr_media.CAPTURED.search(evidence)
        old = captured[1] if captured else "an earlier commit"
        text = (f"Evidence on {pull.url}: kept the capture at {old}, marked stale;"
                f" capturing {sha[:12]} failed: {' '.join(failure.sentence.split())}")
    else:
        shown = pr_media.CAPTURED.search(section)
        text = f"Evidence on {pull.url}: captured at {shown[1] if shown else sha[:12]}"
    ledger(conn, run_id, task_id, "note", text, None)
    return failure
