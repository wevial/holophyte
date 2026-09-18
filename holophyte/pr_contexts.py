"""Commit statuses from the head rollup, normalised beside check runs."""
from holophyte.gates import InfraFailure

CONTEXTS_FIELDS = """
contexts(first: 100, after: $contextsAfter) {
  pageInfo { hasNextPage endCursor }
  nodes { __typename ... on StatusContext { context state } }
}
"""
STATUS_CONTEXTS_QUERY = """
query($owner: String!, $name: String!, $sha: String!, $contextsAfter: String) {
  repository(owner: $owner, name: $name) {
    object(expression: $sha) { ... on Commit {
      statusCheckRollup { %s }
    } }
  }
}
""" % CONTEXTS_FIELDS


def status_contexts_of(target, pull, node, graphql):
    """Normalise the rollup's commit statuses beside the REST check runs."""
    commits = (node.get("commits") or {}).get("nodes") or []
    rollup = ((commits[-1].get("commit") or {}).get("statusCheckRollup")
              if commits else None) or {}
    runs = []
    while True:
        page = rollup.get("contexts") or {}
        for context in page.get("nodes") or []:
            if context.get("__typename") != "StatusContext":
                continue
            conclusion = (context.get("state") or "pending").lower()
            runs.append({"name": context.get("context"),
                         "status": "pending" if conclusion in
                         {"pending", "expected"} else "completed",
                         "conclusion": conclusion})
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return runs
        if not info.get("endCursor"):
            raise InfraFailure("GitHub omitted the status-context page cursor")
        data = graphql(target, pull, STATUS_CONTEXTS_QUERY,
                       {"owner": pull.owner, "name": pull.name,
                        "sha": node["headRefOid"],
                        "contextsAfter": info["endCursor"]})
        commit = (data.get("repository") or {}).get("object") or {}
        rollup = commit.get("statusCheckRollup") or {}
        if not isinstance(rollup.get("contexts"), dict):
            raise InfraFailure("GitHub omitted the status-context page")
