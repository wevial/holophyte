"""A story child's feature follow-up put to the adjudicator: in the story or not."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

import store
from holophyte.agents.agent_routes import routes, safe_command
from holophyte.agents.fallback import record_pending_switch
from holophyte.agents.review_workspace import cleanup_review_refs
from holophyte.agents.roles import agent
from holophyte.loop.gates import sh
from holophyte.pr import github
from story_template import parse_story

IN_STORY = "in_story"
STANDALONE = "standalone"
SCOPE_TIMEOUT = 600
CAUSE_CHARS = 300
_ANSWER = re.compile(r"SCOPE: (in_story|standalone): (\S.*)")


@dataclass(frozen=True)
class Scope:
    verdict: str
    reason: str
    default: bool
    route: str | None = None


def _default(cause):
    return Scope(STANDALONE, " ".join(cause.split())[:CAUSE_CHARS], True)


def scope_goal(conn, story_id, raised_by, text, where):
    title, body = conn.execute("SELECT title, COALESCE(body, '') FROM tickets"
                               " WHERE id = ?", (story_id,)).fetchone()
    sections = parse_story(body).sections
    children = [f"- {key} \"{child}\" ({status})" for key, child, status in
                conn.execute(
                    "SELECT linearIdentifier, title, CASE WHEN EXISTS"
                    " (SELECT 1 FROM runs WHERE ticketId = tickets.id"
                    " AND outcome = 'merged') THEN 'merged' ELSE status END"
                    " FROM tickets WHERE id IN (SELECT ticketId"
                    " FROM storyChildren WHERE storyId = ?) ORDER BY id",
                    (story_id,))]
    return "\n".join([
        f"A merged run of {raised_by}, a child of the story below, raised a"
        " follow-up. Decide whether the follow-up belongs in the story, as a"
        " new child serving its goal, or stands alone outside its scope.",
        "", f"Story: {title}", "",
        "Summary:", sections.get("Summary", "").strip() or "(none)", "",
        "Goal:", sections.get("Goal", "").strip() or "(none)", "",
        "Children:", *children, "",
        f"Raised by: {raised_by}",
        f"Follow-up: {text}",
        f"Found at: {where or 'not given'}", "",
        "Answer in_story only when the follow-up is needed for the story's"
        " goal; answer standalone when it falls outside the story or when"
        " you are unsure. End the reply with one last line that is exactly",
        "SCOPE: in_story: REASON",
        "or",
        "SCOPE: standalone: REASON",
        "where REASON is one line."])


def read_scope(output):
    if getattr(output, "timed_out", False):
        return _default("the adjudicator timed out")
    code = getattr(output, "exit_code", 0)
    if code not in (0, None):
        return _default(f"the adjudicator exited {code}")
    lines = [line.strip() for line in str(output).splitlines() if line.strip()]
    answer = _ANSWER.fullmatch(lines[-1]) if lines else None
    if answer is not None:
        return Scope(answer[1], answer[2].strip(), False)
    if not any(line.startswith("SCOPE:") for line in lines):
        return _default("the reply has no SCOPE: line")
    return _default(f"the reply's last line is not a scope verdict:"
                    f" {lines[-1]}")


def _first_parent(repo, merge_sha):
    """A pull request's merge commit is made remotely; fetch it once."""
    parent = ["git", "rev-parse", "--verify", f"{merge_sha}^1^{{commit}}"]
    try:
        return sh(parent, repo)
    except RuntimeError:
        sh(["git", "fetch", "--quiet", github.REMOTE, github.BASE], repo)
        return sh(parent, repo)


def _turn(target, conn, run_id, goal, merge_sha):
    if not merge_sha:
        return _default("the run recorded no merge sha")
    state = routes(target)
    if state.project_id is None:
        (state.project_id,) = conn.execute(
            "SELECT projectId FROM runs WHERE id = ?", (run_id,)).fetchone()
    try:
        base = _first_parent(target.path, merge_sha)
        output = agent(target, "adjudicate", goal, target.path, base_sha=base,
                       candidate_sha=merge_sha, timeout=SCOPE_TIMEOUT,
                       conn=conn)
    except Exception as e:
        return _default(f"the scope turn failed: {type(e).__name__}: {e}")
    finally:
        cleanup_review_refs(target.path, None)
        record_pending_switch(target, "adjudicate", conn, run_id)
    scope = read_scope(output)
    return Scope(scope.verdict, scope.reason, scope.default,
                 safe_command(target, getattr(output, "command", None)))


def judge(target, conn, run_id, follow_up_id, goal, merge_sha):
    """The seat's verdict, `standalone` by default, recorded as one event."""
    scope = _turn(target, conn, run_id, goal, merge_sha)
    default = " by default" if scope.default else ""
    store.record_event(
        conn, run_id, "follow_up_scope",
        f"follow-up {follow_up_id}: {scope.verdict}{default}: {scope.reason}",
        level="detail", payload=json.dumps(
            {"id": follow_up_id, "verdict": scope.verdict,
             "reason": scope.reason, "route": scope.route,
             "default": scope.default}))
    return scope
