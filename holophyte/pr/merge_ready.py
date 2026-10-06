from __future__ import annotations

import subprocess
from dataclasses import dataclass

import store.read
from holophyte.config.config_tables import merge_config
from holophyte.loop.gates import InfraFailure
from holophyte.pr import pr_status
from holophyte.pr.pr_head import remote_head

PARKED_PHASE = "awaiting_merge_approval"
NOT_READ = "not read from GitHub: the run is not parked for a human's merge"
CHECK_REASONS = {"pending": "checks_pending", "failure": "checks_failing"}
MERGEABLE_REASONS = {"MERGEABLE": None, "CONFLICTING": "conflicting"}
UNREADABLE = "github_unreadable"


@dataclass(frozen=True)
class Readiness:
    run_id: int
    park: store.read.ParkFacts | None
    facts: tuple
    head_sha: str | None

    @property
    def failing(self):
        return next((fact for fact in self.facts if not fact[1]), None)

    @property
    def reason(self):
        return None if self.failing is None else self.failing[3]

    @property
    def detail(self):
        return None if self.failing is None else self.failing[2]

    def answer(self):
        park = self.park
        return {"run": self.run_id,
                "ticket": park.identifier if park else None,
                "pr_url": park.pr_url if park else None,
                "head_sha": self.head_sha, "ready": self.failing is None,
                "reason": self.reason, "detail": self.detail,
                "facts": [{"name": name, "ok": ok, "detail": detail}
                          for name, ok, detail, _ in self.facts]}


@dataclass(frozen=True)
class GithubReads:
    state: object
    remote: str | None
    failure: str | None


def _parked(project, run_id, park):
    if park is None:
        return f"no run {run_id}", "not_parked"
    run, ticket = f"run {run_id}", park.identifier
    for broken, why in (
            (not park.newest, f"{run} is not {ticket}'s newest run"),
            (park.phase != PARKED_PHASE,
             f"{run} is {park.phase}, not {PARKED_PHASE}"),
            (park.active_run_id is not None,
             f"{ticket} has run {park.active_run_id} live"),
            (park.ticket_status != "blocked_on_operator",
             f"{ticket} is {park.ticket_status}, not blocked_on_operator"),
            (pr_status.parse_pr_url(park.pr_url) is None,
             f"{run} was parked with no pull request"),
            (not park.branch, f"{run} records no branch")):
        if broken:
            return why, "not_parked"
    return (f"{run} is {ticket}'s newest run, parked {PARKED_PHASE} on"
            f" {park.pr_url}"), None


def _human_approval(project, run_id, park):
    approve = merge_config(project).approve
    if approve != "human":
        return f'[merge] approve is "{approve}", not "human"', \
            "not_human_approval"
    return '[merge] approve is "human"', None


def _review(park, reads):
    decision = (reads.state.review or "none").upper()
    return (f"GitHub's review decision is {decision}",
            None if decision == "APPROVED" else "review_not_approved")


def _checks(park, reads):
    state = reads.state
    named = [*(c.name for c in state.failed_checks), *state.pending_contexts]
    return (f"the required checks are {state.checks}"
            + (f": {', '.join(named)}" if named else ""),
            CHECK_REASONS.get(state.checks))


def _mergeable(park, reads):
    mergeable = reads.state.mergeable
    return (f"GitHub's mergeable is {mergeable}",
            MERGEABLE_REASONS.get(mergeable, "mergeable_unknown"))


def _threads(park, reads):
    threads = reads.state.threads
    if threads:
        return (f"{len(threads)} review thread(s) open, first {threads[0].url}",
                "threads_unresolved")
    return "no review thread is open", None


def _head(park, reads):
    branch, remote, pr_head = park.branch, reads.remote, reads.state.head_sha
    if remote is None:
        return f"origin's head of {branch} is unreadable: {reads.failure}", \
            UNREADABLE
    if remote != pr_head:
        return (f"origin's {branch} is at {remote}, the pull request's head"
                f" at {pr_head}"), "head_moved"
    if remote not in (park.candidate_sha, park.approved_sha):
        return (f"{branch} is at {remote}, neither the parked candidate"
                f" {park.candidate_sha} nor the approved {park.approved_sha}"), \
            "head_moved"
    return f"origin's {branch} and the pull request are at {remote}", None


LOCAL_FACTS = (("parked", _parked), ("human_approval", _human_approval))
GITHUB_FACTS = (("review_approved", _review), ("checks_passed", _checks),
                ("mergeable", _mergeable), ("threads_resolved", _threads),
                ("head_unchanged", _head))


def read_github(project, park):
    try:
        state = pr_status.pr_state(project,
                                   pr_status.parse_pr_url(park.pr_url))
    except InfraFailure as failure:
        return GithubReads(None, None, str(failure))
    try:
        remote = remote_head(project.path, park.branch)
    except (InfraFailure, subprocess.TimeoutExpired, OSError) as failure:
        return GithubReads(state, None, str(failure))
    return GithubReads(state, remote, None)


def readiness(project, run_id):
    conn = store.read.open_readonly(project.store_path)
    try:
        park = store.read.park_facts(conn, run_id)
    finally:
        conn.close()
    facts = [(name, reason is None, detail, reason) for name, check
             in LOCAL_FACTS for detail, reason in [check(project, run_id, park)]]
    reads = None
    if all(ok for _, ok, _, _ in facts):
        reads = read_github(project, park)
    for name, check in GITHUB_FACTS:
        if reads is None:
            facts.append((name, False, NOT_READ, None))
        elif reads.state is None:
            facts.append((name, False, f"GitHub is unreadable: {reads.failure}",
                          UNREADABLE))
        else:
            detail, reason = check(park, reads)
            facts.append((name, reason is None, detail, reason))
    return Readiness(run_id, park, tuple(facts),
                     reads.remote if reads is not None else None)
