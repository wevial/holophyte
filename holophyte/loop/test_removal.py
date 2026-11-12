"""Test files outside the ticket's In scope list that lost tests since the base."""
import subprocess

from holophyte.loop.gates import VerificationOutput
from holophyte.tickets import ticket_template

REPORT = ("tests removed outside the ticket's scope: "
          "{path} ({before} before, {after} after)")


def removed_outside_scope(wt, base_sha, sha, ticket):
    # Deferred: the trim module imports the review rounds, which import this.
    from holophyte.loop.trim import _is_test, _test_ids

    scope = {path for item in ticket_template.parse(ticket).in_scope
             for path in ticket_template.path_candidates(item)}
    lines = []
    for old, new in _changed_pairs(wt, base_sha, sha):
        if not _is_test(old) or old in scope:
            continue
        before = sum(_test_ids(wt, base_sha, old).values())
        after = sum(_test_ids(wt, sha, new).values()) if new else 0
        if after < before:
            lines.append(REPORT.format(path=old, before=before, after=after))
    return lines


def with_removals(wt, base_sha, sha, ticket, ok, out):
    lines = removed_outside_scope(wt, base_sha, sha, ticket)
    if not lines:
        return ok, out
    report = "\n".join(lines)
    failure = out.failure or {
        "command_index": None, "command": "test removal check",
        "exit_status": None, "last_output_line": lines[-1]}
    return False, VerificationOutput(
        f"{out}\n{report}", out.results, failure=failure)


def _changed_pairs(wt, base_sha, sha):
    fields = subprocess.run(
        ["git", "diff", "--name-status", "-M", "-z", base_sha, sha],
        cwd=wt, capture_output=True, check=True,
    ).stdout.decode(errors="replace").split("\0")
    pairs, index = [], 0
    while index < len(fields) - 1:
        status = fields[index]
        if status.startswith("R"):
            pairs.append((fields[index + 1], fields[index + 2]))
            index += 3
        else:
            path = fields[index + 1]
            pairs.append((path, None if status == "D" else path))
            index += 2
    return pairs
