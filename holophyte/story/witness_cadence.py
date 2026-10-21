"""Whether a loop pass runs a story's witness set, and the verdicts it carries."""
import datetime
import subprocess

from holophyte.config.config_tables import story_config
from holophyte.story.story_close import GIT_TIMEOUT, main_ledger
from store.stories import record_witness_result, story


class _GitFailed(Exception):
    pass


def latest_real(conn, story_id):
    return {row.witnessKey: row for row in main_ledger(conn, story_id)
            if row.carriedFrom is None}


def set_due(target, conn, story_id, sha):
    found = story(conn, story_id)
    latest = latest_real(conn, story_id)
    current = [latest.get(witness.key) for witness in found.witnesses]
    if (not current or None in current
            or any(row.at <= found.approvedAt for row in current)):
        return True
    last = min(current, key=lambda row: row.id)
    if last.at < _last_nightly_ms(story_config(target).witness_nightly_hour):
        return True
    if last.mainSha == sha:
        return False
    try:
        return (_paths_changed(target.path, last.mainSha, sha,
                               found.witnessPaths)
                or _child_merged(target.path, conn, story_id, last.mainSha,
                                 sha))
    except _GitFailed:
        return True


def _last_nightly_ms(hour):
    clock = datetime.datetime.now()
    nightly = clock.replace(hour=hour, minute=0, second=0, microsecond=0)
    if nightly > clock:
        nightly -= datetime.timedelta(days=1)
    return nightly.timestamp() * 1000


def _paths_changed(repo, since, sha, paths):
    if not paths:
        return False
    code, output = _git(repo, "--literal-pathspecs", "diff", "--name-only",
                        since, sha, "--", *paths)
    if code:
        raise _GitFailed
    return bool(output.strip())


def _child_merged(repo, conn, story_id, since, sha):
    merges = [merge for (merge,) in conn.execute(
        "SELECT DISTINCT mergeSha FROM runs WHERE outcome = 'merged'"
        " AND mergeSha IS NOT NULL AND ticketId IN (SELECT ticketId"
        " FROM storyChildren WHERE storyId = ?) ORDER BY mergeSha",
        (story_id,))]
    return any(_ancestor(repo, merge, sha) and not _ancestor(repo, merge, since)
               for merge in merges)


def _ancestor(repo, commit, of):
    code, _ = _git(repo, "merge-base", "--is-ancestor", commit, of)
    if code not in (0, 1):
        raise _GitFailed
    return code == 0


def _git(repo, *args):
    try:
        done = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                              text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        raise _GitFailed from None
    return done.returncode, done.stdout


def carry(conn, story_id, sha, verifier):
    latest = latest_real(conn, story_id)
    at_sha = {row.witnessKey for row in main_ledger(conn, story_id, sha)}
    ids = []
    for witness in story(conn, story_id).witnesses:
        if witness.key in at_sha:
            continue
        row = latest[witness.key]
        ids.append(record_witness_result(
            conn, story_id, witness.key, sha, row.verdict, verifier,
            red_kind=row.redKind, file_hash=row.fileHash,
            evidence_path=row.evidencePath, seconds=0,
            carried_from=row.mainSha))
    return ids
