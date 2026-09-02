"""FINDINGS.md as a bounded window over the store's rows.

The stamp and entry helpers, one entry per review round and per ended run,
the ordering of both kinds into one list, the render of the newest
`FINDINGS_WINDOW` entries under the frozen pre-store preamble, and the three
steps a close-out takes with the result: write the file, commit it if the
bytes changed, refresh it when there is a store to render from. A pure
projection: identical rows render to identical bytes, with no clock and no
environment read. Beyond the standard library it imports `store.read` for the
rows, `sh` from `holophyte.gates` for the commit, and `BLOCK_BREAK_RE` from
`holophyte.review` to drop a reviewer's own bullet marker from a finding line.

Fourth slice of the phase-2 module split; moved verbatim from `factory.py`,
which imports back the names its remaining call sites use.
"""
import json
import subprocess
from pathlib import Path

import store.read
from holophyte.gates import sh
from holophyte.review import BLOCK_BREAK_RE

# --- FINDINGS.md as a window over the rows ------------------------------------
# The ledger used to be the source of truth, appended to once per agent turn,
# and it reached 53 KB in a single day: unreadable for a human and a context
# hazard for an agent working in the checkout. The rows are the history now --
# complete, queryable, cheap -- and the file is a rendering of their recent
# tail, so a reader gets the last few runs and everything older is one query
# away rather than deleted.
#
# The rendering is a function of the rows and of nothing else: no clock, no
# environment, ties in the ordering broken by row id, and the sanitizing
# already done at the row write. That is what makes regenerating the whole file
# at every close-out reviewable -- identical rows produce identical bytes, so
# the diff of a regeneration is exactly the entries the run added.
FINDINGS_WINDOW = 25  # entries kept in the file; per-project config is future work
# What separates the frozen pre-store history from the rendered window. Every
# regeneration reproduces the bytes above this line and replaces everything
# below it, so a ledger written before the store existed is preserved by not
# being rendered at all.
FINDINGS_MARKER = "<!-- store-rendered below -->"
FINDINGS_ARCHIVE = "[{n} earlier entries in holophyte.db — query runs/reviewRounds]"
# One finding is one line of the window: enough to recognize the complaint by,
# with the row holding all of it.
FINDING_LINE_CHARS = 160


STAMP_UNREADABLE = "(unreadable timestamp)"


def _ms(value):
    """`value` as epoch milliseconds, or None when the column does not hold one.

    The timestamp columns are declared INTEGER but SQLite affinity does not
    enforce it, so a row can carry text, NULL, or a float no calendar reaches.
    Anything that is not a finite number the renderer treats as unreadable.
    """
    import math
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return value


def _stamp(ms):
    """Epoch milliseconds as the ledger's UTC timestamp.

    A stamp the row cannot supply renders as a visible placeholder rather than
    raising: one bad row must not take the whole window with it.
    """
    from datetime import datetime, timezone
    ms = _ms(ms)
    if ms is None:
        return STAMP_UNREADABLE
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return STAMP_UNREADABLE


def _entry(at, ticket, lines):
    """One entry: the heading the file has always used, then its body."""
    return "\n".join([f"## {_stamp(at)} — {ticket}", *lines])


def _gist(text):
    """`text` collapsed to one line and cut to the window's width."""
    gist = " ".join(str(text).split())
    if len(gist) > FINDING_LINE_CHARS:
        gist = gist[:FINDING_LINE_CHARS].rstrip() + "…"
    return gist


def _document(text):
    """A stored JSON document column decoded to its list, or None.

    None for anything the schema's `[]` comment does not describe: text that
    is not JSON, JSON nested past what the decoder's stack can walk, or JSON
    that is not an array. The writer refuses all three, so a row like this was
    written past it -- by hand, by an earlier release, or by corruption -- and
    the renderer's job is to show that, not to crash the close-out that
    regenerates every other entry in the window.

    The caught set is wider than `json.JSONDecodeError` on purpose. A column
    SQLite hands back as a BLOB decodes through `UnicodeDecodeError`, which is
    a `ValueError` but not a `JSONDecodeError`, so catching the base class is
    what makes "never raises" hold for a bytes column rather than only for bad
    syntax; `TypeError` covers a column that is not text at all, and
    `RecursionError` — not a `ValueError` — the pathologically nested document.
    """
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return None
    return decoded if isinstance(decoded, list) else None


