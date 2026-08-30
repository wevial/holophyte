#!/usr/bin/env python3
"""Linear provider for holo2.

Uses a personal API key from LINEAR_API_KEY (env var or .env next to this
file) against the direct GraphQL API.

Loop-facing API: claim_next() / set_state() / comment() / list_ready_issues().
"""
import json
import os
import re
import urllib.request
from pathlib import Path

import ticket_template

HERE = Path(__file__).parent
TEAM = "Personal Projects"
GRAPHQL = "https://api.linear.app/graphql"


def _load_env_var(name):
    if os.environ.get(name):
        return os.environ[name]
    env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


# Target Linear project. Configured via HOLO2_PROJECT_ID (env or .env) —
# never hardcoded here, so public history stays free of internal IDs.
PROJECT_ID = _load_env_var("HOLO2_PROJECT_ID")
if not PROJECT_ID:
    raise RuntimeError(
        "HOLO2_PROJECT_ID not set — add it to .env next to this file "
        "(Linear project UUID to drive)")


def _load_env_key():
    return _load_env_var("LINEAR_API_KEY")


def _gql(query, variables=None):
    key = _load_env_key()
    if not key:
        raise RuntimeError("LINEAR_API_KEY not set (env or .env)")
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(GRAPHQL, data=body, headers={
        "Authorization": key, "Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=30))
    if r.get("errors"):
        raise RuntimeError(f"Linear GraphQL error: {r['errors']}")
    return r["data"]


# --- Loop-facing provider API -------------------------------------------------

READY_QUERY = """
query($project: String!) {
  project(id: $project) {
    issues(first: 50, filter: { state: { type: { nin: ["completed", "canceled", "backlog"] } } }) {
      nodes {
        identifier id title description
        estimate
        state { type name }
        relations { nodes { type relatedIssue { identifier state { type } } } }
      }
    }
  }
}"""


def list_ready_issues(project_id=PROJECT_ID):
    """Triaged (Todo/started), non-terminal, unblocked issues in the project.

    Note: Linear stores a blocker as an edge with type=blocks whose SOURCE is
    the blocking issue; the target issue's own relations list does not include
    it. So we fetch the whole project's relations once and invert.
    """
    data = _gql(READY_QUERY, {"project": project_id})
    issues = data["project"]["issues"]["nodes"]

    all_q = """query($project: String!) {
      project(id: $project) { issues(first: 50) {
        nodes { identifier relations { nodes { type relatedIssue { identifier } } } }
      } }
    }"""
    all_nodes = _gql(all_q, {"project": project_id})["project"]["issues"]["nodes"]
    blocked_by = {}
    for n in all_nodes:
        for rel in n["relations"]["nodes"]:
            if rel["type"] == "blocks":
                blocked_by.setdefault(rel["relatedIssue"]["identifier"],
                                      []).append(n["identifier"])

    ready = []
    for i in issues:
        my_blockers = blocked_by.get(i["identifier"], [])
        # blockers only gate while they are themselves open — but any issue
        # still in the ready query's non-terminal set is open by definition,
        # and completed ones are absent from blocked_by sources only if done;
        # be explicit: check each blocker's state via the map we already have.
        open_blockers = [b for b in my_blockers
                         if b in {x["identifier"] for x in issues}]
        if not open_blockers:
            ready.append(i)
    return ready


def parse_task(issue):
    """Extract task + verify command + literal contract checks from a
    ticketTemplate.md description.

    `contracts` is the optional `## Contract checks` fence as (relative path,
    expected literal) pairs, parsed by ticket_template so the ticket-time rules
    and the gate agree. A ticket without that section yields [], leaving the
    verify gate exactly as it was.

    Two ids come back, because Linear has two: `id` is the human identifier
    ("KO-123") the loop prints and names branches after, and `issue_id` is the
    canonical issue UUID. They are kept apart rather than collapsed because
    the UUID is what correlates an issue across renames and what a webhook
    payload carries, so it is the key the store mirrors a ticket under.

    `criteria` is the "Acceptance criteria" section's items, checked ones
    included: the mirror routes a ticket carrying both criteria and a verify
    command to `ready` and everything else to `needs_spec` (state-model §2),
    so dropping them here would mirror every ticket the loop works as
    under-specced — and an under-specced mirror cannot legally enter
    `in_flight`, which is the status the board is a projection of.
    """
    desc = issue.get("description", "") or ""
    m = re.search(r"## Verify command\(s\)\s*```\n(.*?)```", desc, re.S)
    verify = m.group(1).strip() if m else None
    parsed = ticket_template.parse(desc)
    return {"id": issue["identifier"], "issue_id": issue.get("id"),
            "title": issue["title"].strip(),
            "verify": verify,
            "criteria": [*parsed.acceptance, *parsed.acceptance_done,
                         *parsed.acceptance_other],
            "contracts": parsed.contract_checks,
            "budget_min": int(issue.get("estimate") or 20)}


def _state_id(name):
    data = _gql('query($team: String!) { workflowStates(filter: { team: '
                '{ name: { eq: $team } } }) { nodes { id name type } } }',
                {"team": TEAM})
    # A name the team does not have is a mapping the caller got wrong, and it
    # says so: the bare StopIteration `next()` would raise names neither the
    # state nor the team it was looked for in.
    for s in data["workflowStates"]["nodes"]:
        if s["name"] == name:
            return s["id"]
    raise RuntimeError(f"team {TEAM!r} has no workflow state named {name!r}")


def set_state(issue_id, state_name):
    """Move an issue to the workflow state called `state_name`.

    The only state-writing entry point, because state-model §1 makes Linear a
    notice board Holophyte posts to: the loop's own status lives in the store
    and is projected here by `factory.mirror_push()`, never read back. A
    caller that changes an issue's state from anywhere else is a second
    source of truth for the same fact.

    Raises when Linear says the move did not happen. `issueUpdate` reports a
    refusal it did not treat as an error as `success: false`, with no `errors`
    block for `_gql()` to turn into one, so an unchecked mutation returns
    quietly while the issue stays in the state it was in. The projection's
    whole failure story hangs on that: `factory.mirror_push()` only warns
    about a push that says it did not land, and a silent one leaves a stale
    board nothing in the run's event stream mentions.
    """
    data = _gql('mutation($id: String!, $state: String!) { issueUpdate(id: '
                '$id, input: { stateId: $state }) { success } }',
                {"id": issue_id, "state": _state_id(state_name)})
    if not data["issueUpdate"]["success"]:
        raise RuntimeError(
            f"Linear refused to move issue {issue_id} to {state_name!r}")


def claim_next():
    """First ready issue (by identifier), parsed. None when there is none.

    Claiming no longer moves the issue to In Progress here. The claim's status
    change belongs to the store — the loop transitions its mirror to
    `in_flight` and projects that through `mirror_push()` — so leaving a state
    call in the provider would post the same fact twice, from a place that
    does not know whether the claim actually took the project's lease.
    """
    ready = list_ready_issues()
    if not ready:
        return None
    issue = sorted(ready, key=lambda i: i["identifier"])[0]
    task = parse_task(issue)
    print(f"[holo2] claimed {task['id']}: {task['title']} "
          f"(budget {task['budget_min']} min)")
    return task


def comment(task_id, body):
    _gql('mutation($issue: String!, $body: String!) { commentCreate(input: '
         '{ issueId: $issue, body: $body }) { success } }',
         {"issue": task_id, "body": body})


if __name__ == "__main__":
    t = claim_next()
    print(t)
