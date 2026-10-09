"""The adversarial pass beside the primary review: brief, reply and re-run rule."""
import contextvars
import json
import re
import threading
from dataclasses import dataclass
from time import monotonic

import store
from holophyte.agents.agent_routes import routes
from holophyte.agents.agent_turns import family_label
from holophyte.agents.fallback import outage_reason
from holophyte.agents.review_workspace import review_refs
from holophyte.board.projection import ledger
from holophyte.config.agent_settings import fallback_entries, review_route
from holophyte.config.reader import ADVERSARY_CLAUDE, adversary_credential
from holophyte.config.review_settings import review_config
from holophyte.loop.gates import InfraFailure
from holophyte.redact import safe_print as print
from holophyte.review.blast_radius import continued_runs, high_path
from holophyte.review.briefs import _changed_files
from holophyte.review.reply_parsing import (
    BLOCK_BREAK_RE,
    FINDING_PATH_RE,
    citation,
    finding_blocks,
    finding_message,
    finding_severity,
    unparsed_path,
)
from store.working import working

DEPTHS = {"high": "full", "medium": "light"}
TIME_BOXES = {"full": 1800, "light": 900}
MAX_SUBAGENTS = 5
DONE = "ADVERSARY: DONE"
REFUSAL = "This content was flagged for possible cybersecurity risk"
BLOCKING = ("reproduced", "traced")
LEVELS = (*BLOCKING, "concern")
EVIDENCE_RE = re.compile(r"^[\s>*`_-]*EVIDENCE[*`_]*:[*`\s]*([^\s*`]*)",
                         re.I | re.M)
LEADING_PATH_RE = re.compile(
    r"^\s*(?:[-*+]|\d+[.)])\s+[`*_]*([\w.\-/]*\w):(\d+)(?![\w.])")
SURFACES = "browser UI, HTTP or API, CLI, data and migrations"
SUBAGENT_RE = re.compile(
    r"^[ \t>*`_-]*SUBAGENT[*`_]*:[ \t*`_]*([^\s*`].*?)[*`_]*[ \t]+[—–-][ \t]+"
    r"[*`_]*([^\s*`].*?)[ \t\r*`_]*$", re.I | re.M)


@dataclass(frozen=True)
class Family:
    name: str
    model: str | None = None
    effort: str | None = None
    reason: str | None = None
    credential: str | None = None

    def route(self):
        if self.name != "claude":
            return None
        return {"harness": "claude", "model": self.model,
                "effort": self.effort, "credential": self.credential}

    def record(self):
        reason = {} if self.reason is None else {"family_reason": self.reason}
        return {"family": self.name, "model": self.model,
                "effort": self.effort, **reason}


@dataclass(frozen=True)
class Pass:
    round: int
    tier: str
    depth: str
    scope: str
    start: str
    sha: str
    family: Family
    concerns: tuple = ()

    @property
    def seconds(self):
        return TIME_BOXES[self.depth]


def _payloads(conn, run_id, kind, rnd):
    return [payload for run in continued_runs(conn, run_id)
            for (text,) in conn.execute(
                "SELECT payload FROM runEvents WHERE runId = ? AND kind = ?"
                " ORDER BY seq", (run, kind))
            if (payload := json.loads(text))["round"] <= rnd]


def gated(project, root, start, sha):
    return sorted(path for path in _changed_files(root, start, sha)
                  if high_path(project, path))


def fallback_family(project, conn, run_id):
    pair = (None if fallback_entries(project, "adversary")
            else review_route(project, fallback=True))
    reasons = [evidence["reason"] for run in continued_runs(conn, run_id)
               for (text,) in conn.execute(
                   "SELECT summary FROM runEvents WHERE runId = ?"
                   " AND kind = 'route_fallback' ORDER BY seq", (run,))
               if (evidence := json.loads(text)).get("seat") == "adversary"]
    return Family("fallback", *(pair or (None, None)),
                  reasons[-1] if reasons else "adversary route on its fallback")


def choose_family(project, conn, run_id, passes):
    if "adversary" in routes(project).commands:
        return fallback_family(project, conn, run_id)
    if "adversary" in (project.config().get("agents") or {}):
        return Family("configured")
    model, effort = review_route(project)
    credential = adversary_credential(project)
    if credential is None:
        return Family("codex", model, effort, "no adversary_credential")
    if (continued_runs(conn, run_id)[0] + passes) % 2:
        return Family("claude", *ADVERSARY_CLAUDE, credential=credential)
    return Family("codex", model, effort)


