#!/usr/bin/env python3
"""The Linear board through its GraphQL API, keyed by LINEAR_API_KEY."""
import fcntl
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from time import localtime, sleep, strftime, time

import ticket_template

HERE = Path(__file__).parent
GRAPHQL = "https://api.linear.app/graphql"
_UNNAMED_RESET_RETRY_MS = 3_600_000


def _load_env_var(name):
    if os.environ.get(name):
        return os.environ[name]
    env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _load_env_key():
    return _load_env_var("LINEAR_API_KEY")


def _clock(epoch_ms):
    return strftime("%H:%M", localtime(epoch_ms / 1000))


class LinearBudgetExhausted(RuntimeError):

    def __init__(self, message, reset_at=None):
        super().__init__(message)
        self.reset_at = reset_at


class LinearBudget:

    def __init__(self, shared=False):
        self.shared = shared
        self._retry_at = None
        self._blocked_until = None
        self.limit = None
        self.remaining = None
        self.reset_at = None
        self._announced_reset = None

    def _shared_until(self, deadline=0):
        # Only the deadline is stored, never the key or board data.
        key = _load_env_key() if self.shared else None
        if not key:
            return 0
        home = Path(os.environ.get("HOLOPHYTE_HOME") or "~/.holophyte").expanduser()
        directory = home / "linear-budget"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / hashlib.sha256(key.encode()).hexdigest()
        with path.open("a+") as state:
            fcntl.flock(state, fcntl.LOCK_EX)
            state.seek(0)
            saved = int(state.read() or "0")
            if deadline > saved:
                state.seek(0)
                state.truncate()
                state.write(str(deadline))
                state.flush()
            return max(saved, deadline)

    def remember(self, headers):
        if headers is None:
            return
        fields = {str(k).lower(): str(v) for k, v in headers.items()}
        if "x-ratelimit-complexity-remaining" in fields:
            # A new reading without a reset must not reuse an expired window.
            self.reset_at = None
            self._retry_at = int(time() * 1000) + _UNNAMED_RESET_RETRY_MS
        for name, attr in (("x-ratelimit-complexity-limit", "limit"),
                           ("x-ratelimit-complexity-remaining", "remaining"),
                           ("x-ratelimit-complexity-reset", "reset_at")):
            if name in fields:
                try:
                    setattr(self, attr, int(fields[name]))
                except ValueError:
                    pass

        if self.limit is not None and self.remaining is not None \
                and self.remaining < self.limit / 10:
            self._shared_until(self.reset_at or self._retry_at or 0)

    def low(self, now=None):
        now = int(time() * 1000) if now is None else now
        deadline = self._shared_until()
        if self.limit is not None and self.remaining is not None \
                and self.remaining < self.limit / 10:
            deadline = max(deadline, self.reset_at or self._retry_at or 0)
        self._blocked_until = deadline
        if now >= deadline:
            if self.limit is not None and self.remaining is not None \
                    and self.remaining < self.limit / 10:
                self.limit = self.remaining = self.reset_at = None
                self._retry_at = None
            self._announced_reset = None
            return False
        return True

    def notice(self, now=None):
        if not self.low(now):
            return None
        state = self._blocked_until
        if state == self._announced_reset:
            return None
        self._announced_reset = state
        return f"board not asked: budget resets at {_clock(state)}"


LINEAR_BUDGET = LinearBudget(shared=True)

# Seconds before each retry of a read. A mutation is never retried: Linear may
# have applied it before answering with the error.
READ_RETRY_WAITS = (2, 5)
_TRANSIENT_CODES = (502, 503, 504)


def _is_read(query):
    text = query.lstrip()
    return text.startswith("{") or re.match(r"query\b", text) is not None


def _transient(error):
    if isinstance(error, urllib.error.HTTPError):
        return error.code in _TRANSIENT_CODES
    return isinstance(error, (urllib.error.URLError, TimeoutError))


def _admit(what):
    # Looked up, not imported, so this module still imports alone.
    bounds = sys.modules.get("holophyte.deadline")
    if bounds is not None:
        bounds.admit(what)


def _urlopen(req, retry, what):
    waits = iter(READ_RETRY_WAITS if retry else ())
    while True:
        _admit(what)
        try:
            return urllib.request.urlopen(req, timeout=30)
        except (urllib.error.URLError, TimeoutError) as e:
            wait = next(waits, None) if _transient(e) else None
            if wait is None:
                raise
            if isinstance(e, urllib.error.HTTPError):
                LINEAR_BUDGET.remember(e.headers)
            _admit(what)
            sleep(wait)


