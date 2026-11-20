"""A project's `[[pipeline.check]]` stages run on a candidate and recorded."""
import io
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from contextvars import ContextVar
from pathlib import Path

from holophyte.agents.review_workspace import review_base
from holophyte.config.config_tables import verify_config
from holophyte.config.pipeline_settings import check_secrets, current_checks
from holophyte.isolation.isolation_git import git
from holophyte.isolation.launcher import SignalExit
from holophyte.loop.gates import _verify_command
from holophyte.loop.runs import heartbeat_while
from holophyte.redact import REDACTED, outbound
from holophyte.review.blast_radius import continued_runs, matching
from holophyte.review.briefs import _changed_files
from holophyte.stages import record
from store import pipelines

OUTPUT_TAIL = 1000
FRAGMENT = 8
GROUP_GRACE_S = 2.0
UNLAUNCHED = re.compile(
    r"^(?:\S*/)?(?:ba|da|z)?sh: (?:line )?(?:\d+: )?.+: "
    r"(?:command not found|not found|Permission denied|cannot execute.*)$")
UNBOUND = ("this check's pinned commands hold a redacted value this process "
           "did not pin, so it cannot run them")
_trim_findings = ContextVar("trim_check_findings", default=None)


def run(project, conn, run_id, beat_s, wt, base_sha, sha, after, *,
        required_only=False, **detail):
    findings = []
    if after == "trim":
        _trim_findings.set((wt, conn, run_id, findings))
    elif detail.get("round") == 1:
        findings.extend(_after_trim(conn, run_id, wt))
    for check in configured(project, conn, run_id, after):
        if not required_only or check["mode"] == "required":
            record.checks_guarded(conn, run_id, lambda: _run_one(
                project, conn, run_id, beat_s, wt, base_sha, sha, check,
                detail, findings))
    return findings


def _after_trim(conn, run_id, wt):
    held = _trim_findings.get()
    if held and held[:3] == (wt, conn, run_id):
        return held[3]
    if conn is None or run_id is None:
        return []
    for previous in reversed(continued_runs(conn, run_id)):
        trim = conn.execute("SELECT MAX(id) FROM stageAttempts"
                            " WHERE runId = ? AND stageId = 'trim'", (previous,))
        if (last_trim := trim.fetchone()[0]) is not None:
            return [json.loads(row[0]) for row in conn.execute(
                "SELECT json_extract(detail, '$.finding') FROM stageAttempts"
                " WHERE runId = ? AND id > ? AND json_type(detail, '$.finding')"
                " = 'object' ORDER BY id", (previous, last_trim))]
    return []


def _run_one(project, conn, run_id, beat_s, wt, base_sha, sha, check, detail,
             findings):
    reason = skip_reason(check, wt, base_sha, sha)
    if reason:
        record.skip(conn, run_id, check["id"], reason, sha, verbatim=True)
        return
    with record.Attempt(conn, run_id, check["id"], sha,
                        {**detail, "mode": check["mode"]},
                        wt=wt, verbatim=("mode",)) as attempt, \
            heartbeat_while(conn, run_id, beat_s):
        outcome, found = _ran_bound(project, check, wt, sha)
        blocking = check["mode"] == "required" and outcome != "pass"
        if blocking:
            finding = _finding(check, found)
            findings.append(finding)
            if check["after"] == "trim":
                found["finding"] = finding
        attempt.close(outcome, "on_fail" if blocking else "next", sha, **found)


def _finding(check, result):
    command = outbound(" && ".join(check["commands"] or []), check["secrets"])
    name = outbound(check["id"], check["secrets"])
    status = result.get("exit_status")
    title = f"required check {name} failed: {command}"
    if result.get("timed_out"):
        title += " (timed out)"
    elif status is not None:
        title += f" (exit {status})"
    tail = result.get("output") or result.get("error")
    return {"path": name, "line": None, "severity": "p1", "title": title,
            "message": f"{title}\n{tail}" if tail else title}


def _ran_bound(project, check, wt, sha):
    if check["commands"] is None:
        return "error", {"error": UNBOUND}
    try:
        return ran(project, check, wt, sha)
    except SignalExit:
        raise
    except SystemExit as error:
        if check["mode"] != "required":
            raise
        return "error", {"error": scrubbed_output(str(error), check["secrets"])}


