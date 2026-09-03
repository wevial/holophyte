#!/usr/bin/env python3
"""Linear provider for holo2.

Uses a personal API key from LINEAR_API_KEY (env var or .env next to this
file) against the direct GraphQL API. The project and team are parameters of
the calls that need them, never module state.

Loop-facing API: claim_next() / fetch_task() / set_state() / comment() /
list_ready_issues(). Operator API, for `--file-ticket`: create_issue() /
add_blocker() / fetch_description().
"""
import json
import os
import re
import urllib.request
from pathlib import Path

import ticket_template

HERE = Path(__file__).parent
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


# Which project to drive and which team's workflow states to resolve in are
# the caller's to say: `list_ready_issues()`, `_state_id()`, `set_state()`
# and `claim_next()` take them as parameters, and `provider.LinearProvider`
# carries the pair the target's `[board]` table names. Nothing is read from
# the environment at import, so importing this module is not a configuration
# read; the one variable it still reads, `LINEAR_API_KEY`, is read at the
# first request.


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

def _paginate(query, variables, path):
    """Every node of the connection at `path`, walked with Linear's cursors.

    `query` must declare `$after: String` and pass it as the connection's
    `after:`; `path` is the key path from the response root to the
    connection. Linear caps a page at fifty issues by default, and a project
    that has passed fifty would otherwise have its second page silently
    invisible: a ready ticket there is never claimed, and a blocks relation
    whose source sits there is never seen.
    """
    nodes = []
    after = None
    while True:
        data = _gql(query, {**variables, "after": after})
        for key in path:
            data = data[key]
        nodes.extend(data["nodes"])
        page = data.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return nodes
        after = page["endCursor"]


READY_QUERY = """
query($project: String!, $after: String) {
  project(id: $project) {
    issues(
      first: 50
      after: $after
      filter: { state: { type: { nin: ["completed", "canceled", "backlog"] } } }
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        identifier id title description
        estimate priority
        state { type name }
        relations { nodes { type relatedIssue { identifier state { type } } } }
      }
    }
  }
}"""

RELATIONS_QUERY = """
query($project: String!, $after: String) {
  project(id: $project) {
    issues(first: 50, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        identifier state { type }
        relations { nodes { type relatedIssue { identifier } } }
      }
    }
  }
}"""

# A blocker in any state but these still blocks: Backlog and Triage are open.
CLOSED_STATE_TYPES = {"completed", "canceled"}


def list_ready_issues(project_id):
    """Triaged (Todo/started), non-terminal, unblocked issues in the project.

    Note: Linear stores a blocker as an edge with type=blocks whose SOURCE is
    the blocking issue; the target issue's own relations list does not include
    it. So we fetch the whole project's relations once and invert. A blocker
    gates by its own state type — anything not completed/canceled, Backlog
    included — rather than by whether it happens to be in the ready set.
    """
    path = ("project", "issues")
    issues = _paginate(READY_QUERY, {"project": project_id}, path)
    all_nodes = _paginate(RELATIONS_QUERY, {"project": project_id}, path)

    blocked_by = {}
    for n in all_nodes:
        if (n.get("state") or {}).get("type") in CLOSED_STATE_TYPES:
            continue
        for rel in n["relations"]["nodes"]:
            if rel["type"] == "blocks":
                blocked_by.setdefault(rel["relatedIssue"]["identifier"],
                                      []).append(n["identifier"])

    return [i for i in issues if not blocked_by.get(i["identifier"])]


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

    `body` is the description as approved, verbatim: it is the contract the
    implementer turn is given and the reviewer holds the candidate to, so
    parsing hands it through untouched rather than reducing the ticket to the
    fields the loop happens to branch on. It is prompt input only — the store
    mirrors named fields, never this one.

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
            "body": desc,
            "budget_min": int(issue.get("estimate") or 20),
            "priority": issue.get("priority")}