class GraphQLError(RuntimeError):

    def __init__(self, errors):
        super().__init__(f"Linear GraphQL error: {errors}")
        self.errors = errors


def _issue_not_found(error):
    return bool(error.errors) and all(
        isinstance(e, dict)
        and str(e.get("message", "")).startswith("Entity not found: Issue")
        and (e.get("extensions") or {}).get("code") == "INPUT_ERROR"
        for e in error.errors)


def _gql(query, variables=None):
    key = _load_env_key()
    if not key:
        raise RuntimeError("LINEAR_API_KEY not set (env or .env)")
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(GRAPHQL, data=body, headers={
        "Authorization": key, "Content-Type": "application/json"})
    root = re.search(r"\{\s*(\w+)", query)
    what = f"Linear's {root.group(1) if root else 'GraphQL'} request"
    try:
        res = _urlopen(req, retry=_is_read(query), what=what)
    except urllib.error.HTTPError as e:
        LINEAR_BUDGET.remember(e.headers)
        if e.code == 429:
            reset = LINEAR_BUDGET.reset_at
            raise LinearBudgetExhausted(
                "Linear refused the query (429): the API key's complexity"
                " budget is spent" + (
                    f"; it resets at {_clock(reset)}"
                    if reset is not None else ""), reset_at=reset) from e
        raise
    LINEAR_BUDGET.remember(res.headers)
    r = json.load(res)
    if r.get("errors"):
        raise GraphQLError(r["errors"])
    return r["data"]