def finding_line(finding):
    """One stored finding as one line: where, how bad, and the gist.

    The message is collapsed to a single line and cut, because the window is a
    place to notice a complaint rather than to read it: the whole message is in
    `reviewRounds.findings`, and the alternative -- full prose per finding --
    is the unbounded file this rendering replaces.

    Never raises. A finding that is not a mapping with a `path` and a
    `severity` is rendered as a marked line carrying its compact repr, so the
    row's shape is visible in the window rather than fatal to it.
    """
    if (not isinstance(finding, dict) or "path" not in finding
            or "severity" not in finding):
        return f"- (malformed finding) {_gist(repr(finding))}"
    where = str(finding["path"])
    if finding.get("line"):
        where = f"{where}:{finding['line']}"
    # The reviewer's own bullet marker opens most stored messages; the line
    # this renders already is a bullet, so it is dropped rather than nested.
    gist = BLOCK_BREAK_RE.sub("", _gist(finding.get("message", "")), count=1)
    return f"- {where} [{finding['severity']}] {gist}".rstrip()


def round_at(row):
    """When a `store.read.ReviewRound` is dated: its end, or its start if open.

    `COALESCE(endedAt, startedAt)` as the entry's stamp -- a round still being
    reviewed is placed by when it began rather than left undated.
    """
    return row.endedAt if row.endedAt is not None else row.startedAt


def round_entry(row):
    """One `store.read.ReviewRound` as an entry: the verdict and what it filed.

    Never raises on the row's two JSON columns. A `verificationResults` or
    `findings` document that does not decode to the schema's array, or an
    array holding a result that is not a mapping, renders as an `unreadable`
    verify note or an `unparseable` findings line quoting the raw column,
    rather than as a verdict the row does not actually carry: the writer
    refuses such rows, so one that exists is evidence to show, and a render
    that died on it would leave the file stale for every good row after it.
    """
    at, ticket, number = round_at(row), row.linearIdentifier, row.round
    verdict, model = row.verdict, row.reviewerModel
    results, findings = row.verificationResults, row.findings
    results = _document(results)
    verify = ""
    if results is None or not all(isinstance(r, dict) for r in results):
        verify = " · verify unreadable"
    elif results:
        verify = (" · verify "
                  + ("passed" if all(r.get("exitCode") == 0 for r in results)
                     else "failed"))
    raw_findings, findings = findings, _document(findings)
    lines = [f"Round {number}: {verdict} · reviewer {model}{verify}"]
    if findings is None:
        lines.append(f"Findings: unparseable — {_gist(raw_findings)}")
    elif findings:
        lines.append(f"Findings ({len(findings)}):")
        lines.extend(finding_line(finding) for finding in findings)
    return _entry(at, ticket, lines)


def run_entry(row):
    """One `store.read.EndedRun` as an entry: its outcome and the timing line.

    Every number on the timing line is read off the run row itself -- the two
    stamps, the estimate it was claimed under, and the round count stamped at
    close-out (terminal adjudication included) -- rather than off the loop's
    in-frame counters or the ticket's current estimate. That is what makes the
    line and the row say the same thing: `--report` queries the same columns,
    and a rendering that needs the loop's variables is not a rendering of the
    rows.
    """
    at, ticket, outcome = row.endedAt, row.linearIdentifier, row.outcome
    reason, branch, started = row.outcomeReason, row.branch, row.startedAt
    time_box, rounds = row.timeBoxMs, row.reviewRoundCount
    if outcome == "merged":
        head = ("MERGED to main"
                + (f" (branch {branch} deleted)" if branch else "") + ".")
    else:
        head = (outcome or "ended").upper()
        head += f": {' '.join(reason.split())}" if reason else "."
    # Byte-stable: a burndown script greps this line.
    estimate = f"{time_box // 60000} min" if _ms(time_box) else "n/a"
    ended, started = _ms(at), _ms(started)
    actual = ("n/a" if ended is None or started is None
              else f"{(ended - started) / 60000:.1f} min")
    return _entry(at, ticket, [
        head,
        f"actual: {actual} · estimate: {estimate} · rounds: {rounds}",
    ])