def configured(project, conn, run_id, after):
    secrets = check_secrets(project)
    pinned = None
    if conn is not None and run_id is not None:
        pinned = pipelines.run_pipelines(conn, [run_id]).get(run_id)
    if not pinned:
        return [{**check, "secrets": secrets}
                for check in current_checks(project) if check["after"] == after]
    stages = [stage for stage in pinned.get("stages", []) if "after" in stage]
    raw, held = record.pinned_raw_checks(conn, run_id) or ((), frozenset())
    if len(raw) == len(stages):
        return [{**check, "id": stage["id"], "secrets": secrets | held}
                for stage, check in zip(stages, raw) if check["after"] == after]
    return [{"id": stage["id"], "after": stage["after"], "mode": stage["mode"],
             "paths": stage.get("paths") or [], "secrets": secrets,
             "commands": None if stage.get("redacted") else stage["commands"]}
            for stage in stages if stage["after"] == after]


def skip_reason(check, wt, base_sha, sha):
    if check["mode"] == "off":
        return "mode off"
    if not check["paths"]:
        return None
    changed = _changed_files(wt, review_base(wt, base_sha, sha), sha)
    if any(matching(path, check["paths"]) for path in changed):
        return None
    return f"no changed file matches {', '.join(check['paths'])}"


def ran(project, check, wt, sha):
    outcome, found = "error", {}
    try:
        scratch = tempfile.TemporaryDirectory(prefix="holo-check-")
        with scratch as root:
            tree = Path(root, "checkout")
            git(wt, "worktree", "add", "--detach", str(tree), sha)
            try:
                outcome, found = _outcome(project, check, tree)
            finally:
                cleanup_error = _remove_worktree(wt, tree, scratch)
                if cleanup_error:
                    found["cleanup_error"] = cleanup_error
    except Exception as error:
        found["error"] = str(error)
    return outcome, {key: scrubbed_output(value, check["secrets"])
                     if key in ("output", "error", "cleanup_error") else value
                     for key, value in found.items()}


def _remove_worktree(wt, tree, scratch):
    args = ("worktree", "remove", "--force", "--force", str(tree))
    try:
        git(wt, *args)
    except subprocess.CalledProcessError:
        try:
            scratch.cleanup()
            git(wt, *args)
        except (OSError, subprocess.CalledProcessError) as error:
            return f"could not remove {tree}: {error}"
    return None


def _outcome(project, check, wt):
    started = []
    try:
        status, out = _verify_command(project, " && ".join(check["commands"]),
                                      wt, verify_config(project).timeout_sec,
                                      on_start=lambda proc: _held(proc, started),
                                      disposable_worktree=True)
    except subprocess.TimeoutExpired as expired:
        return "fail", {"exit_status": None, "timed_out": True,
                        "output": _text(expired.output)}
    except Exception as error:
        return "error", {"error": str(error)}
    finally:
        for proc in started:
            _end_group(proc.pid)
    out = _text(out)
    if status == 0:
        return "pass", {"exit_status": 0, "output": out}
    last = out.splitlines()[-1] if out.strip() else ""
    unlaunched = status in (126, 127) and bool(UNLAUNCHED.match(last))
    return "error" if unlaunched else "fail", {
        "exit_status": status, "timed_out": False, "output": out}


def _held(proc, started):
    started.append(proc)
    if isinstance(proc.stdout, io.TextIOWrapper):
        proc.stdout.reconfigure(errors="replace")


def _end_group(group):
    deadline = time.monotonic() + GROUP_GRACE_S
    try:
        os.killpg(group, signal.SIGKILL)
        while time.monotonic() < deadline:
            os.killpg(group, 0)
            time.sleep(0.02)
    except (ProcessLookupError, PermissionError):
        pass


def _text(out):
    if isinstance(out, bytes):
        return out.decode(errors="replace")
    return out or ""


def scrubbed_output(text, secrets):
    text = outbound(text, secrets)
    for secret in sorted(secrets, key=len, reverse=True):
        for size in range(len(secret) - 1, FRAGMENT - 1, -1):
            for part in (secret[:size], secret[-size:]):
                text = text.replace(part, REDACTED)
    return text[-OUTPUT_TAIL:]