def planned(project, conn, run_id, root, base, sha, rnd):
    if conn is None or run_id is None or not review_config(project).adversary:
        return None
    tiers = _payloads(conn, run_id, "blast_radius", rnd)
    tier = tiers[-1]["tier"] if tiers and tiers[-1]["round"] == rnd else "low"
    earlier = _payloads(conn, run_id, "adversary_round", rnd - 1)
    family = choose_family(project, conn, run_id, len(earlier))
    if not earlier:
        return (Pass(rnd, tier, DEPTHS[tier], "candidate", base, sha, family)
                if tier in DEPTHS else None)
    previous = [payload["sha"] for payload in tiers if payload["round"] < rnd]
    if not previous or not gated(project, root, previous[-1], sha):
        return None
    concerns = tuple(concern for payload in earlier
                     for concern in payload["concerns"])
    return Pass(rnd, tier, "light", "fix", previous[-1], sha, family, concerns)


def _attackers(depth):
    if depth == "full":
        return (f"one review subagent per surface the diff touches ({SURFACES}) "
                "plus one per risky module")
    return (f"one review subagent per surface the diff touches ({SURFACES}), "
            "and no per-module subagents")


def _target(plan, run_id):
    base_ref, candidate_ref = review_refs(run_id)
    if plan.scope == "candidate":
        return (f"Scope: candidate. Verify the whole candidate, {plan.start}.."
                f"{plan.sha}, using {base_ref} as the frozen base and "
                f"{candidate_ref} as the candidate in this repo.\n")
    listed = "".join(f"- {concern['message']}\n" for concern in plan.concerns)
    return (f"Scope: fix. Verify only the fix range {plan.start}..{plan.sha}; "
            f"{candidate_ref} is the candidate in this repo. Re-check each "
            "concern earlier adversarial passes recorded, and report it again "
            "at the evidence level it now reaches:\n" + (listed or "- (none)\n"))


def brief(plan, ticket, run_id):
    return (
        "You are a READ-ONLY adversarial reviewer. Another reviewer checks "
        "this change against its ticket; your job is to verify its robustness "
        "and find its defects before users do: find inputs where this change "
        "behaves wrongly, check that each guard holds, and reproduce each "
        "defect read-only.\n\n"
        + _target(plan, run_id)
        + f"Depth: {plan.depth}. Time box: {plan.seconds} seconds. Start "
        f"{_attackers(plan.depth)}, and at most {MAX_SUBAGENTS} subagents in "
        "the pass. Give mechanical checks, such as listing every export form "
        "or every ignored path, to a light model (luna for Codex, haiku or "
        "sonnet for Claude), and defect reasoning and reproduction to a "
        "strong one (sol for Codex, opus for Claude). A reproduction runs "
        "read-only against the candidate and leaves the checkout as it found "
        "it. Do not modify anything.\n\n"
        f"The ticket the change implements:\n\n{ticket}\n\n"
        "Report each finding as one list item, `- PATH:LINE [p0|p1|p2] what "
        "goes wrong`, followed by one line naming its evidence level:\n"
        "EVIDENCE: reproduced — give the input and the observed bad result.\n"
        "EVIDENCE: traced — give file:line for each step from the input to "
        "the harm.\n"
        "EVIDENCE: concern — give the scenario and why it cannot be shown "
        "yet.\n"
        "Reproduced and traced findings block the change; concerns are "
        "recorded and do not. After the findings, list each review subagent "
        "you started on one line, `SUBAGENT: MODEL — SURFACE`. End "
        f"your reply with exactly one line:\n{DONE}")


def subagents(reply):
    return [{"model": model, "surface": surface}
            for model, surface in SUBAGENT_RE.findall(str(reply))]


def finished(reply):
    lines = [line.strip() for line in str(reply).splitlines() if line.strip()]
    return bool(lines) and lines[-1] == DONE


def refused(reply):
    return (reply is not None and REFUSAL in str(reply)
            and not finished(reply))


def _evidence(block):
    found = EVIDENCE_RE.search(block)
    level = found.group(1).rstrip(".,;:").lower() if found else "concern"
    return level if level in LEVELS else "concern"


def _indent(block):
    return len(block) - len(block.lstrip())


def _items(reply):
    items, current = [], None
    for block in finding_blocks(SUBAGENT_RE.sub("", str(reply)).replace(DONE, "")):
        nested = current is not None and _indent(block) > _indent(current[0])
        if nested or (current is not None and EVIDENCE_RE.match(block)):
            current.append(block)
        elif BLOCK_BREAK_RE.match(block):
            current = [block]
            items.append(current)
    return items


