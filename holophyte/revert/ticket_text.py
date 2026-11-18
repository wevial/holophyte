from holophyte.revert.checks import dependent_lines, title_prefix
from holophyte.tickets.ticket_template import MAX_IN_SCOPE

ESTIMATE_MIN = 30


def revert_ticket(merge, reason, found):
    reason = " ".join(reason.split()).replace("<", "&lt;").replace(
        "{{", "{&#123;")
    mainline = " -m 1" if merge.parents > 1 else ""
    quoted = [f"`{path}`" for path in merge.files]
    files = ", ".join(quoted)
    scope = ([f"- {path}" for path in quoted] if len(quoted) <= MAX_IN_SCOPE
             else [f"- {files}"])
    return "\n".join([
        "+++",
        'ui_change = "none"',
        f"estimate = {ESTIMATE_MIN}",
        "depends_on = []",
        "+++",
        f"# {title_prefix(merge.key)}{merge.title}",
        "",
        "## Summary",
        "",
        f"Undo {merge.key}'s merge: {reason}",
        "",
        f"Merge sha: `{merge.sha}`.",
        "",
        f"Files: {files}.",
        "",
        f"Dependents: {'; '.join(dependent_lines(found))}.",
        "",
        f"Reverts: {merge.key} at {merge.sha}",
        "",
        "## What / Why / How",
        "",
        f"**What:** Run `git revert --no-edit{mainline} {merge.sha}` in the"
        " task worktree and"
        " commit nothing else. If it conflicts, stop without resolving it"
        " and say so as an unmet criterion.",
        "",
        f"**Why:** {reason}",
        "",
        f"**How:** `git revert` alone; the verify commands are {merge.key}'s.",
        "",
        "## In scope",
        "",
        *scope,
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
        "\n".join(merge.verify),
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
