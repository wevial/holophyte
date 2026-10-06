import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from holophyte.config.project import Project

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


def runs_lines(body):
    return [f"{row['ticket']}  {row['outcome']}  ended {when(row['ended_ms'])}"
            f"  {minutes(row['actual_min'])} of {minutes(row['estimate_min'])}"
            f"  {row['rounds']} rounds" for row in body["rows"]]


def run_lines(body):
    run = body["run"]
    lines = [f"run {run['id']}  {run['ticket']}  attempt {run['attempt']}"
             f"  {run['outcome'] or run['phase']}  {run['title']}"]
    lines += [f"  round {r['round']}  {r['verdict'] or 'open'}"
              f"  {r['reviewer_model'] or '-'}" for r in body["rounds"]]
    return lines


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
    return [f"{t['id']}  {t['role']}  {t['route']}  {t['label'] or '-'}"
            f"  {t['seconds'] if t['seconds'] is not None else '-'}s"
            for t in body["turns"]]


def item_line(item):
    subject = " ".join(f"{label}{item[key]}" for key, label in (
        ("project", ""), ("ticket", ""), ("run", "run "))
        if item.get(key) is not None)
    said = next((item[key] for key in ("question", "reason", "note", "phase",
                                       "state", "error", "detail")
                 if item.get(key)), "")
    return f"{item['kind']}  {subject}  {said}".rstrip()


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


READS = {("runs",): (runs_answer, runs_lines),
         ("run",): (run_answer, run_lines),
         ("attention",): (attention_answer, attention_lines),
         ("board",): (board_answer, board_lines),
         ("ticket",): (ticket_answer, ticket_lines)}
RUN_LISTINGS = {"files": files_lines, "ledger": ledger_lines,
                "turns": turns_lines}


def listing(args, body):
    if args.command.words == ("run",):
        part = next((part for part in RUN_PARTS if getattr(args, part)), None)
        if part is not None:
            return RUN_LISTINGS[part](body)
    return READS[args.command.words][1](body)


def read(args, target):
    if target is None:
        route, (code, body) = "GET /attention", host_attention_answer()
    else:
        project = Project.locate(Path(target), adopt=False)
        route, (code, body) = READS[args.command.words][0](args, project)
    if args.json:
        print(json.dumps(body))
    elif code == 200:
        for line in listing(args, body):
            print(line)
    else:
        detail = f": {body['detail']}" if body.get("detail") else ""
        print(f"[holo2] {route}: {body.get('error') or 'not found'}{detail}",
              file=sys.stderr)
    return exit_code(code)
