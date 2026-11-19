import json
import re
import shlex
from pathlib import Path

from holophyte.agents.agent_output import AgentOutput
from holophyte.review.briefs import REFUTED_CLOSE, REFUTED_OPEN
from holophyte.review.reply_parsing import (
    UNPARSED_PATH,
    WORKSPACE_PREFIXES,
    parse_findings,
)

SCHEMA_FILE = Path(__file__).with_name("review_reply.schema.json")

TARGET_FIELD = "target"

TOKEN_COUNT_TAIL_RE = re.compile(r"\s*(?:tokens used\s+[\d,]+\s*)?")

MD_LINK_RE = re.compile(r"(`[^`]*`)|\[([^\]\n]*)\]\(([^)\s]+)\)")

LINE_BREAK_RE = re.compile(r"[ \t]*[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]+[ \t]*")

UNPLACED = "[unplaced](#)"

NO_REASON = "(no reason given)"

UNNAMED_WITNESS = "met claimed but no test or check named"

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
    "- scope: one entry per file a changed-files list above asks about, "
    "with path, status (needed or tangent) and reason; empty when none is "
    "listed.\n"
    "- refuted: each finding a verification refuted, with finding and the "
    "reason it did not hold; empty when none was.\n"
    "Where these instructions ask for a criterion, scope, follow-up or "
    "verdict line or a refuted-findings section, give it as that field "
    "instead. With REQUEST_CHANGES, list only concrete blockers in findings.")


class SchemaBrief(str):
    def __new__(cls, text, line_text, schema_text):
        brief = super().__new__(cls, text)
        brief.line_text = line_text
        brief.schema_text = schema_text
        return brief

    def __add__(self, more):
        return SchemaBrief(str(self) + more, self.line_text + more,
                           self.schema_text + more)

    def __radd__(self, head):
        return SchemaBrief(head + str(self), head + self.line_text,
                           head + self.schema_text)


def reply_brief(project, line_format, schema_tail=""):
    schema_text = REPLY_BRIEF + schema_tail
    text = schema_text if schema_enforced(project) else line_format
    return SchemaBrief(text, line_format, schema_text)


def route_brief(project, goal, command=None):
    if not isinstance(goal, SchemaBrief):
        return goal
    schemed = schema_command(command) if command else schema_enforced(project)
    if not schemed:
        return goal.line_text
    return SchemaBrief(goal.schema_text, goal.line_text, goal.schema_text)


def schema_command(command):
    from holophyte.agents.harness import Codex

    return "--output-schema" in Codex.schemed_command(shlex.split(command))


def schema_enforced(project):
    from holophyte.agents.agent_routes import routes
    from holophyte.agents.harness import Codex, seat
    from holophyte.agents.probes import container_fallback_profile
    from holophyte.config.reader import AGENT_CONFIG_KEYS, config_table

    if command := routes(project).commands.get("review"):
        return (container_fallback_profile(project, "review") is not None
                or schema_command(command))
    configured = config_table(project, "agents").get(AGENT_CONFIG_KEYS["review"])
    if configured is None:
        return True
    if isinstance(configured, str):
        return schema_command(configured)
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
    for start in reversed(starts):
        try:
            value, end = decoder.raw_decode(reply, start)
        except ValueError:
            continue
        if TOKEN_COUNT_TAIL_RE.fullmatch(reply, end) is None:
            continue
        return value if schema_valid(value) else None
    return None


def schema_valid(value):
    return valid(value, json.loads(SCHEMA_FILE.read_text(encoding="utf-8")))


def kept_line(text):
    return LINE_BREAK_RE.sub(" ", text or "").strip()


def candidate_path(path):
    for prefix in WORKSPACE_PREFIXES:
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


def message_line(text):
    return MD_LINK_RE.sub(
        lambda link: link[1] or f"{link[2]} ({link[3]})", kept_line(text))


def place(path, line):
    return f"{path}:{line}" if path and line is not None and line > 0 else path


def finding_line(finding):
    severity, message = finding["severity"], message_line(finding["message"])
    if finding[TARGET_FIELD] == "description":
        tagged = f'- path: "PR description" [{severity}]'
        rendered_line = f"{tagged} {message}"
        if parsed_place(rendered_line)[0].startswith(f"{UNPARSED_PATH}:"):
            return rendered_line
        return f"{tagged} {UNPLACED} {message}"
    path, line = candidate_path(kept_line(finding["path"])), finding["line"]
    where = place(path, line)
    tags = f"[{severity}] [{finding['category']}] {message}"
    if not where:
        return f"- {UNPLACED} {tags}"
    expected = (path, line if line is not None and line > 0 else None, severity)
    for rendered_line in (f"- {where} {tags}", f"- [{where}]({where}) {tags}"):
        if parsed_place(rendered_line) == expected:
            return rendered_line
    return f"- {UNPLACED} {tags} (at {where})"


def parsed_place(rendered_line):
    [parsed] = parse_findings(rendered_line)
    return parsed["path"], parsed.get("line"), parsed["severity"]


def criterion_line(criterion):
    number = re.search(r"\d+", criterion["id"])
    label = f"C{int(number[0])}" if number else kept_line(criterion["id"])
    status = criterion["status"].replace("_", " ")
    reason, witness = kept_line(criterion["reason"]), kept_line(criterion["witness"])
    if status == "met" and not (reason or witness):
        status, reason = "unwitnessed", UNNAMED_WITNESS
    line = f"CRITERION {label}: {status} \u2014 {reason or NO_REASON}"
    return f"{line}; witness: {witness}" if witness else line


def follow_up_line(follow_up):
    line = f"FOLLOW_UP({follow_up['kind']}): {kept_line(follow_up['text'])}"
    if follow_up["path"]:
        path = candidate_path(kept_line(follow_up["path"]))
        line += f" @ {place(path, follow_up['line'])}"
    return line


def scope_line(entry):
    return (f"SCOPE `{kept_line(entry['path'])}`: {entry['status']} \u2014 "
            f"{kept_line(entry['reason'])}")


def refuted_lines(refuted):
    if not refuted:
        return []
    notes = [f"- {message_line(entry['finding'])} \u2014 "
             f"{kept_line(entry['reason'])}" for entry in refuted]
    return [REFUTED_OPEN, *notes, REFUTED_CLOSE]


def rendered(value):
    findings = [*map(finding_line, value["findings"])]
    lines = [*findings, *([""] if findings else []),
             *map(follow_up_line, value["follow_ups"]),
             *map(scope_line, value["scope"]),
             *refuted_lines(value["refuted"]),
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
