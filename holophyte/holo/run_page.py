"""`holo run N` for a person: `run_detail()` and `run_files()` as a page."""
import re
import sys

from holophyte.holo.render import age, clock, colour_on, symbol, zone

PARKED = {"awaiting_merge_approval": "parked, awaiting merge approval",
          "blocked_on_operator": "parked, blocked on operator"}
PHASE_MARKS = {"awaiting_merge_approval": "!", "blocked_on_operator": "!",
               "failed": "✗", "killed": "✗", "rejected": "✗"}
EVENT_MARKS = {kind: "✗" for kind in (
    "run_cap", "route_failure", "ci_expired", "launch_loop_failed")}
NEXT = {
    ("awaiting_merge_approval", None): (
        ("holo approve {ticket}", False),
        ('holo send-back {run} "note"', True),
        ("holo babysit {ticket}", True)),
    ("failed", "failed"): (('holo requeue {ticket} "note"', False),),
}
LEDGER = "holo run {run} --ledger"
PULL = re.compile(r"/pull/(\d+)/?$")


def show(project, detail, zone_name, out=None):
    from holophyte.serve.serve_runs import run_files
    _, files = run_files(project, str(detail["run"]["id"]))
    show_page(detail, files, zone_name, out)


def show_page(detail, files, zone_name, out=None):
    out = sys.stdout if out is None else out
    lines = page(detail, files, zone(zone_name), colour_on(out))
    print("\n".join(lines), file=out)


def page(detail, files, tz=None, colour=False):
    run = detail["run"]
    rows = timeline(detail)
    width = max((len(label) for _, _, label, _ in rows), default=0)
    lines = [f"{run['ticket']}  {first_line(run['title'])}", state_line(run), ""]
    lines += [f"  {symbol(mark, colour)}  {label.ljust(width)}  {clock(at, tz)}"
              f"   {text}".rstrip() for at, mark, label, text in rows]
    return lines + ["", f"Files  {files_text(files)}",
                    f"Next   {'   ·   '.join(next_commands(run))}"]


def state_line(run):
    parts = [f"run {run['id']}",
             run["outcome"] or PARKED.get(run["phase"], run["phase"])]
    elapsed = run["elapsed_ms"] // 60000
    box = run["time_box_ms"]
    parts.append(f"{elapsed} of {box // 60000} min" if box else f"{elapsed} min")
    if run["heartbeat_age_ms"] is not None and run["phase"] not in PARKED:
        parts.append(f"heartbeat {age(run['heartbeat_age_ms'] // 1000)} ago")
    if run["pr_url"]:
        number = PULL.search(run["pr_url"])
        parts.append(f"PR #{number[1]}" if number else f"PR {run['pr_url']}")
    return " · ".join(parts)


def timeline(detail):
    rows = [(rnd["ended_ms"] or rnd["started_ms"],
             "✓" if rnd["verdict"] == "pass" else "✗", f"review r{rnd['round']}",
             round_text(rnd)) for rnd in detail["rounds"]]
    rows += [(event["at"], *event_row(event)) for event in detail["events"]]
    return sorted(rows, key=lambda row: row[0])


def event_row(event):
    summary = first_line(event["summary"])
    if event["kind"] != "phase_change":
        return EVENT_MARKS.get(event["kind"], "✓"), event["kind"], summary
    moved, _, note = summary.partition(": ")
    phase = moved.split("->")[-1].strip()
    return PHASE_MARKS.get(phase, "✓"), phase, note or moved


def round_text(rnd):
    parts = [rnd["verdict"]]
    if rnd["reviewer_model"]:
        parts.append(rnd["reviewer_model"])
    if rnd["findings"]:
        parts.append(finding_text(rnd["findings"][0]))
    return " · ".join(parts)


def finding_text(finding):
    where = finding.get("path") or ""
    if where and finding.get("line") is not None:
        where += f":{finding['line']}"
    return " ".join(part for part in (finding.get("severity"), where) if part)


def files_text(files):
    if "error" in files:
        return first_line(files["error"])
    if not files["files"]:
        return "none"
    text = " · ".join(f"{f['path']} +{f['added']} −{f['deleted']}"
                      for f in files["files"])
    return text + (" · (truncated)" if files["truncated"] else "")


def next_commands(run):
    names = {"ticket": run["ticket"], "run": run["id"]}
    rows = NEXT.get((run["phase"], run["outcome"]), ())
    return [command.format(**names) for command, needs_pr in rows
            if run["pr_url"] or not needs_pr] + [LEDGER.format(**names)]


def first_line(text):
    lines = (text or "").strip().splitlines()
    return lines[0] if lines else ""
