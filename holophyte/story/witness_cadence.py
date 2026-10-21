"""Which of a story's witnesses a loop pass runs, and the verdicts it carries."""
import subprocess
import time

from holophyte.story.story_close import main_ledger
from store.stories import record_witness_result

HOUR_MS = 3600 * 1000
GIT_TIMEOUT = 30


def latest_real(conn, story_id):
    return {row.witnessKey: row for row in main_ledger(conn, story_id)
            if row.carriedFrom is None}


def due_keys(repo, conn, story_id, witnesses, sha, now=None):
    if now is None:
        now = int(time.time() * 1000)
    latest = latest_real(conn, story_id)
    return {witness.key for witness in witnesses
            if _due(repo, witness, latest.get(witness.key), sha, now)}


def _due(repo, witness, last, sha, now):
    if witness.cadence is None or last is None:
        return True
    hours = witness.cadence.get("hours")
    if hours is not None and now - last.at < hours * HOUR_MS:
        return False
    paths = witness.cadence.get("paths")
    return not paths or _changed(repo, last.mainSha, sha,
                                 [*paths, witness.file])


def _changed(repo, since, sha, paths):
    try:
        diff = subprocess.run(
            ["git", "diff", "--name-only", since, sha, "--", *paths],
            cwd=repo, capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return True
    return diff.returncode != 0 or bool(diff.stdout.strip())


def carry(conn, story_id, sha, keys, verifier):
    latest = latest_real(conn, story_id)
    at_sha = {row.witnessKey for row in main_ledger(conn, story_id, sha)}
    ids = []
    for key in sorted(keys - at_sha):
        row = latest[key]
        ids.append(record_witness_result(
            conn, story_id, key, sha, row.verdict, verifier,
            red_kind=row.redKind, file_hash=row.fileHash,
            evidence_path=row.evidencePath, seconds=0,
            carried_from=row.mainSha))
    return ids
