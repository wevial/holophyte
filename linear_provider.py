#!/usr/bin/env python3
"""Minimal Linear MCP-over-HTTP client for holo2 (read/write issues).

Uses the Hermes-stored OAuth token (~/.hermes/mcp-tokens/linear.json) against
https://mcp.linear.app/mcp. Handles the streamable-HTTP JSON-RPC dance:
initialize -> initialized -> tools/call. Re-initializes per invocation; fine
for a single-threaded loop calling a few times per task.
"""
import json
import uuid
import urllib.request
from pathlib import Path

MCP_URL = "https://mcp.linear.app/mcp"
TOKEN_FILE = Path.home() / ".hermes/mcp-tokens/linear.json"
PROJECT_ID = "REDACTED"  # Lotuspod


def _token():
    return json.loads(TOKEN_FILE.read_text())["access_token"]


def _post(payload, session_id=None):
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    req = urllib.request.Request(MCP_URL, data=json.dumps(payload).encode(),
                                 headers=headers)
    r = urllib.request.urlopen(req, timeout=60)
    sid = r.headers.get("mcp-session-id", session_id)
    body = r.read().decode()
    # SSE-style response: take the data: line(s)
    results = []
    for line in body.splitlines():
        if line.startswith("data: "):
            try:
                results.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    if not results and body.strip():
        try:
            results.append(json.loads(body))
        except json.JSONDecodeError:
            pass
    return results, sid


class Linear:
    def __init__(self):
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "holo2", "version": "0.1"}}}
        _, self.sid = _post(init)
        _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, self.sid)
        self._n = 1

    def call(self, tool, args):
        return self._call_no_team(tool, {"team": "Personal Projects", **args})

    def _call_no_team(self, tool, args):
        self._n += 1
        payload = {"jsonrpc": "2.0", "id": self._n, "method": "tools/call",
                   "params": {"name": tool, "arguments": args}}
        results, _ = _post(payload, self.sid)
        for r in results:
            if "error" in r:
                raise RuntimeError(r["error"])
            content = r.get("result", {}).get("content", [])
            text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
            try:
                return json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return text
        raise RuntimeError("no result from MCP call")


# --- Loop-facing provider API -------------------------------------------------

def list_ready_issues(project_id=PROJECT_ID):
    """Issues in the project that are triaged, not terminal, unblocked.

    Note: status comes back as a plain string; dependency info lives under
    get_issue(includeRelations=True) -> relations.blockedBy, whose entries
    carry their own statusType.
    """
    lin = Linear()
    raw = lin.call("list_issues", {"project": project_id, "limit": 50})
    issues = raw.get("issues", []) if isinstance(raw, dict) else raw.get("issues", [])
    ready = []
    for i in issues:
        if i.get("statusType", "") in ("completed", "canceled", "backlog"):
            continue  # done, killed, or untriaged backlog (Todo = pickable)
        full = lin._call_no_team("get_issue",
                                 {"id": i["id"], "includeRelations": True})
        blocked = [b for b in (full.get("relations", {}).get("blockedBy") or [])
                   if b.get("statusType") not in ("completed", "canceled")]
        if not blocked:
            ready.append(i)
    return ready


def parse_task(issue):
    """Extract task text + verify command from a ticket's description."""
    import re
    desc = issue.get("description", "") or ""
    m = re.search(r"\*\*Verify command\(s\):\*\*\s*```\n(.*?)```", desc, re.S)
    verify = m.group(1).strip() if m else None
    title = issue.get("title", "").strip()
    return {"id": issue["identifier"], "title": title, "verify": verify,
            "budget_min": int(issue.get("estimate", {}).get("value") or 20)}


def claim_next():
    """Pick the first ready issue -> In Progress. Returns parsed task or None."""
    ready = list_ready_issues()
    if not ready:
        return None
    issue = sorted(ready, key=lambda i: i["identifier"])[0]
    lin = Linear()
    lin.call("save_issue", {"id": issue["identifier"], "state": "In Progress"})
    task = parse_task(issue)
    print(f"[holo2] claimed {task['id']}: {task['title']} "
          f"(budget {task['budget_min']} min)")
    return task


def complete(task_id):
    lin = Linear()
    lin.call("save_issue", {"id": task_id, "state": "Done"})


def comment(task_id, body):
    lin = Linear()
    lin.call("save_comment", {"issue": task_id, "body": body})


if __name__ == "__main__":
    t = claim_next()
    print(t)
