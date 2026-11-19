import json
import re
from pathlib import Path

from holophyte.agents.agent_output import AgentOutput

SCHEMA_FILE = Path(__file__).with_name("review_reply.schema.json")

TARGET_FIELD = "target"

TYPES = {"object": dict, "array": list, "string": str, "integer": int,
         "null": type(None)}

REPLY_BRIEF = (
    "Do not modify anything. Your reply is one JSON object that the turn's "
    "output schema checks, with these fields:\n"
    "- verdict: APPROVE or REQUEST_CHANGES.\n"
    "- criteria: one entry per numbered acceptance criterion, each with id "
    "(C1, C2, ...), status (met, not_met or unwitnessed), reason, and "
    "witness: the test or check that witnesses it, named as "
    "`tests/file.py::TestClass::test_name`, or null.\n"
    "- findings: each concrete blocker, with path and line (null when none "
    "applies), severity (p0, p1 or p2), category (one of correctness, "
    "security, tests, performance, maintainability, docs), " + TARGET_FIELD
    + " (code, or description for a finding about the pull request "
    "description rather than the code) and message.\n"
    "- follow_ups: each with kind (feature or guardrail), text, and the path "
    "and line it concerns, or null.\n"
    "- scope: one entry per changed file the ticket does not name, with "
    "path, status (needed or tangent) and reason; empty when none is listed.\n"
    "Where these instructions ask for a CRITERION, SCOPE, FOLLOW_UP or "
    "VERDICT line, give it as that field instead. With REQUEST_CHANGES, list "
    "only concrete blockers in findings; a refuted finding is left out.")


def reply_brief(project, line_format, schema_tail=""):
    return REPLY_BRIEF + schema_tail if schema_enforced(project) else line_format


def schema_enforced(project):
    from holophyte.agents.agent_routes import routes
    from holophyte.agents.harness import Codex, seat

    if routes(project).commands.get("review"):
        return False
    reviewer = seat(project, "review")
    return reviewer is not None and reviewer.name == Codex.name


def valid(value, rule):
    kinds = rule["type"]
    kinds = [kinds] if isinstance(kinds, str) else kinds
    if not any(isinstance(value, TYPES[kind]) and not isinstance(value, bool)
               for kind in kinds):
        return False
    if "enum" in rule and value not in rule["enum"]:
        return False
    if isinstance(value, list):
        return all(valid(item, rule["items"]) for item in value)
    if isinstance(value, dict):
        fields = rule["properties"]
        return (set(rule["required"]) <= value.keys() <= fields.keys()
                and all(valid(value[key], fields[key]) for key in value))
    return True


def reply_object(reply):
    starts, offset = [], 0
    for line in reply.splitlines(keepends=True):
        indent = len(line) - len(line.lstrip())
        if line[indent:indent + 1] == "{":
            starts.append(offset + indent)
        offset += len(line)
    decoder = json.JSONDecoder()
    schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    for start in reversed(starts):
        try:
            value = decoder.raw_decode(reply, start)[0]
        except ValueError:
            continue
        if valid(value, schema):
            return value
    return None


def one_line(text):
    return " ".join((text or "").split())


def place(path, line):
    return f"{path}:{line}" if path and line is not None and line > 0 else path


def finding_line(finding):
    severity, message = finding["severity"], one_line(finding["message"])
    if finding[TARGET_FIELD] == "description":
        return f'- path: "PR description" [{severity}] {message}'
    where = place(one_line(finding["path"]), finding["line"])
    tags = f"[{severity}] [{finding['category']}] {message}"
    return f"- {where} {tags}" if where else f"- {tags}"


def criterion_line(criterion):
    number = re.search(r"\d+", criterion["id"])
    label = f"C{int(number[0])}" if number else one_line(criterion["id"])
    line = (f"CRITERION {label}: {criterion['status'].replace('_', ' ')} \u2014 "
            f"{one_line(criterion['reason'])}")
    witness = one_line(criterion["witness"])
    return f"{line}; witness: {witness}" if witness else line


def follow_up_line(follow_up):
    line = f"FOLLOW_UP({follow_up['kind']}): {one_line(follow_up['text'])}"
    if follow_up["path"]:
        line += f" @ {place(one_line(follow_up['path']), follow_up['line'])}"
    return line


def scope_line(entry):
    return (f"SCOPE `{one_line(entry['path'])}`: {entry['status']} \u2014 "
            f"{one_line(entry['reason'])}")


def rendered(value):
    lines = [*map(finding_line, value["findings"]),
             *map(follow_up_line, value["follow_ups"]),
             *map(scope_line, value["scope"]),
             *map(criterion_line, value["criteria"]),
             f"VERDICT: {value['verdict']}"]
    return "\n".join(lines)


def read_reply(reply):
    value = reply_object(reply)
    if value is None:
        return reply, "markdown"
    text = rendered(value)
    if isinstance(reply, AgentOutput):
        text = AgentOutput(text, reply.command)
        text.__dict__.update(reply.__dict__)
    return text, "json"


def record_format(conn, run_id, reply_format, review_round):
    if conn is None or run_id is None:
        return
    import store
    store.record_event(conn, run_id, "review_reply_format",
                       f"review reply format: {reply_format}", level="detail",
                       payload=json.dumps({"format": reply_format,
                                           "round": review_round}))
