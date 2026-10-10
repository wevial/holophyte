import json
import os
import subprocess
from collections import Counter
from dataclasses import dataclass
from time import monotonic

import review_runner
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
from holophyte.agents.roles import REVIEW_TIMEOUT
from holophyte.config.agent_settings import review_mode, review_route, review_tier
from holophyte.config.config_tables import verify_config
from holophyte.config.reader import config_table, review_profile
from holophyte.config.worktree_settings import carry_directories
from holophyte.isolation import launcher
from holophyte.loop.gates import run_capped, run_verify, sh, with_baseline
from holophyte.loop.review_round import round_prompt
from holophyte.loop.task_worktree import run_worktree_setup
from holophyte.redact import known_secrets, outbound, redact_values
from holophyte.redact import safe_print as print
from holophyte.review.reply_parsing import criteria_findings, parse_findings


@dataclass(frozen=True)
class ShadowBrief:
    goal: str
    ticket: str
    criteria: list
    task_id: str
    verify: str
    contracts: list
    base_sha: str
    branch: str
    seconds: float


def shadow_label(seat):
    return " ".join([seat.name, *(seat.options[key] for key in ("model", "effort")
                                  if key in seat.options)])


def shadow_branch(primary):
    return "shadow/" + primary.split("/", 1)[-1]


def run_shadow(project, conn, run_id, brief):
    seat = shadow_seat(project)
    if seat is None:
        return None
    slug = brief.branch.split("/", 1)[-1]
    branch, wt = shadow_branch(brief.branch), project.worktrees / f"{slug}.shadow"
    result = {"route": outbound(shadow_label(seat), known_secrets(project.config())),
              "branch": branch, "base_sha": brief.base_sha, "head_sha": None,
              "commits": 0, "lines_changed": 0, "seconds": 0, "exit_status": None,
              "timed_out": False, "usage": None, "verify": None, "outcome": None,
              "detail": None, "review": None}
    cut = []
    try:
        try:
            _attempt(project, run_id, seat, brief, branch, wt, result, cut)
        except Exception as error:
            result["outcome"], result["detail"] = "error", str(error)
        if result["detail"] is not None:
            result["detail"] = route_prose(project, result["detail"])
        store.record_event(conn, run_id, "shadow_result",
                           f"Shadow {result['route']}: {result['outcome']} in "
                           f"{result['seconds'] / 60:.1f} min", level="detail",
                           payload=json.dumps(result))
    finally:
        if cut and wt.exists():
            _remove_worktree(project, wt)
    return result


def _attempt(project, run_id, seat, brief, branch, wt, result, cut):
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
    failure = out.failure
    result["verify"] = {"ok": ok, "failed_command": None if ok or not failure
                        else failure["command_index"]}
    result["outcome"] = ("timed_out" if result["timed_out"] else
                         "crashed" if result["exit_status"] else
                         "no_commits" if not result["commits"] else
                         "verified" if ok else "verify_failed")
    if result["outcome"] in ("verified", "verify_failed"):
        result["review"] = _review(project, run_id, brief, wt, branch, head, ok,
                                   out)


def _review(project, run_id, brief, wt, branch, head, ok, out):
    if "reviewer" in config_table(project, "agents"):
        return {"verdict": "skipped", "reason": "configured reviewer"}
    model, effort = review_route(project)
    tier = review_tier(project)
    review = {"verdict": None, "findings": {}, "unwitnessed": None,
              "reviewer": review_profile(model, effort), "seconds": 0,
              "error": None}
    started = monotonic()
    try:
        prompt = round_prompt(project, run_id, brief.task_id, wt, brief.base_sha,
                              head, brief.ticket, brief.verify, brief.criteria,
                              review_mode(project), ok, out, evidence=False)
        prompt = _as_primary(prompt, wt, branch, brief.branch)
        reply = review_runner.run_review(
            repo=wt, run_id=run_id, base_sha=brief.base_sha, candidate_sha=head,
            prompt=outbound(prompt, known_secrets(project.config())),
            model=model, effort=effort, profile=review["reviewer"],
            timeout=REVIEW_TIMEOUT, verdicts=None,
            carry=carry_directories(project), service_tier=tier)
        review.update(_judged(project, reply, brief.criteria, wt))
    except Exception as error:
        review.update(verdict="error", error=route_prose(project, str(error)))
    review["seconds"] = round(monotonic() - started, 3)
    return review


def _as_primary(text, wt, branch, primary):
    slug = primary.split("/", 1)[-1]
    return text.replace(wt.name, slug).replace(branch, primary)


def _judged(project, reply, criteria, wt):
    try:
        verdict = review_runner.terminal_verdict(reply)
    except review_runner.ReviewBoundaryError as error:
        return {"verdict": "MALFORMED", "error": route_prose(project, str(error))}
    asked = parse_findings(reply) if verdict == "REQUEST_CHANGES" else []
    return {"verdict": verdict,
            "findings": dict(Counter(finding["severity"] for finding in asked)),
            "unwitnessed": len(criteria_findings(reply, criteria, wt))}


def _turn(project, seat, brief, wt, result):
    argv = seat.turn(outbound(brief.goal, known_secrets(project.config())))
    started = monotonic()
    try:
        code, out = launcher.launch(launcher.turn_route(project, argv), wt,
                                    launcher.environment(project), argv,
                                    timeout=brief.seconds, runner=run_capped,
                                    project=project, keep_session=True)
    except subprocess.TimeoutExpired as expired:
        code, out = None, ""
        result["timed_out"] = True
        partial = expired.output or ""
        if isinstance(partial, bytes):
            partial = partial.decode(errors="replace")
        result["detail"] = (f"{seat.name} turn timed out after {expired.timeout:g}s\n"
                            + (partial.strip()[-2000:] or "(no output)"))
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
