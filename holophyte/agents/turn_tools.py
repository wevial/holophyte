"""The tools an SDK critic turn hosts, scoped to the ticket it judges."""
import json

from holophyte.holo.mcp_server import refusal
from holophyte.holo.mcp_tools import READ, WRITE, Answer, Field, Tool, input_schema
from holophyte.redact import outbound

SERVER = "holo_run"
VERDICTS = ("fresh", "stale", "unsure")
FRESHNESS_ASK = "End your reply with exactly one of these lines"
REPORT_ASK = ("Report your verdict by calling the `report_freshness` tool once,"
              " with a reason unless it is fresh; `read_ticket` returns the"
              " ticket above.\n\n")

VERDICT = Field("verdict", {
    "enum": list(VERDICTS),
    "description": "fresh, stale (work on main already did it or changed"
                   " what it assumes) or unsure"}, None)
REASON = Field("reason", {
    "type": "string", "minLength": 1,
    "description": "one line on why; required unless the verdict is fresh"},
    None)
TOOLS = (
    Tool("read_ticket", "The key, title and body of the ticket being judged.",
         (), tier=READ),
    Tool("report_freshness", "Report whether the ticket being judged is still"
         " relevant to main; the first report stands.", (),
         fields=(VERDICT, REASON), required=("verdict",), tier=WRITE),
)


class Scope:
    def __init__(self, task, secrets=()):
        self.task = task
        self.secrets = secrets
        self.reported = None


def allowed():
    return [f"mcp__{SERVER}__{tool.name}" for tool in TOOLS]


def report_asked(goal):
    head, marker, tail = goal.rpartition(FRESHNESS_ASK)
    return (f"{head}{REPORT_ASK}{marker}{tail}" if marker
            else f"{goal.rstrip()}\n\n{REPORT_ASK}")


def read_ticket(scope, arguments):
    body = {name: outbound(scope.task.get(field) or "", scope.secrets)
            for name, field in (("key", "id"), ("title", "title"),
                                ("body", "body"))}
    return Answer(False, json.dumps(body), body)


def report_freshness(scope, arguments):
    verdict = arguments["verdict"]
    reason = " ".join((arguments.get("reason") or "").split())
    if scope.reported is not None:
        return Answer(True, f"[holo2] report_freshness: a verdict was already"
                            f" reported ({scope.reported[0]}); it stands")
    if verdict != "fresh" and not reason:
        return Answer(True, f"[holo2] report_freshness: 'reason' is required"
                            f" unless the verdict is fresh, not {verdict}")
    scope.reported = (verdict, reason)
    return Answer(False, f"recorded: {verdict}")


HANDLERS = {"read_ticket": read_ticket, "report_freshness": report_freshness}


def answer(scope, tool, arguments):
    refused = refusal(tool, arguments)
    if refused is not None:
        return Answer(True, f"[holo2] {tool.name}: {refused}")
    return HANDLERS[tool.name](scope, arguments)


def server(sdk, scope):
    def handler(tool):
        async def handle(arguments):
            said = answer(scope, tool, arguments or {})
            return {"content": [{"type": "text", "text": said.text}],
                    "is_error": said.error}
        return handle

    return sdk.create_sdk_mcp_server(SERVER, tools=[
        sdk.SdkMcpTool(tool.name, tool.description, input_schema(tool),
                       handler(tool)) for tool in TOOLS])