def findings_entries(conn):
    """Every entry the store holds, oldest first.

    Two row kinds are entries: a review round, and a run that ended. A run
    still in flight has no entry -- it has no outcome yet, and an entry that
    changed on the next render would make the file churn.
    """
    rounds = store.read.review_rounds(conn)
    runs = store.read.ended_runs(conn)
    entries = [(round_at(row), "round", row.id, round_entry(row))
               for row in rounds]
    entries += [(row.endedAt, "run", row.id, run_entry(row)) for row in runs]
    # Two rows stamped the same millisecond still have one order: kind, then
    # the id the database gave them. A row with no readable stamp cannot be
    # placed in time, so it sorts ahead of every dated one -- deterministic,
    # and never a comparison between a number and whatever the column held.
    def _order(entry):
        at = _ms(entry[0])
        return (at is not None, at or 0, entry[1], entry[2])
    entries.sort(key=_order)
    return [entry[3] for entry in entries]


def render_findings(conn, preamble=""):
    """The whole of FINDINGS.md: `preamble`, the marker, then the window.

    Only the newest `FINDINGS_WINDOW` entries are rendered; the rest collapse
    into the one archive line that says how many there are and where to read
    them. Newest last, so the file reads forwards like the append-only ledger
    it replaces.
    """
    entries = findings_entries(conn)
    hidden = max(0, len(entries) - FINDINGS_WINDOW)
    blocks = [FINDINGS_ARCHIVE.format(n=hidden)] if hidden else []
    blocks += entries[hidden:]
    # The preamble is reproduced byte for byte and only padded away from the
    # marker, which keeps this idempotent: the padded text is what the next
    # render reads back as the preamble.
    if preamble and not preamble.endswith("\n\n"):
        preamble += "\n" if preamble.endswith("\n") else "\n\n"
    body = "\n\n".join(blocks)
    return preamble + FINDINGS_MARKER + "\n" + (f"\n{body}\n" if body else "")


def frozen_preamble(text):
    """The half of `text` a regeneration must reproduce: above the marker.

    A file with no marker is entirely pre-store history, so all of it is
    preamble and the window is written below it. The marker counts only as a
    line of its own, which is what keeps a reviewer who quoted it from moving
    the boundary: no line this module renders can be a bare marker -- every
    one of them is a heading, a `Round`/outcome line, a `- ` finding or the
    archive line -- so the first standalone marker line is always the real one.
    """
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip() == FINDINGS_MARKER:
            return "".join(lines[:i])
    return text


def write_findings(target, conn, path=None):
    """Regenerate FINDINGS.md in place, keeping everything above the marker."""
    path = Path(path) if path else target.path / "FINDINGS.md"
    existing = path.read_text() if path.exists() else ""
    path.write_text(render_findings(conn, frozen_preamble(existing)))
    return path


def commit_findings(target, message):
    """Commit FINDINGS.md in the target checkout, if the render changed it.

    Returns whether it committed. The guard is not an optimization: a
    regeneration that produced the same bytes has nothing to record, and
    `git commit` on an unchanged tree fails.
    """
    r = subprocess.run(["git", "status", "--porcelain", "FINDINGS.md"],
                       cwd=target.path, capture_output=True, text=True)
    if not r.stdout.strip():
        return False
    sh(["git", "add", "FINDINGS.md"], target.path)
    sh(["git", "commit", "-m", message], target.path)
    return True


def refresh_findings(target, conn):
    """Render the window over the store's rows into the target's FINDINGS.md.

    A `conn` of None makes this a no-op, like `set_phase()` and
    `record_round()`: a storeless `run_task()` has no rows to render, and the
    file it would otherwise overwrite with an empty window is left alone.
    """
    if conn is None:
        return
    write_findings(target, conn)
