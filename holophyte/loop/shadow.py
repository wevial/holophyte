import json
import os
import subprocess
from dataclasses import dataclass
from time import monotonic

import store
from holophyte.agents.agent_output import claude_result
from holophyte.agents.agent_routes import route_prose
from holophyte.agents.harness import SHADOW_KEY, shadow_seat
from holophyte.agents.probes import (
    PROBE_GOAL,
    PROBE_TIMEOUT,
    probe_diagnostic,
    probe_launch,
)
from holophyte.config.config_tables import verify_config
from holophyte.isolation import launcher
from holophyte.loop.gates import run_capped, run_verify, sh, with_baseline
from holophyte.loop.task_worktree import run_worktree_setup
from holophyte.redact import known_secrets, outbound, redact_values
from holophyte.redact import safe_print as print


@dataclass(frozen=True)
class ShadowBrief:
    goal: str
    verify: str
    contracts: list
    base_sha: str
    branch: str
    seconds: float


def shadow_label(seat):
    return " ".join([seat.name, *(seat.options[key] for key in ("model", "effort")
                                  if key in seat.options)])


def run_shadow(project, conn, run_id, brief):
    seat = shadow_seat(project)
    if seat is None:
        return None
    slug = brief.branch.split("/", 1)[-1]
    branch, wt = f"shadow/{slug}", project.worktrees / f"{slug}.shadow"
    result = {"route": outbound(shadow_label(seat), known_secrets(project.config())),
              "branch": branch, "base_sha": brief.base_sha, "head_sha": None,
              "commits": 0, "lines_changed": 0, "seconds": 0, "exit_status": None,
              "timed_out": False, "usage": None, "verify": None, "outcome": None,
              "detail": None}
    cut = []
    try:
        try:
            _attempt(project, seat, brief, branch, wt, result, cut)
        except Exception as error:
            result["outcome"], result["detail"] = "error", str(error)
        if result["detail"] is not None:
            result["detail"] = route_prose(project, result["detail"])
        minutes = result["seconds"] / 60
        store.record_event(conn, run_id, "shadow_result",
                           f"Shadow {result['route']}: {result['outcome']} in "
                           f"{minutes:.1f} min", level="detail",
                           payload=json.dumps(result))
    finally:
        if cut and wt.exists():
            _remove_worktree(project, wt)
    return result


def _attempt(project, seat, brief, branch, wt, result, cut):
    probe = probe_launch(project, seat.turn(PROBE_GOAL), PROBE_TIMEOUT, SHADOW_KEY)
    if not probe.ok:
        result["outcome"] = "route_down"
        result["detail"] = probe_diagnostic(project, probe)
        return
    known = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=project.path, capture_output=True).returncode == 0
    if known or os.path.lexists(wt):
        result["outcome"] = "branch_exists"
        return
    cut.append(wt)
    sh(["git", "worktree", "add", "--quiet", "-b", branch, str(wt), brief.base_sha],
       project.path)
    ready, output = run_worktree_setup(project, wt)
    if not ready:
        result["outcome"], result["detail"] = "setup_failed", output
        return
    _turn(project, seat, brief, wt, result)
    head = sh(["git", "rev-parse", "HEAD"], wt)
    result["head_sha"] = head
    result["commits"] = int(sh(["git", "rev-list", "--count",
                                f"{brief.base_sha}..{head}"], wt))
    result["lines_changed"] = sum(
        int(count) for line in sh(["git", "diff", "--numstat", brief.base_sha, head],
                                  wt).splitlines()
        for count in line.split("\t")[:2] if count.isdigit())
    ok, out = run_verify(brief.verify, wt, brief.contracts,
                         verify_config(project).timeout_sec, project=project)
    ok, out = with_baseline(project, wt, brief.verify, ok, out)
    failure = getattr(out, "failure", None)
    result["verify"] = {"ok": ok, "failed_command": None if ok or not failure
                        else failure["command_index"]}
    result["outcome"] = ("timed_out" if result["timed_out"] else
                         "crashed" if result["exit_status"] else
                         "no_commits" if not result["commits"] else
                         "verified" if ok else "verify_failed")


def _turn(project, seat, brief, wt, result):
    argv = seat.turn(outbound(brief.goal, known_secrets(project.config())))
    started = monotonic()
    try:
        code, out = launcher.launch(launcher.turn_route(project, argv), wt,
                                    launcher.environment(project), argv,
                                    timeout=brief.seconds, runner=run_capped,
                                    project=project, keep_session=True)
    except subprocess.TimeoutExpired:
        code, out = None, ""
        result["timed_out"] = True
    result["seconds"] = round(monotonic() - started, 3)
    result["exit_status"] = code
    decoded = claude_result(out) if seat.name == "claude" else None
    if decoded is not None:
        result["usage"] = decoded[1]


def _remove_worktree(project, wt):
    try:
        sh(["git", "worktree", "remove", "--force", str(wt)], project.path)
    except RuntimeError as error:
        print(f"[holo2] shadow worktree {wt} not removed: {redact_values(str(error))}")
