import ticket_template
from holophyte.board.projection import ledger
from holophyte.config.config_tables import merge_config
from holophyte.loop.runs import heartbeat_while
from holophyte.pr import github, image_fetch, pr_media


def publish_evidence(project, conn, run_id, task_id, wt, pull, ticket, sha, beat_s):
    if not merge_config(project).ui_paths:
        return None
    states = ticket_template.parse(ticket).evidence_states
    endpoint = f"repos/{pull.repo}/pulls/{pull.number}"
    with heartbeat_while(conn, run_id, beat_s):
        body = github.rest(project, pull, "GET", endpoint)["body"] or ""
        evidence = github.split_pr_body(body)[2]
        section = pr_media.refresh(
            project, wt, task_id, evidence, evidence_states=states,
            record_note=lambda text: ledger(conn, run_id, task_id, "note", text, None))
        failure = pr_media.capture_failure(project, wt, task_id, states)
        if section is None or section.rstrip() == evidence.rstrip():
            return failure
        body = github.rest(project, pull, "GET", endpoint)["body"] or ""
        body = github.replace_pr_evidence(body, section)
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
