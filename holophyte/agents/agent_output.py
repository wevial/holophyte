import json

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


def claude_result(output):
    try:
        document = json.loads(output)
    except ValueError:
        return None
    if not isinstance(document, dict) or not isinstance(document.get("result"), str):
        return None
    return document["result"], dict(claude_usage(document),
                                    num_turns=count(document.get("num_turns")))


def claude_marked_error(output):
    try:
        document = json.loads(output)
    except ValueError:
        return False
    return isinstance(document, dict) and document.get("is_error") is True


class AgentOutput(str):

    def __new__(cls, output, command, *, timed_out=False, exit_code=0):
        result = super().__new__(cls, output)
        result.command = command
        result.exit_code = None if timed_out else exit_code
        result.timed_out = timed_out
        result.exit_code = exit_code
        return result


class ImplementerOutput(AgentOutput):

    def __new__(cls, output, exit_code, command=""):
        result = super().__new__(cls, output, command)
        result.exit_code = exit_code
        return result
