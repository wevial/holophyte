#!/usr/bin/env python3
"""Linear provider for holo2.

Uses a personal API key from LINEAR_API_KEY (env var or .env next to this
file) against the direct GraphQL API. The project and team are parameters of
the calls that need them, never module state.

Loop-facing API: claim_next() / fetch_task() / set_state() / comment() /
list_ready_issues() / ready_issues() / closed_identifiers(). Operator API, for
`--file-ticket`: create_issue() / add_blocker() / fetch_description().
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
# carries the pair the target's `[board]` table names. The only variable
# this module reads from the environment is `LINEAR_API_KEY`, at the first
# request; there is no script mode, because a claim outside the loop's
# store lease is how two loops once claimed from one project.


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
        identifier id url title description
        estimate priority
        labels { nodes { id name } }
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

    `url` is the issue's page on Linear, when the query carried it: it is
    what a written pull request body links at its end (KO-336), and nothing
    else reads it, so an issue without one is a task without the key's value.

    `labels` is the names of the issue's labels, in the order Linear lists
    them, or [] for a query that did not ask (`ISSUE_QUERY`). The claim
    reads it for another writer's `holo:` lease label (KO-351) and nothing
    else does; the store never mirrors it.

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
            "priority": issue.get("priority"),
            "url": issue.get("url"),
            "labels": label_names(issue)}


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


def label_names(issue):
    """The names of `issue`'s labels as the ready query lists them; [] when
    the query did not ask for them."""
    return [n["name"] for n in ((issue.get("labels") or {}).get("nodes") or [])]


def _label_ids_of(issue_id):
    """The ids of the labels `issue_id` carries now, by name."""
    data = _gql('query($id: String!) { issue(id: $id) { labels { nodes { id '
                'name } } } }', {"id": issue_id})
    issue = data.get("issue")
    if not issue:
        raise RuntimeError(f"Linear has no issue {issue_id!r}")
    return {n["name"]: n["id"] for n in issue["labels"]["nodes"]}


def _label_id(name, team):
    """The id of `team`'s label called `name`, created on first use.

    A team label rather than a workspace one, so the lease labels of one
    board's team do not have to exist in every other team's picker; the
    lookup is by name within the team for the same reason."""
    data = _gql('query($name: String!, $team: String!) { issueLabels(filter: '
                '{ name: { eq: $name }, team: { name: { eq: $team } } }) '
                '{ nodes { id } } }', {"name": name, "team": team})
    nodes = data["issueLabels"]["nodes"]
    if nodes:
        return nodes[0]["id"]
    made = _gql('mutation($input: IssueLabelCreateInput!) { issueLabelCreate('
                'input: $input) { success issueLabel { id } } }',
                {"input": {"name": name, "teamId": _team_id(team)}})
    if not made["issueLabelCreate"]["success"]:
        raise RuntimeError(f"Linear refused to create the label {name!r}")
    return made["issueLabelCreate"]["issueLabel"]["id"]


def _add_label(issue_id, label_id, what):
    """`issueUpdate` with `addedLabelIds`: attach one label and touch no
    other. Additive on purpose -- the whole-list `labelIds` form would let
    a write built from a moment-old read drop a label another writer or a
    human attached in between, which is the one thing a lease write must
    never do. Raises when Linear says it did not land, for the reason
    `set_state()` gives -- a refusal comes back as `success: false` with no
    `errors` block."""
    data = _gql('mutation($id: String!, $labels: [String!]!) { issueUpdate(id: '
                '$id, input: { addedLabelIds: $labels }) { success } }',
                {"id": issue_id, "labels": [label_id]})
    if not data["issueUpdate"]["success"]:
        raise RuntimeError(f"Linear refused to {what} on issue {issue_id}")


def _remove_label(issue_id, label_id, what):
    """`issueUpdate` with `removedLabelIds`: detach one label and touch no
    other; see `_add_label()`."""
    data = _gql('mutation($id: String!, $labels: [String!]!) { issueUpdate(id: '
                '$id, input: { removedLabelIds: $labels }) { success } }',
                {"id": issue_id, "labels": [label_id]})
    if not data["issueUpdate"]["success"]:
        raise RuntimeError(f"Linear refused to {what} on issue {issue_id}")


def label_issue(issue_id, name, team):
    """Add `team`'s label `name` to the issue, creating the label on first use.

    The board half of a claim (KO-351): the loop calls it after the store
    lease so a second writer with a store of its own sees, in the ready
    column, that this one holds the ticket. Additive -- `addedLabelIds`,
    never the whole-list `labelIds`, so a write built from a moment-old
    read cannot drop a label another writer or a human attached in between
    -- and it raises when Linear refuses, so the caller can give the store
    lease back rather than start a run no other writer can see. Linear has
    no compare-and-swap on labels: the write decides nothing by itself, and
    the caller reads the issue back (`issue_labels()`) to learn whether
    another writer's lease landed beside its own.
    """
    _add_label(issue_id, _label_id(name, team), f"add the label {name!r}")


def issue_labels(issue_id):
    """The names of the issue's labels as Linear holds them now: the
    read-back a lease write is judged by (KO-351). Raises when Linear
    cannot be asked, or has no such issue."""
    return list(_label_ids_of(issue_id))


def unlabel_issue(issue_id, name):
    """Remove the label `name` from the issue; a no-op when it is not there.

    The close-out half of the lease label: called when the run ends in any
    terminal state, and by `--requeue`, so the label never outlives the
    store lease it mirrors. A label the issue does not carry is nothing to
    remove, not an error -- the label may have gone with an earlier
    close-out, or a human may have taken it off. `removedLabelIds`, so the
    ticket's other labels are not rewritten from this read.
    """
    have = _label_ids_of(issue_id)
    if name not in have:
        return
    _remove_label(issue_id, have[name], f"remove the label {name!r}")


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


def ready_issues(project_id):
    """Every issue `claim_next()` chooses from, parsed: the ready listing as
    `list_ready_issues()` filters it -- Todo/started, open, unblocked -- in
    the shape `parse_task()` gives a claimed task. The loop mirrors this
    list at each claim so the Board shows the queue and not only the one
    ticket picked from it (KO-334); Backlog is not in it because the loop
    could not claim it. Reads only; nothing is written to Linear.
    """
    return [parse_task(issue) for issue in list_ready_issues(project_id)]


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


# Issues named by identifier, with their state type and whether Linear has
# archived them. An identifier is the team's key and the issue's number
# (`KO-217`), and those two are what the filter takes: IssueFilter has no
# `identifier` field, so the pair is the identifier spelled in the terms the
# API filters on. `includeArchived` is what makes an archived issue come back
# at all -- Linear omits them by default, and it archives a Done issue on its
# own after a while, which is how a finished ticket stayed on the board as a
# ghost -- and there is no state filter because an archived issue whose state
# is still open is one the caller reports too. The number filter bounds the
# answer to the identifiers asked, so it is still a small page.
CLOSED_QUERY = """
query($key: String!, $numbers: [Float!]!, $after: String) {
  issues(
    first: 50
    after: $after
    includeArchived: true
    filter: {
      team: { key: { eq: $key } }
      number: { in: $numbers }
    }
  ) {
    pageInfo { hasNextPage endCursor }
    nodes { identifier archivedAt state { type } }
  }
}"""

IDENTIFIER_RE = re.compile(r"\A([A-Za-z0-9]+)-(\d+)\Z")


def closed_identifiers(identifiers):
    """Which of `identifiers` Linear holds closed, as identifier -> state type.

    One query per team key named in `identifiers` -- one, for a board whose
    identifiers share a prefix -- rather than one per identifier: the loop
    asks this of every open mirrored ticket at startup (`_reconcile_mirror`),
    and a store can hold tens of them. The value is the state *type*, one of
    `CLOSED_STATE_TYPES`, so the caller tells a finished ticket from a
    cancelled one without learning the team's state names; an open identifier
    or one Linear has no issue for is simply absent. Archived issues are
    included: an archived issue with a closed state answers that state, and
    an archived issue whose state is still open answers `canceled`, since an
    archived issue is one nobody will work. A read, like
    `fetch_task()`: nothing here moves a ticket. An identifier not of the
    `KEY-n` shape is skipped rather than sent, since the filter could not
    name it.
    """
    by_key = {}
    for identifier in identifiers:
        m = IDENTIFIER_RE.match(identifier)
        if m:
            by_key.setdefault(m.group(1), []).append(int(m.group(2)))
    asked = set(identifiers)
    closed = {}
    for key, numbers in by_key.items():
        nodes = _paginate(CLOSED_QUERY, {"key": key, "numbers": numbers},
                          ("issues",))
        for node in nodes:
            if node["identifier"] not in asked:
                continue
            state_type = (node.get("state") or {}).get("type")
            if state_type in CLOSED_STATE_TYPES:
                closed[node["identifier"]] = state_type
            elif node.get("archivedAt"):
                closed[node["identifier"]] = "canceled"
    return closed


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


def blockers_of(identifier):
    """The identifiers of the issues blocking `identifier`, in Linear's order.

    Read through the issue's own `inverseRelations` -- the edges whose
    related issue is this one -- so a `blocks` edge, whose source is the
    blocking issue (see `list_ready_issues()`), shows up here without the
    project sweep that function does. One issue query; the other edge types
    are dropped.
    """
    data = _gql(
        'query($id: String!) { issue(id: $id) { inverseRelations { nodes '
        '{ type issue { identifier } } } } }',
        {"id": identifier})
    if not data.get("issue"):
        raise RuntimeError(f"Linear has no issue {identifier!r}")
    return [rel["issue"]["identifier"]
            for rel in data["issue"]["inverseRelations"]["nodes"]
            if rel["type"] == "blocks"]


def update_issue(identifier, title, body, estimate):
    """Replace the title, description and estimate of the issue `identifier`
    and return its UUID.

    Everything else about the issue -- state, priority, relations -- is left
    as it is: the input names only the three fields a ticket file is the
    source of truth for. The identifier is resolved the way `add_blocker()`
    resolves its blocker, so an issue that does not exist raises before any
    mutation is sent, and a `success: false` is raised like `set_state()`'s:
    a body that was not stored must not print as one that was. The UUID
    comes back so the caller can relate the issue without resolving it
    twice.
    """
    issue_id = _issue_id(identifier)
    data = _gql(
        'mutation($id: String!, $input: IssueUpdateInput!) { issueUpdate('
        'id: $id, input: $input) { success } }',
        {"id": issue_id,
         "input": {"title": title, "description": body,
                   "estimate": estimate}})
    if not data["issueUpdate"]["success"]:
        raise RuntimeError(f"Linear refused to update issue {identifier}")
    return issue_id


def fetch_description(identifier):
    """The description Linear stores for `identifier`, as it stores it."""
    data = _gql('query($id: String!) { issue(id: $id) { description } }',
                {"id": identifier})
    if not data.get("issue"):
        raise RuntimeError(f"Linear has no issue {identifier!r}")
    return data["issue"]["description"] or ""

