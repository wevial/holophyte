import ticket_template
from holophyte.board.projection import ledger
from holophyte.config.config_tables import merge_config
from holophyte.loop.runs import heartbeat_while
from holophyte.pr import github, pr_media


def publish_evidence(project, conn, run_id, task_id, wt, pull, ticket, sha, beat_s):
    if not merge_config(project).ui_paths:
        return ""
    states = ticket_template.parse(ticket).evidence_states
    with heartbeat_while(conn, run_id, beat_s):
        body = github.rest(project, pull, "GET",
                           f"repos/{pull.repo}/pulls/{pull.number}")["body"] or ""
        evidence = github.split_pr_body(body)[2]
        section = pr_media.refresh(
            project, wt, task_id, evidence, evidence_states=states,
            record_note=lambda text: ledger(conn, run_id, task_id, "note", text, None))
        if section is None or section.rstrip() == evidence.rstrip():
            return ""
        failure = pr_media.capture_failure(project, wt, task_id, states)
        github.edit_pr_body(project, pull, github.replace_pr_evidence(body, section))
    if failure:
        captured = pr_media.CAPTURED.search(evidence)
        old = captured[1] if captured else "an earlier commit"
        text = (f"Evidence on {pull.url}: kept the capture at {old}, marked stale;"
                f" capturing {sha[:12]} failed: {' '.join(failure.split())}")
    else:
        shown = pr_media.CAPTURED.search(section)
        text = f"Evidence on {pull.url}: captured at {shown[1] if shown else sha[:12]}"
    ledger(conn, run_id, task_id, "note", text, None)
    return failure
