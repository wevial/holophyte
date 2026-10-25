import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from holophyte.config.project import Project
from holophyte.holo.grammar import SCOUT_PAGE
from store.enums import ScoutPhase

EXIT = {200: 0, 400: 2, 404: 1, 503: 1}
RUN_PARTS = ("files", "ledger", "turns")


def exit_code(code):
    return EXIT.get(code, 1)


def when(ms):
    if ms is None:
        return "-"
    moment = datetime.fromtimestamp(ms / 1000, timezone.utc)
    return moment.strftime("%Y-%m-%d %H:%MZ")


def minutes(value):
    return "-" if value is None else f"{value:.0f}m"


def host_attention_answer():
    from holophyte.host.registry import Host, HostError, settings
    from holophyte.serve.serve_host import host_attention
    host = Host.locate()
    try:
        return host_attention(SimpleNamespace(host=host, settings=settings(host)))
    except HostError as bad:
        return 503, {"error": str(bad)}


def runs_answer(args, project):
    from holophyte.serve.serve_runs import runs
    query = urlencode({"limit": args.limit[-1]}) if args.limit else ""
    return "GET /runs", runs(project, query)


def run_answer(args, project):
    from holophyte.serve import serve_runs
    parts = [part for part in RUN_PARTS if getattr(args, part)]
    if len(parts) > 1:
        args.leaf.error("give at most one of --files, --ledger and --turns")
    route = f"GET /runs/{args.arg0}" + "".join(f"/{part}" for part in parts)
    view = getattr(serve_runs, f"run_{parts[0]}" if parts else "run_detail")
    return route, view(project, args.arg0)


def attention_answer(args, project):
    from holophyte.serve.views import attention
    return "GET /attention", attention(project)


def board_answer(args, project):
    from holophyte.serve.views import board
    return "GET /board", board(project)


def ticket_answer(args, project):
    from holophyte.serve.views import ticket_detail
    return f"GET /tickets/{args.arg0}", ticket_detail(project, args.arg0)


def scouts_answer(args, project):
    from holophyte.scout.views import scouts
    query = urlencode({"limit": args.limit[-1]}) if args.limit else ""
    return "GET /scouts", scouts(project, query)


def scout_answer(args, project):
    from holophyte.scout.views import scout_detail
    return f"GET /scouts/{args.arg0}", scout_detail(project, args.arg0)


def thread_body(body):
    return {"scout": body["scout"]["id"], "thread": body["thread"]}


def thread_answer(args, project):
    route, (code, body) = scout_answer(args, project)
    return route, (code, thread_body(body) if code == 200 else body)


def page_refusal(args, code, body):
    if code != 200 or args.command.words != SCOUT_PAGE:
        return None
    scout = body["scout"]
    if scout["phase"] == ScoutPhase.DONE.value:
        return None
    return (f"[holo2] scout {scout['id']} is {scout['phase']}; only a done"
            " scout's report makes a page")


def page_lines(body):
    from holophyte.scout.scout_page import page_lines as lines
    return lines(body)


def runs_lines(body):
    return [f"{row['ticket']}  run {row['run']}  {row['outcome']}"
            f"  ended {when(row['ended_ms'])}"
            f"  {minutes(row['actual_min'])} of {minutes(row['estimate_min'])}"
            f"  {row['rounds']} rounds" for row in body["rows"]]


def files_lines(body):
    lines = [f"{f['status']}  +{f['added']} -{f['deleted']}  {f['path']}"
             for f in body["files"]]
    more = " (truncated)" if body["truncated"] else ""
    return lines + [f"{len(body['files'])} files  +{body['total_added']}"
                    f" -{body['total_deleted']}  {body['base']}..{body['head']}"
                    f"{more}"]


def ledger_lines(body):
    return [f"{when(e['at'])}  {e['kind']}  {e['source']}  {e['text']}"
            for e in body["entries"]]


def turns_lines(body):
    return [f"{t['id']}  {t['role']}  {t['route'] or '-'}  {t['label'] or '-'}"
            f"  {t['seconds'] if t['seconds'] is not None else '-'}s"
            for t in body["turns"]]


def item_line(item):
    subject = " ".join(f"{label}{item[key]}" for key, label in (
        ("project", ""), ("ticket", ""), ("run", "run "))
        if item.get(key) is not None)
    said = _waits_on(item) if item["kind"] == "stuck_wait" else next(
        (item[key] for key in ("question", "reason", "note", "phase",
                               "state", "error", "detail")
         if item.get(key)), "")
    return f"{item['kind']}  {subject}  {said}{triage_text(item)}".rstrip()


