import json
from typing import NamedTuple

import store.read
from holophyte.redact import known_secrets, outbound
from holophyte.scout.report import GUARDRAIL, LAYERS
from holophyte.scout.scout_config import scout_config
from holophyte.serve.serve_findings import FINDINGS_CAP, findings

WINDOW_MS = 30 * 24 * 60 * 60 * 1000
SHOWN_ROWS = 8
TEXT_CHARS = 300
SECTION_CHARS = 24000
SECTION_CUT = f"(digest cut at {SECTION_CHARS} characters)"
NO_PATH = "follow-ups with no path"
GUARDRAIL_STATES = ("pending", "ledger")


class Row(NamedTuple):
    id: str
    source: str
    kind: str
    path: str | None
    line: int | None
    ticket: str
    text: str
    at: int


class Cluster(NamedTuple):
    label: str
    rows: list

    def tickets(self):
        return len({row.ticket for row in self.rows})

    def newest(self):
        return sorted(self.rows, key=lambda row: -row.at)


class Digest(NamedTuple):
    text: str
    clusters: list
    summary: str
    since: int

    def row_ids(self):
        return {row.id for cluster in self.clusters
                for row in cluster.newest()[:SHOWN_ROWS]
                if f"- {row.id} · " in self.text}


class DigestUnavailable(RuntimeError):
    pass


def finding_rows(run, since):
    for served in run["rounds"]:
        if (served["ended_ms"] or 0) < since:
            continue
        for n, finding in enumerate(served["findings"], 1):
            yield Row(f"F{run['run']}.{served['round']}.{n}", "finding",
                      finding.get("severity") or "finding",
                      finding.get("path"), finding.get("line"), run["ticket"],
                      finding.get("message") or "", served["ended_ms"] or 0)


def follow_up_rows(follow_ups):
    return [Row(f"U{each['id']}", "follow-up", each["kind"], each["path"],
                each["line"], each["ticket"], each["text"] or "",
                each["created_ms"])
            for each in follow_ups
            if each["kind"] == "guardrail" and each["state"] in GUARDRAIL_STATES]


def defect_rows(defects):
    return [Row(f"D{each['run']}", "defect", each["kind"], None, None,
                each["ticket"],
                ": ".join(part for part in (", ".join(each["criteria"]),
                                            each["reason"]) if part),
                each["at"])
            for each in defects]


def clusters_of(rows):
    by_path, loose, wording = {}, [], {}
    for row in rows:
        if row.source == "defect":
            wording.setdefault(f"ticket wording: {row.kind}", []).append(row)
        elif row.path:
            by_path.setdefault(row.path, []).append(row)
        elif row.source == "follow-up":
            loose.append(row)
    kept = [Cluster(path, members) for path, members in by_path.items()
            if len({row.ticket for row in members}) > 1
            or any(row.source == "follow-up" for row in members)]
    kept += [Cluster(label, members) for label, members in wording.items()]
    if loose:
        kept.append(Cluster(NO_PATH, loose))
    return sorted(kept, key=lambda c: (-c.tickets(), -len(c.rows), c.label))


def plural(count, noun):
    return f"{count} {noun}" + ("" if count == 1 else "s")


def row_line(row, secrets):
    where = row.path and (f"{row.path}:{row.line}" if row.line else row.path)
    text = outbound(" ".join(row.text.split()), secrets)[:TEXT_CHARS]
    return " · ".join(part for part in (row.id, row.kind, where, row.ticket,
                                        text) if part)


def cluster_block(number, cluster, secrets):
    rows = cluster.newest()
    lines = [f"Cluster {number} · {cluster.label} · "
             f"{plural(cluster.tickets(), 'ticket')} · "
             f"{plural(len(rows), 'row')}"]
    lines += [f"- {row_line(row, secrets)}" for row in rows[:SHOWN_ROWS]]
    if len(rows) > SHOWN_ROWS:
        lines.append(f"(+{len(rows) - SHOWN_ROWS} more)")
    return "\n".join(lines)


def instruction():
    return ("Plan one \"read\" task per cluster below. Each asks which "
            "protection on main already covers the cluster's rows, if any, "
            "and the first layer of the ladder (" + ", ".join(LAYERS)
            + ") that would have caught them. Cite rows by their ids.")


def section(kept, cut, secrets):
    blocks = [cluster_block(n, cluster, secrets)
              for n, cluster in enumerate(kept, 1)]
    if cut:
        blocks.append(f"({plural(cut, 'cluster')} cut)")
    text = ("## Findings digest\n\n" + instruction() + "\n\n"
            + "\n\n".join(blocks))
    if len(text) > SECTION_CHARS:
        text = text[:SECTION_CHARS - len(SECTION_CUT) - 1] + "\n" + SECTION_CUT
    return text + "\n\n"


def summary(kept, cut):
    rows = [row for cluster in kept for row in cluster.rows]
    counts = {source: sum(row.source == source for row in rows)
              for source in ("finding", "follow-up", "defect")}
    return " · ".join(
        ["digest: " + plural(len(kept), "cluster"),
         plural(counts["finding"], "finding"),
         plural(counts["follow-up"], "follow-up"),
         plural(counts["defect"], "defect")]
        + ([f"{cut} cut"] if cut else []))


def window_rows(project, since):
    status, body = findings(project, f"since={since}&limit={FINDINGS_CAP}")
    if status != 200:
        raise DigestUnavailable(f"the findings view answered {status}: "
                                f"{body.get('error')}")
    rows = [row for run in body["runs"] for row in finding_rows(run, since)]
    return (rows + follow_up_rows(body["follow_ups"])
            + defect_rows(body["ticket_defects"]))


def filed_covers(project, scout):
    conn = store.read.open_readonly(project.store_path)
    try:
        items = conn.execute(
            "SELECT a.item FROM scoutActions a JOIN scouts s"
            " ON s.id = a.scoutId WHERE s.projectId = ? AND s.id != ?"
            " AND a.ticketKey IS NOT NULL",
            (scout.project_id, scout.id)).fetchall()
    finally:
        conn.close()
    covered = set()
    for (item,) in items:
        action = json.loads(item)
        if action["kind"] == GUARDRAIL:
            covered.update(action["covers"])
    return covered


def findings_digest(project, scout, now):
    since = now - WINDOW_MS
    covered = filed_covers(project, scout)
    ranked = clusters_of([row for row in window_rows(project, since)
                          if row.id not in covered])
    if not ranked:
        return None
    limit = scout.helper_turns or scout_config(project).helper_turns
    kept, cut = ranked[:limit], len(ranked[limit:])
    secrets = known_secrets(project.config())
    return Digest(section(kept, cut, secrets), kept, summary(kept, cut), since)
