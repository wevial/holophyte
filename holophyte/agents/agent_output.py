import json
from pathlib import Path

TRANSPORT_SIGNATURES = (
    "ECONNRESET", "ECONNREFUSED", "ENOTFOUND", "ETIMEDOUT", "getaddrinfo",
    "fetch failed", "Could not resolve host", "502 Bad Gateway",
    "503 Service Unavailable", "504 Gateway Timeout", "overloaded_error",
    "network error",
)
CRASH_TAIL_CHARS = 16384
PANIC_SUMMARY_CHARS = 200


def killed_by_signal(exit_code, timed_out):
    return not timed_out and exit_code is not None and (
        exit_code < 0 or exit_code >= 128)


def transport_failure(exit_code, output):
    if exit_code is None or exit_code == 0:
        return None
    tail = output[-4000:].casefold()
    return next((sig for sig in TRANSPORT_SIGNATURES
                 if sig.casefold() in tail), None)


def count(value):
    return value if type(value) is int and value >= 0 else None


def claude_usage(document):
    usage = document.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    inputs = [count(usage.get(key)) for key in (
        "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
    cost = document.get("total_cost_usd")
    return dict(
        input_tokens=sum(filter(None, inputs)) if any(n is not None for n in inputs)
        else None,
        output_tokens=count(usage.get("output_tokens")),
        cost_usd=cost if type(cost) in (int, float) and cost >= 0 else None,
    )


def result_documents(output):
    try:
        document = json.loads(output)
    except ValueError:
        return [document for document in map(json_line, output.splitlines())
                if isinstance(document, dict)
                and document.get("type", "result") == "result"]
    return [document] if isinstance(document, dict) else []


def json_line(line):
    try:
        return json.loads(line)
    except ValueError:
        return None


def streamed(output):
    return any(isinstance(json_line(line), dict) for line in output.splitlines())


def chosen_result(documents):
    answered = [document for document in documents
                if isinstance(document.get("result"), str) and document["result"]]
    return (answered or documents or [None])[-1]


def summed(values):
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def claude_result(output):
    documents = result_documents(output)
    chosen = chosen_result(documents)
    if chosen is None or not isinstance(chosen.get("result"), str):
        return None
    usages = [claude_usage(document) for document in documents]
    return chosen["result"], dict(
        input_tokens=summed(usage["input_tokens"] for usage in usages),
        output_tokens=summed(usage["output_tokens"] for usage in usages),
        cost_usd=usages[-1]["cost_usd"],
        num_turns=summed(count(document.get("num_turns"))
                         for document in documents))


def claude_json_turn(argv, harness=None):
    pairs = list(map(list, zip(argv, argv[1:])))
    return ((harness or Path(argv[0]).name) == "claude"
            and any(f"--output-format={kind}" in argv
                    or ["--output-format", kind] in pairs
                    for kind in ("json", "stream-json")))


def claude_marked_error(output):
    chosen = chosen_result(result_documents(output))
    return (chosen is not None and chosen.get("type") == "result"
            and chosen.get("is_error") is True)


class AgentOutput(str):

    def __new__(cls, output, command, *, timed_out=False, exit_code=0):
        result = super().__new__(cls, output)
        result.command = command
        result.exit_code = None if timed_out else exit_code
        result.timed_out = timed_out
        return result


class ImplementerOutput(AgentOutput):

    def __new__(cls, output, exit_code, command=""):
        result = super().__new__(cls, output, command)
        result.exit_code = exit_code
        return result