ISSUE_QUERY = """
query($id: String!) {
  issue(id: $id) { identifier id title description estimate }
}"""


def fetch_task(issue_id):
    """Re-read one issue by id and parse it; None when Linear has no such issue.

    The read half of the merge-time drift check: `factory.run_task()` freezes
    the ticket at the claim and asks for it again at the merge gate, so what
    this returns has to be the *same shape* the claim was taken from — hence
    `parse_task()` rather than a second parser that could disagree with it
    about what the body says.

    Deliberately not part of `claim_next()`'s path: nothing here decides what
    to work on, and state-model §1 keeps Linear a notice board. This reads one
    issue's body back, which is the one fact the board is authoritative about
    — a human edits the contract there, not in the store.
    """
    issue = _gql(ISSUE_QUERY, {"id": issue_id})["issue"]
    return parse_task(issue) if issue else None


def _state_id(name, team):
    data = _gql('query($team: String!) { workflowStates(filter: { team: '
                '{ name: { eq: $team } } }) { nodes { id name type } } }',
                {"team": team})
    # A name the team does not have is a mapping the caller got wrong, and it
    # says so: the bare StopIteration `next()` would raise names neither the
    # state nor the team it was looked for in.
    for s in data["workflowStates"]["nodes"]:
        if s["name"] == name:
            return s["id"]
    raise RuntimeError(f"team {team!r} has no workflow state named {name!r}")


