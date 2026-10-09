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
REVIEW_FACT = "review_approved"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
REVIEW_BYPASSABLE = "review_bypassable"
BYPASSING_ANSWERS = frozenset({"always", "pull_requests_only", "exempt"})


@dataclass(frozen=True)
class Readiness:
    run_id: int
    park: store.read.ParkFacts | None
    facts: tuple
    head_sha: str | None
    yielding: frozenset = frozenset()

    @property
    def failing(self):
        failing = [fact for fact in self.facts if not fact[1]]
        return next((fact for fact in failing
                     if fact[0] not in self.yielding),
                    failing[0] if failing else None)

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
GITHUB_FACTS = ((REVIEW_FACT, _review), ("checks_passed", _checks),
                ("mergeable", _mergeable), ("threads_resolved", _threads),
                ("head_unchanged", _head))


class NoBypass(Exception):
    pass


def _asked_reviews(rule):
    parameters = rule.get("parameters")
    parameters = parameters if isinstance(parameters, dict) else {}
    count = parameters.get("required_approving_review_count")
    owners = parameters.get("require_code_owner_review", False)
    last_push = parameters.get("require_last_push_approval", False)
    reviewers = parameters.get("required_reviewers", [])
    if (type(count) is not int or count < 0 or type(owners) is not bool
            or type(last_push) is not bool or type(reviewers) is not list):
        raise NoBypass(f"the pull_request rule of ruleset"
                       f" {rule.get('ruleset_id')} cannot be made out")
    return [phrase for phrase, asked in (
        (f"{count} approving review{'' if count == 1 else 's'}", count > 0),
        ("a code owner's review", owners),
        ("approval of the most recent push", last_push),
        (f"{len(reviewers)} required reviewer group(s)", bool(reviewers)))
        if asked]


def _review_rulesets(rules):
    if not isinstance(rules, list):
        raise NoBypass("the rules on main cannot be made out")
    asking = {}
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("type"), str):
            raise NoBypass("a rule on main cannot be made out")
        if rule["type"] != "pull_request":
            continue
        asked = _asked_reviews(rule)
        ruleset = rule.get("ruleset_id")
        if asked and type(ruleset) is not int:
            raise NoBypass("a pull_request rule on main names no ruleset")
        if asked:
            asking.setdefault(ruleset, []).extend(asked)
    if not asking:
        raise NoBypass("no ruleset on main asks for a review")
    return asking


def _ruleset_bypass(project, pull, ruleset, asked):
    try:
        answer = pr_status.rest(
            project, pull, "GET",
            f"repos/{pull.owner}/{pull.name}/rulesets/{ruleset}")
    except InfraFailure as failure:
        raise NoBypass(f"ruleset {ruleset} is unreadable: {failure}") \
            from failure
    answer = answer if isinstance(answer, dict) else {}
    name = answer.get("name")
    if not isinstance(name, str) or not name:
        raise NoBypass(f"ruleset {ruleset} answers no name")
    bypass = answer.get("current_user_can_bypass")
    if not isinstance(bypass, str) or bypass not in BYPASSING_ANSWERS:
        raise NoBypass(f"ruleset {name} answers current_user_can_bypass"
                       f" {bypass if isinstance(bypass, str) else 'nothing'}")
    return (f"ruleset {name} asks {' and '.join(asked)},"
            f" current_user_can_bypass {bypass}")


def _review_bypass(project, park):
    required = f"GitHub's review decision is {REVIEW_REQUIRED}"
    pull = pr_status.parse_pr_url(park.pr_url)
    try:
        try:
            rules = pr_status.main_rules(project, pull)
        except InfraFailure as failure:
            raise NoBypass(f"the rules on main are unreadable: {failure}") \
                from failure
        bypasses = [_ruleset_bypass(project, pull, ruleset, asked)
                    for ruleset, asked in _review_rulesets(rules).items()]
    except NoBypass as why:
        return f"{required}; no bypass: {why}", "review_not_approved"
    return (f"{required}; the host's GitHub user may bypass it: "
            + "; ".join(bypasses)), REVIEW_BYPASSABLE


def _github_facts(project, park, reads):
    if reads is None:
        return [(name, False, NOT_READ, None) for name, _ in GITHUB_FACTS]
    if reads.state is None:
        return [(name, False, f"GitHub is unreadable: {reads.failure}",
                 UNREADABLE) for name, _ in GITHUB_FACTS]
    facts = [(name, reason is None, detail, reason) for name, check
             in GITHUB_FACTS for detail, reason in [check(park, reads)]]
    others_hold = all(ok for name, ok, _, _ in facts if name != REVIEW_FACT)
    if not _review_required(reads) or not others_hold:
        return facts
    detail, reason = _review_bypass(project, park)
    return [(name, False, detail, reason) if name == REVIEW_FACT
            else (name, *rest) for name, *rest in facts]


def _review_required(reads):
    return reads is not None and reads.state is not None \
        and (reads.state.review or "").upper() == REVIEW_REQUIRED


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
    facts.extend(_github_facts(project, park, reads))
    return Readiness(run_id, park, tuple(facts),
                     reads.remote if reads is not None else None,
                     frozenset({REVIEW_FACT}) if _review_required(reads)
                     else frozenset())
