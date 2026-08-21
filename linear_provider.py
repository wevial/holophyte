#!/usr/bin/env python3
"""Linear provider for holo2.

Uses a personal API key from LINEAR_API_KEY (env var or .env next to this
file) against the direct GraphQL API.

Loop-facing API: claim_next() / complete() / comment() / list_ready_issues().
"""
import json
import os
import re
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
PROJECT_ID = "REDACTED"  # Lotuspod
TEAM = "Personal Projects"
GRAPHQL = "https://api.linear.app/graphql"


def _load_env_key():
    if os.environ.get("LINEAR_API_KEY"):
        return os.environ["LINEAR_API_KEY"]
    env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("LINEAR_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


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
    """Extract task + verify command from a ticketTemplate.md description."""
    desc = issue.get("description", "") or ""
    m = re.search(r"## Verify command\(s\)\s*```\n(.*?)```", desc, re.S)
    verify = m.group(1).strip() if m else None
    return {"id": issue["identifier"], "title": issue["title"].strip(),
            "verify": verify,
            "budget_min": int(issue.get("estimate") or 20)}


def _state_id(name):
    data = _gql('query($team: String!) { workflowStates(filter: { team: '
                '{ name: { eq: $team } } }) { nodes { id name type } } }',
                {"team": TEAM})
    return next(s["id"] for s in data["workflowStates"]["nodes"]
                if s["name"] == name)


def _set_state(issue_id, state_id):
    _gql('mutation($id: String!, $state: String!) { issueUpdate(id: $id, '
         'input: { stateId: $state }) { success } }',
         {"id": issue_id, "state": state_id})


def claim_next():
    """First ready issue (by identifier) -> In Progress. Parsed task or None."""
    ready = list_ready_issues()
    if not ready:
        return None
    issue = sorted(ready, key=lambda i: i["identifier"])[0]
    _set_state(issue["id"], _state_id("In Progress"))
    task = parse_task(issue)
    print(f"[holo2] claimed {task['id']}: {task['title']} "
          f"(budget {task['budget_min']} min)")
    return task


def complete(task_id):
    _set_state(task_id, _state_id("Done"))


def comment(task_id, body):
    _gql('mutation($issue: String!, $body: String!) { commentCreate(input: '
         '{ issueId: $issue, body: $body }) { success } }',
         {"issue": task_id, "body": body})


if __name__ == "__main__":
    t = claim_next()
    print(t)