def parse(reply):
    findings = []
    for parts in _items(reply):
        block = "\n".join(parts)
        match = (LEADING_PATH_RE.match(parts[0])
                 or FINDING_PATH_RE.search(citation(parts[0])))
        if match is None and not EVIDENCE_RE.search(block):
            continue
        message = finding_message(block)
        line = int(match.group(2)) if match and match.group(2) else None
        findings.append({
            "path": match.group(1) if match else unparsed_path(message),
            "line": line if line else None,
            "severity": finding_severity(parts[0]), "message": message,
            "evidence": _evidence(block)})
    return findings


def attack(project, conn, run_id, wt, base, ticket, plan, run_agent):
    started = monotonic()
    goal = brief(plan, ticket, run_id)
    route = plan.family.route()
    for _ in range(2):
        reply = run_agent(project, "adversary", goal, wt, base_sha=base,
                          candidate_sha=plan.sha, timeout=plan.seconds,
                          conn=conn, run_id=run_id, family_route=route)
        claude_down(project, route, reply)
        if finished(reply) or refused(reply):
            return reply, round(monotonic() - started, 3)
        goal += (f"\n\nYour previous reply did not end with {DONE}. Your "
                 f"reply must end with exactly one line, {DONE}, and nothing "
                 "after it.")
    return None, round(monotonic() - started, 3)


def claude_down(project, route, reply):
    if route is None or "adversary" in routes(project).commands:
        return
    reason = outage_reason(family_label(route), str(reply))
    if reason:
        print(f"[holo2] adversary route {family_label(route)} down: {reason}")
        raise InfraFailure(f"adversary route {family_label(route)} down with "
                           f"no fallback: {reason}", "review_route")


def beside(conn, run_id, primary, side):
    if side is None:
        return primary(), None
    outcome = {}

    def run(context):
        try:
            outcome["value"] = context.run(side)
        except BaseException as exc:  # noqa: BLE001 - re-raised after the join
            outcome["error"] = exc
    with working(conn, run_id):
        thread = threading.Thread(target=run, args=(contextvars.copy_context(),),
                                  name=f"adversary-run-{run_id}", daemon=True)
        thread.start()
        try:
            result = primary()
        finally:
            thread.join()
    if "error" in outcome:
        raise outcome["error"]
    return result, outcome["value"]


def settle(project, conn, run_id, provider, task_id, plan, attacked):
    if plan is None:
        return []
    reply, seconds = attacked
    answered = reply is not None and not refused(reply)
    found = parse(reply) if answered else []
    started = subagents(reply) if answered else []
    blocking = [f for f in found if f["evidence"] in BLOCKING]
    concerns = [f for f in found if f["evidence"] not in BLOCKING]
    outcome = ("malformed" if reply is None else
               "refused" if refused(reply) else
               "blocked" if blocking else "clear")
    family = (fallback_family(project, conn, run_id)
              if "adversary" in routes(project).commands else plan.family)
    store.record_event(
        conn, run_id, "adversary_round",
        f"round {plan.round} adversary ({plan.depth}, {plan.scope}): {outcome}",
        level="detail", payload=json.dumps({
            "round": plan.round, "tier": plan.tier, "depth": plan.depth,
            "scope": plan.scope, "range": [plan.start, plan.sha],
            **family.record(), "outcome": outcome, "seconds": seconds,
            "subagents": started, "over_cap": len(started) > MAX_SUBAGENTS,
            "findings": blocking, "concerns": concerns}))
    if outcome == "refused":
        ledger(conn, run_id, task_id, "note",
               f"Round {plan.round} adversary turn was refused by the provider;"
               " the round stands on the primary review.", provider)
        print(f"[holo2] round {plan.round}: adversary turn refused by the "
              "provider; the round stands on the primary review")
    if concerns:
        ledger(conn, run_id, task_id, "note",
               f"Round {plan.round} adversary concerns (non-blocking):\n"
               + _bullets(concerns), provider)
    if blocking:
        print(f"[holo2] round {plan.round}: {len(blocking)} adversary findings "
              "reproduced or traced; treating as REQUEST_CHANGES")
    return [dict(finding, reviewer="adversary") for finding in blocking]


def require_done(plan, attacked):
    if plan is not None and attacked[0] is None:
        print(f"[holo2] round {plan.round}: adversary returned no {DONE} twice")
        raise InfraFailure(f"adversary returned no {DONE} line twice; "
                           f"candidate preserved at {plan.sha}", "review_route")


def _bullets(findings):
    return "\n".join(f"- {f['message'].lstrip('-*+ ')}" for f in findings)
