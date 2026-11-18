from holophyte.revert.checks import dependent_lines
from holophyte.tickets.ticket_template import MAX_IN_SCOPE

ESTIMATE_MIN = 30


def scope_entries(files):
    quoted = [f"`{path}`" for path in files]
    if len(quoted) <= MAX_IN_SCOPE:
        return [f"- {path}" for path in quoted]
    return [f"- {', '.join(quoted)}"]


def revert_ticket(merge, reason, found):
    reason = " ".join(reason.split())
    dependents = "; ".join(dependent_lines(found))
    files = ", ".join(f"`{path}`" for path in merge.files)
    verify = "\n".join(merge.verify)
    return "\n".join([
        "+++",
        'ui_change = "none"',
        f"estimate = {ESTIMATE_MIN}",
        "depends_on = []",
        "+++",
        f"# [Revert] Undo {merge.key}: {merge.title}",
        "",
        "## Summary",
        "",
        f"Undo {merge.key}'s merge: {reason}",
        "",
        f"Merge sha: `{merge.sha}`.",
        "",
        f"Files: {files}.",
        "",
        f"Dependents: {dependents}.",
        "",
        f"Reverts: {merge.key} at {merge.sha}",
        "",
        "## What / Why / How",
        "",
        f"**What:** Run `git revert --no-edit {merge.sha}` in the task worktree and"
        " commit nothing else. If it conflicts, stop without resolving it"
        " and say so as an unmet criterion.",
        "",
        f"**Why:** {reason}",
        "",
        f"**How:** `git revert` alone; the verify commands are {merge.key}'s.",
        "",
        "## In scope",
        "",
        *scope_entries(merge.files),
        "",
        "## Out of scope",
        "",
        "- Any change beyond the revert commit, a conflict's resolution"
        " included.",
        "",
        "## Acceptance criteria",
        "",
        "- [ ] The candidate is a single commit whose message carries"
        f" `This reverts commit {merge.sha}` and whose diff touches only"
        " the files listed under In scope.",
        "",
        "## Verify command(s)",
        "",
        "```",
        verify,
        "```",
        "",
        "## Implementation notes",
        "",
        "- A dependent above changed a file this revert restores, so the"
        " revert may conflict with it." if found else
        "- No later merge on main changed these files.",
        "",
        "## Open questions",
        "",
        "- None",
        "",
    ])