def _waits_on(item):
    reason = f": {item['reason']}" if item.get("reason") else ""
    return (f"waits on {item['dependency']} ({item['dependency_state']})"
            f"{reason}")


def triage_text(item):
    triage = item.get("triage")
    if not triage or triage.get("choice") is None:
        return ""
    requeued = ", requeued" if triage.get("requeued") else ""
    return f"  [{triage['choice']} {round(triage['confidence'], 2):g}{requeued}]"


def attention_lines(body):
    return ([item_line(item) for item in body["items"]]
            or [f"nothing waits on the operator ({body['level']})"])


def board_lines(body):
    return [f"{column['state']}  {entry['ticket']}  {entry['title']}"
            for column in body["columns"] for entry in column["tickets"]]


def ticket_lines(body):
    return ([f"{body['ticket']}  {body['status']}  {body['title']}"]
            + [f"  given: {line}" for line in body["acceptance_criteria"]]
            + [f"  verify: {line}" for line in body["verification_commands"]])


def scout_line(row):
    return f"scout {row['id']}  {row['kind']}  {row['phase']}  {row['ask']}"


def scouts_lines(body):
    return [scout_line(row) for row in body["scouts"]]


def scout_lines(body):
    subject = body["scout"]["subject"]
    lines = [scout_line(body["scout"])]
    if subject is not None:
        lines.append(f"about {subject['kind']}:{subject['ref']}")
    lines += [f"  task {task['id']}  pass {task['pass']} wave {task['wave']}"
              f"  {task['phase']}  {task['question']}" for task in body["tasks"]]
    lines += [f"  report pass {entry['pass']}: {entry['report']['summary']}"
              for entry in body["reports"]]
    lines += [action_line(item) for item in body["actions"]]
    return lines


def action_line(item):
    revises = item["revises"]
    if item["kind"] == "guardrail":
        return (f"A{item['position']} guardrail {item['layer']}:"
                f" {item['title']} (covers {len(item['covers'])})")
    if revises is not None:
        return (f"A{item['position']} {item['kind']} {revises['ticket']}"
                f" r{revises['revision']}: {item['title']}")
    return (f"  item {item['position']}  {item['kind']}  {item['title']}"
            f"  {item['ticket'] or 'unfiled'}")


def choice_lines(message):
    return [f"    {choice['label']}  ({choice['mode']})"
            for block in message["blocks"] if block["type"] == "proposal"
            for choice in block["choices"]]


def thread_lines(body):
    return [line for message in body["thread"] for line in [
        f"message {message['id']}  {message['author']}  {message['mode']}"
        f"  {message['text']}"
        + ("  waiting" if message["mode"] != "answer"
           and message["consumed_by"] is None else ""),
        *choice_lines(message)]]


READS = {("runs",): (runs_answer, runs_lines),
         ("run",): (run_answer, None),
         ("attention",): (attention_answer, attention_lines),
         ("board",): (board_answer, board_lines),
         ("ticket",): (ticket_answer, ticket_lines),
         ("scouts",): (scouts_answer, scouts_lines),
         ("scout", "show"): (scout_answer, scout_lines),
         ("scout", "thread"): (thread_answer, thread_lines),
         SCOUT_PAGE: (scout_answer, page_lines)}
RUN_LISTINGS = {"files": files_lines, "ledger": ledger_lines,
                "turns": turns_lines}


def run_part(args):
    return next((part for part in RUN_PARTS if getattr(args, part)), None)


def listing(args, body):
    if args.command.words == ("run",):
        return RUN_LISTINGS[run_part(args)](body)
    return READS[args.command.words][1](body)


def read(args, target, zone_name=None):
    if target is None:
        route, (code, body) = "GET /attention", host_attention_answer()
    else:
        project = Project.locate(Path(target), adopt=False)
        route, (code, body) = READS[args.command.words][0](args, project)
    if refusal := page_refusal(args, code, body):
        print(refusal, file=sys.stderr)
        return 1
    if (code == 200 and args.command.words == ("run",) and not args.json
            and not run_part(args)):
        from holophyte.holo.run_page import show as show_page
        show_page(project, body, zone_name)
    else:
        show(args, route, code == 200, body)
    return exit_code(code)


def show(args, route, ok, body):
    if getattr(args, "json", False):
        print(json.dumps(body))
    elif ok:
        for line in listing(args, body):
            print(" ".join(line.splitlines()))
    else:
        detail = f": {body['detail']}" if body.get("detail") else ""
        print(f"[holo2] {route}: {body.get('error') or 'not found'}{detail}",
              file=sys.stderr)
