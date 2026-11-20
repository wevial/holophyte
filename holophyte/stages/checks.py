"""A project's `[[pipeline.check]]` stages run on a candidate and recorded."""
import json
import os
import subprocess
from pathlib import Path

from holophyte.agents.review_workspace import review_base
from holophyte.config.config_tables import verify_config
from holophyte.config.pipeline_settings import check_secrets, current_checks
from holophyte.loop.gates import run_verify, timed_out, verify_timed_out
from holophyte.loop.runs import heartbeat_while
from holophyte.redact import REDACTED, outbound
from holophyte.review.blast_radius import matching
from holophyte.review.briefs import _changed_files
from holophyte.stages import record
from store import pipelines

OUTPUT_TAIL = 1000
FRAGMENT = 8
NOT_STARTED = {126: ("Permission denied", "cannot execute"),
               127: ("not found",)}
UNBOUND = ("this check's pinned command or paths hold a redacted value this "
           "process did not pin, so it cannot run them")


def run(project, conn, run_id, beat_s, wt, base_sha, sha, after, **detail):
    secrets = check_secrets(project)
    for check in configured(project, conn, run_id, after):
        reason = skip_reason(check, wt, base_sha, sha)
        if reason:
            record.skip(conn, run_id, check["id"], reason, sha)
            continue
        with record.Attempt(conn, run_id, check["id"], sha,
                            {**detail, "mode": check["mode"]},
                            wt=wt) as attempt, \
                heartbeat_while(conn, run_id, beat_s):
            outcome, found = (ran(project, check, wt, secrets)
                              if check["commands"] is not None
                              else ("error", {"error": UNBOUND}))
            attempt.close(outcome, "next", sha, **found)


def configured(project, conn, run_id, after):
    pinned = None
    if conn is not None and run_id is not None:
        pinned = pipelines.run_pipelines(conn, [run_id]).get(run_id)
    if not pinned:
        return [check for check in current_checks(project)
                if check["after"] == after]
    raw = {check["id"]: check
           for check in record.pinned_raw_checks(conn, run_id) or ()}
    return [bound(stage, raw.get(stage["id"]))
            for stage in pinned.get("stages", []) if stage.get("after") == after]


def bound(stage, raw):
    if raw is not None:
        return {**stage, "commands": raw["commands"], "paths": raw["paths"]}
    if REDACTED in json.dumps([stage["commands"], stage["paths"]]):
        return {**stage, "commands": None, "paths": None}
    return stage


def skip_reason(check, wt, base_sha, sha):
    if check["mode"] == "off":
        return "mode off"
    if not check["paths"]:
        return None
    changed = _changed_files(wt, review_base(wt, base_sha, sha), sha)
    if any(matching(path, check["paths"]) for path in changed):
        return None
    return f"no changed file matches {', '.join(check['paths'])}"


def ran(project, check, wt, secrets):
    before = _status(wt)
    try:
        outcome, found = _outcome(project, check, wt)
    finally:
        restored = _restore(wt, before)
    if "output" in found:
        found["output"] = scrubbed_output(found["output"], secrets)
    if "error" in found:
        found["error"] = scrubbed_output(found["error"], secrets)
    return outcome, {**found, **({"restored": restored} if restored else {})}


def _outcome(project, check, wt):
    try:
        ok, out = run_verify(" && ".join(check["commands"]), wt, None,
                             verify_config(project).timeout_sec,
                             project=project, rerun=False)
    except Exception as error:
        return "error", {"error": str(error)}
    if ok:
        return "pass", {"exit_status": 0, "output": str(out)}
    facts = getattr(out, "failure", None) or {}
    status = facts.get("exit_status")
    unlaunched = any(word in str(facts.get("last_output_line"))
                     for word in NOT_STARTED.get(status, ()))
    return "error" if unlaunched else "fail", {
        "exit_status": status,
        "timed_out": timed_out(out) or verify_timed_out(out),
        "output": str(out)}


def scrubbed_output(text, secrets):
    text = outbound(text, secrets)
    for secret in sorted(secrets, key=len, reverse=True):
        for size in range(len(secret) - 1, FRAGMENT - 1, -1):
            for part in (secret[:size], secret[-size:]):
                text = text.replace(part, REDACTED)
    return text[-OUTPUT_TAIL:]


def _status(wt):
    listed = subprocess.run(
        ["git", "status", "--porcelain", "-z", "--no-renames",
         "--untracked-files=all"], cwd=wt, capture_output=True,
        check=True).stdout
    return {os.fsdecode(entry) for entry in listed.split(b"\0") if entry}


def _restore(wt, before):
    changed = sorted(_status(wt) - before)
    for entry in changed:
        if entry.startswith("??"):
            (Path(wt) / entry[3:]).unlink(missing_ok=True)
        else:
            subprocess.run(["git", "restore", "--source=HEAD", "--staged",
                            "--worktree", "--", entry[3:]], cwd=wt,
                           capture_output=True, check=True)
    return [entry[3:] for entry in changed]