def _paginate(query, variables, path):
    # `query` declares `$after: String`; Linear caps a page at fifty issues.
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
        identifier id url title description updatedAt createdAt
        estimate priority
        labels { nodes { id name } }
        state { type name }
        relations { nodes { type relatedIssue { identifier state { type } } } }
      }
    }
  }
}"""

OPEN_QUERY = READY_QUERY.replace('"canceled", "backlog"]', '"canceled"]')

RELATIONS_QUERY = """
query($project: String!, $after: String) {
  project(id: $project) {
    issues(first: 50, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        identifier id state { type }
        relations { nodes { type relatedIssue { identifier } } }
      }
    }
  }
}"""

# A blocker in any other state still blocks: Backlog and Triage are open.
CLOSED_STATE_TYPES = {"completed", "canceled"}


def list_ready_issues(project_id, label=None):
    issues, blockers = _ready_with_blockers(project_id, label)
    return [i for i in issues if not blockers.get(i["identifier"])]


def _ready_with_blockers(project_id, label):
    issues = _paginate(READY_QUERY, {"project": project_id},
                       ("project", "issues"))
    blockers = _open_blockers(project_id)
    if label is not None:
        issues = [i for i in issues if label in label_names(i)]
    return issues, blockers


def _open_blockers(project_id):
    # Linear stores a blocks edge on its source, the blocker, so the whole
    # project's relations are read and inverted.
    blockers = {}
    for n in _paginate(RELATIONS_QUERY, {"project": project_id},
                       ("project", "issues")):
        if (n.get("state") or {}).get("type") in CLOSED_STATE_TYPES:
            continue
        for rel in n["relations"]["nodes"]:
            if rel["type"] == "blocks":
                blockers.setdefault(rel["relatedIssue"]["identifier"],
                                    []).append(n)
    return blockers


def parse_task(issue):
    desc = issue.get("description", "") or ""
    m = re.search(r"## Verify command\(s\)\s*```[^\n]*\n(.*?)```", desc, re.S)
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
            "board_state": (issue.get("state") or {}).get("name"),
            "labels": label_names(issue),
            "filed_at": filed_at(issue)}


ISSUE_QUERY = """
query($id: String!) {
  issue(id: $id) {
    identifier id url title description estimate priority createdAt updatedAt
    archivedAt state { name type } labels { nodes { name } }
    inverseRelations { nodes { type issue { id state { type } } } }
  }
}"""


def _column(state_type, archived, labels, label):
    if state_type == "completed":
        return None
    if state_type == "canceled" or archived:
        return "canceled"
    if state_type == "backlog" or (label is not None and label not in labels):
        return "backlog"
    return "ready"


def fetch_task(issue_id, label=None):
    try:
        issue = _gql(ISSUE_QUERY, {"id": issue_id})["issue"]
    except GraphQLError as e:
        if _issue_not_found(e):
            return None
        raise
    if not issue:
        return None
    state = issue.get("state") or {}
    inverse = (issue.get("inverseRelations") or {}).get("nodes") or ()
    return dict(_listed_task(issue), column=_column(
        state.get("type"), issue.get("archivedAt"), label_names(issue), label),
        blocked_by=[r["issue"]["id"] for r in inverse if r["type"] == "blocks"
                    and (r["issue"].get("state") or {}).get("type")
                    not in CLOSED_STATE_TYPES])


def _state_id(name, team):
    data = _gql('query($team: String!) { workflowStates(filter: { team: '
                '{ name: { eq: $team } } }) { nodes { id name type } } }',
                {"team": team})
    for s in data["workflowStates"]["nodes"]:
        if s["name"] == name:
            return s["id"]
    raise RuntimeError(f"team {team!r} has no workflow state named {name!r}")


def set_state(issue_id, state_name, team):
    data = _gql('mutation($id: String!, $state: String!) { issueUpdate(id: '
                '$id, input: { stateId: $state }) { success } }',
                {"id": issue_id, "state": _state_id(state_name, team)})
    # issueUpdate reports some refusals as success: false with no errors block.
    if not data["issueUpdate"]["success"]:
        raise RuntimeError(
            f"Linear refused to move issue {issue_id} to {state_name!r}")


def filed_at(issue):
    created = issue.get("createdAt")
    if not created:
        return None
    return int(datetime.fromisoformat(created.replace("Z", "+00:00"))
               .timestamp() * 1000)


def label_names(issue):
    return [n["name"] for n in ((issue.get("labels") or {}).get("nodes") or [])]


def _label_ids_of(issue_id):
    data = _gql('query($id: String!) { issue(id: $id) { labels { nodes { id '
                'name } } } }', {"id": issue_id})
    issue = data.get("issue")
    if not issue:
        raise RuntimeError(f"Linear has no issue {issue_id!r}")
    return {n["name"]: n["id"] for n in issue["labels"]["nodes"]}


def _label_id(name, team):
    # A team label, so one board's lease labels stay out of other teams' pickers.
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
    # addedLabelIds, not the whole labelIds list: a write from a moment-old read
    # must not drop a label another writer attached since.
    data = _gql('mutation($id: String!, $labels: [String!]!) { issueUpdate(id: '
                '$id, input: { addedLabelIds: $labels }) { success } }',
                {"id": issue_id, "labels": [label_id]})
    if not data["issueUpdate"]["success"]:
        raise RuntimeError(f"Linear refused to {what} on issue {issue_id}")


def _remove_label(issue_id, label_id, what):
    data = _gql('mutation($id: String!, $labels: [String!]!) { issueUpdate(id: '
                '$id, input: { removedLabelIds: $labels }) { success } }',
                {"id": issue_id, "labels": [label_id]})
    if not data["issueUpdate"]["success"]:
        raise RuntimeError(f"Linear refused to {what} on issue {issue_id}")


def label_issue(issue_id, name, team):
    # Linear has no compare-and-swap on labels: the caller reads back issue_labels().
    _add_label(issue_id, _label_id(name, team), f"add the label {name!r}")


def issue_labels(issue_id):
    return list(_label_ids_of(issue_id))


def unlabel_issue(issue_id, name):
    have = _label_ids_of(issue_id)
    if name not in have:
        return
    _remove_label(issue_id, have[name], f"remove the label {name!r}")


# Linear's priority 0 means none, which ranks after low (4), not before urgent.
PRIORITY_RANK = {1: 0, 2: 1, 3: 2, 4: 3}
UNPRIORITISED_RANK = 4


def _claim_key(order):
    if order == "priority":
        return lambda i: (PRIORITY_RANK.get(i.get("priority"), UNPRIORITISED_RANK),
                          i["identifier"])
    return lambda i: i["identifier"]


def ready_issues(project_id, label=None):
    return [_listed_task(issue)
            for issue in list_ready_issues(project_id, label=label)]


def listing(project_id, label=None):
    issues, blockers = _ready_with_blockers(project_id, label)
    return [dict(_listed_task(issue),
                 blocked_by=[b["id"] for b in blockers.get(issue["identifier"], ())])
            for issue in issues]


def open_issues(project_id, label=None):
    issues = _paginate(OPEN_QUERY, {"project": project_id},
                       ("project", "issues"))
    blockers = _open_blockers(project_id)
    return [dict(_listed_task(issue),
                 # Linear leaves archived issues out of a project's listing.
                 column=_column((issue.get("state") or {}).get("type"), None,
                                label_names(issue), label),
                 blocked_by=[b["id"] for b in blockers.get(issue["identifier"], ())])
            for issue in issues]


def _listed_task(issue):
    return dict(parse_task(issue), updatedAt=(
        int(datetime.fromisoformat(issue["updatedAt"].replace("Z", "+00:00"))
            .timestamp() * 1000) if issue.get("updatedAt") else None))


def claim_next(project_id, team, skip=(), order="identifier", label=None):
    issues = list_ready_issues(project_id, label=label)
    ready = [i for i in issues if i["identifier"] not in skip]
    if not ready:
        return None, [i["identifier"] for i in issues]
    issue = min(ready, key=_claim_key(order))
    task = parse_task(issue)
    print(f"[holo2] claimed {task['id']}: {task['title']} "
          f"(budget {task['budget_min']} min)")
    return task, [i["identifier"] for i in issues]


def comment(task_id, body):
    data = _gql('mutation($issue: String!, $body: String!) { commentCreate('
                'input: { issueId: $issue, body: $body }) { success } }',
                {"issue": task_id, "body": body})
    if not (data.get("commentCreate") or {}).get("success"):
        raise RuntimeError(f"Linear refused the comment on {task_id}")


# IssueFilter has no identifier field, so the team key and number spell it.
# Linear archives a Done issue on its own and omits archived ones by default.
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
    nodes {
      identifier archivedAt state { type name }
      labels { nodes { name } }
    }
  }
}"""