def set_state(issue_id, state_name, team):
    """Move an issue to the workflow state called `state_name` of `team`.

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
                {"id": issue_id, "state": _state_id(state_name, team)})
    if not data["issueUpdate"]["success"]:
        raise RuntimeError(
            f"Linear refused to move issue {issue_id} to {state_name!r}")


# Linear's `priority` is 0 (none), 1 (urgent), 2 (high), 3 (medium), 4 (low).
# The claim rank is that scale with the one wrinkle fixed: an unprioritised
# issue is the *least* urgent, not the most, so 0 and an absent field sort
# after 4 rather than before 1.
PRIORITY_RANK = {1: 0, 2: 1, 3: 2, 4: 3}
UNPRIORITISED_RANK = 4


def _claim_key(order):
    """The sort key `claim_next()` orders the ready set by, per `order`."""
    if order == "priority":
        return lambda i: (PRIORITY_RANK.get(i.get("priority"), UNPRIORITISED_RANK),
                          i["identifier"])
    return lambda i: i["identifier"]


def claim_next(project_id, team, skip=(), order="identifier"):
    """First ready issue of `project_id`, parsed. None when there is none.

    `team` is the board's team, carried alongside the project so the pair
    that names a board travels together; the claim itself queries only the
    project.

    `order` is `[loop] order`: `"identifier"` (the default) offers the lowest
    identifier; `"priority"` offers the most urgent Linear priority first,
    identifier ascending within a priority and unprioritised issues last.

    Claiming no longer moves the issue to In Progress here. The claim's status
    change belongs to the store — the loop transitions its mirror to
    `in_flight` and projects that through `mirror_push()` — so leaving a state
    call in the provider would post the same fact twice, from a place that
    does not know whether the claim actually took the project's lease.

    `skip` is the identifiers the caller has already refused on this pass, and
    it exists because "first ready issue" is otherwise the *same* issue every
    time it is asked. A ticket the loop will not claim — one blocked by
    repeated failures — still projects to a column the ready query counts, so
    without a way to ask for the next one after it, one unclaimable ticket at
    the head of the queue starves every ticket behind it forever.
    """
    ready = [i for i in list_ready_issues(project_id)
             if i["identifier"] not in skip]
    if not ready:
        return None
    issue = min(ready, key=_claim_key(order))
    task = parse_task(issue)
    print(f"[holo2] claimed {task['id']}: {task['title']} "
          f"(budget {task['budget_min']} min)")
    return task


def comment(task_id, body):
    _gql('mutation($issue: String!, $body: String!) { commentCreate(input: '
         '{ issueId: $issue, body: $body }) { success } }',
         {"issue": task_id, "body": body})


# --- Operator API: filing a ticket from a file ------------------------------
#
# Not part of the loop's provider protocol: `--file-ticket` is an operator
# command on this module directly, the way `--requeue` is on the store. The
# loop never creates an issue, so `provider.LinearProvider` does not learn to.

def _team_id(team):
    """The id of the team called `team`, looked up by name."""
    data = _gql('query($team: String!) { teams(filter: { name: { eq: $team } '
                '}) { nodes { id } } }', {"team": team})
    nodes = data["teams"]["nodes"]
    if not nodes:
        raise RuntimeError(f"Linear has no team named {team!r}")
    return nodes[0]["id"]


def _issue_id(identifier):
    """The issue UUID behind the human identifier (`KO-n`)."""
    data = _gql('query($id: String!) { issue(id: $id) { id } }',
                {"id": identifier})
    if not data.get("issue"):
        raise RuntimeError(f"Linear has no issue {identifier!r}")
    return data["issue"]["id"]


def create_issue(project_id, team, title, body, estimate, state_name,
                 priority=None):
    """Create an issue in `project_id` for `team` and return its
    `{"id": UUID, "identifier": "KO-n"}`.

    `body` is the description, as markdown; `estimate` is Linear's number;
    `state_name` is resolved in `team`'s workflow the way `set_state()`
    resolves it; `priority` is Linear's integer (1 urgent .. 4 low) and is
    put in the input only when given, so None creates the issue with no
    priority rather than an explicit 0. A `success: false` is raised like
    `set_state()`'s: an issue that was not created must not print as one
    that was.
    """
    fields = {"teamId": _team_id(team), "projectId": project_id,
              "title": title, "description": body, "estimate": estimate,
              "stateId": _state_id(state_name, team)}
    if priority is not None:
        fields["priority"] = priority
    data = _gql(
        'mutation($input: IssueCreateInput!) { issueCreate(input: $input) '
        '{ success issue { id identifier } } }',
        {"input": fields})
    created = data["issueCreate"]
    if not created["success"] or not created.get("issue"):
        raise RuntimeError(f"Linear refused to create issue {title!r}")
    return {"id": created["issue"]["id"],
            "identifier": created["issue"]["identifier"]}


def add_blocker(issue_id, blocker_identifier):
    """Record that the issue `blocker_identifier` blocks the issue `issue_id`.

    The edge is stored the way `list_ready_issues()` reads it: type `blocks`,
    its source the blocking issue, its related issue the one it blocks.
    """
    data = _gql(
        'mutation($input: IssueRelationCreateInput!) { issueRelationCreate('
        'input: $input) { success } }',
        {"input": {"issueId": _issue_id(blocker_identifier),
                   "relatedIssueId": issue_id, "type": "blocks"}})
    if not data["issueRelationCreate"]["success"]:
        raise RuntimeError(
            f"Linear refused to record {blocker_identifier} as blocking "
            f"issue {issue_id}")


def fetch_description(identifier):
    """The description Linear stores for `identifier`, as it stores it."""
    data = _gql('query($id: String!) { issue(id: $id) { description } }',
                {"id": identifier})
    if not data.get("issue"):
        raise RuntimeError(f"Linear has no issue {identifier!r}")
    return data["issue"]["description"] or ""


if __name__ == "__main__":
    # Run as a script there is no target config to read `[board]` from, so
    # the board comes from the environment (or the `.env` beside this file),
    # the same pair `board_config()` falls back to.
    _project_id = _load_env_var("HOLO2_PROJECT_ID")
    _team = _load_env_var("HOLO2_TEAM")
    if not (_project_id and _team):
        raise SystemExit("[holo2] linear_provider.py needs HOLO2_PROJECT_ID "
                         "and HOLO2_TEAM in the environment or .env")
    print(claim_next(_project_id, _team))
