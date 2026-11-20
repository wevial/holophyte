"""A project's `[[pipeline.check]]` stages run on a candidate and recorded."""
from holophyte.agents.review_workspace import review_base
from holophyte.config.config_tables import verify_config
from holophyte.config.pipeline_settings import check_sources, current_checks
from holophyte.loop.gates import run_verify, timed_out, verify_timed_out
from holophyte.loop.runs import heartbeat_while
from holophyte.redact import known_secrets
from holophyte.review.blast_radius import matching
from holophyte.review.briefs import _changed_files
from holophyte.stages import record, standard
from store import pipelines

OUTPUT_TAIL = 1000
NOT_STARTED = (126, 127)
UNAVAILABLE = ("the config no longer holds the command this check was pinned "
               "with")


def run(project, conn, run_id, beat_s, wt, base_sha, sha, after, **detail):
    sources, secrets = check_sources(project)
    for check in configured(project, conn, run_id, after, sources, secrets):
        reason = skip_reason(check, wt, base_sha, sha)
        if reason:
            record.skip(conn, run_id, check["id"], reason, sha)
            continue
        with record.Attempt(conn, run_id, check["id"], sha,
                            {**detail, "mode": check["mode"]},
                            wt=wt) as attempt, \
                heartbeat_while(conn, run_id, beat_s):
            outcome, found = (ran(project, check, wt) if check["commands"]
                              else ("error", {"error": UNAVAILABLE}))
            attempt.close(outcome, "next", sha,
                          **standard.scrubbed(found, secrets))


def configured(project, conn, run_id, after, sources, secrets):
    pinned = None
    if conn is not None and run_id is not None:
        pinned = pipelines.run_pipelines(conn, [run_id]).get(run_id)
    if not pinned:
        return [check for check in current_checks(project)
                if check["after"] == after]
    return [executable(project, stage, sources, secrets)
            for stage in pinned.get("stages", [])
            if stage.get("after") == after]


def executable(project, stage, sources, secrets):
    shown = known_secrets(project.config())
    for check in sources:
        if check["id"] == stage["id"] and standard.scrubbed(standard.scrubbed(
                check["commands"], secrets), shown) == stage["commands"]:
            return {**stage, "commands": check["commands"]}
    return {**stage, "commands": None}


def skip_reason(check, wt, base_sha, sha):
    if check["mode"] == "off":
        return "mode off"
    if not check["paths"]:
        return None
    changed = _changed_files(wt, review_base(wt, base_sha, sha), sha)
    if any(matching(path, check["paths"]) for path in changed):
        return None
    return f"no changed file matches {', '.join(check['paths'])}"


def ran(project, check, wt):
    try:
        ok, out = run_verify(" && ".join(check["commands"]), wt, None,
                             verify_config(project).timeout_sec,
                             project=project, rerun=False)
    except Exception as error:
        return "error", {"error": str(error)}
    if ok:
        return "pass", {"exit_status": 0, "output": str(out)[-OUTPUT_TAIL:]}
    status = (getattr(out, "failure", None) or {}).get("exit_status")
    return "error" if status in NOT_STARTED else "fail", {
        "exit_status": status,
        "timed_out": timed_out(out) or verify_timed_out(out),
        "output": str(out)[-OUTPUT_TAIL:]}