IDENTIFIER_RE = re.compile(r"\A([A-Za-z0-9]+)-(\d+)\Z")


def closed_identifiers(identifiers):
    closed = {}
    for node in _issues_named(identifiers):
        state_type = (node.get("state") or {}).get("type")
        if state_type in CLOSED_STATE_TYPES:
            closed[node["identifier"]] = state_type
        elif node.get("archivedAt"):
            # An archived issue is one nobody will work.
            closed[node["identifier"]] = "canceled"
    return closed


def _issues_named(identifiers):
    by_key = {}
    for identifier in identifiers:
        m = IDENTIFIER_RE.match(identifier)
        if m:
            by_key.setdefault(m.group(1), []).append(int(m.group(2)))
    asked = set(identifiers)
    return [node for key, numbers in by_key.items()
            for node in _paginate(CLOSED_QUERY, {"key": key, "numbers": numbers},
                                  ("issues",))
            if node["identifier"] in asked]


def states(identifiers, label=None):
    # Imported here: this module imports standalone, without the seam.
    from provider import GONE
    answer ={i: {"state": GONE, "name": None, "column": None}
              for i in identifiers if IDENTIFIER_RE.match(i)}
    for node in _issues_named(identifiers):
        state = node.get("state") or {}
        column = _column(state.get("type"), node.get("archivedAt"),
                         label_names(node), label)
        answer[node["identifier"]] = {
            "state": {None: "completed", "canceled": "canceled"}.get(
                column, "open"),
            "name": state.get("name"), "column": column}
    return answer


def _team_id(team):
    data = _gql('query($team: String!) { teams(filter: { name: { eq: $team } '
                '}) { nodes { id } } }', {"team": team})
    nodes = data["teams"]["nodes"]
    if not nodes:
        raise RuntimeError(f"Linear has no team named {team!r}")
    return nodes[0]["id"]


def _issue_id(identifier):
    data = _gql('query($id: String!) { issue(id: $id) { id } }',
                {"id": identifier})
    if not data.get("issue"):
        raise RuntimeError(f"Linear has no issue {identifier!r}")
    return data["issue"]["id"]


def create_issue(project_id, team, title, body, estimate, state_name,
                 priority=None):
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
    data = _gql('query($id: String!) { issue(id: $id) { description } }',
                {"id": identifier})
    if not data.get("issue"):
        raise RuntimeError(f"Linear has no issue {identifier!r}")
    return data["issue"]["description"] or ""

