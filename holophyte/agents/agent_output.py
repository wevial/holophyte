TRANSPORT_SIGNATURES = (
    "ECONNRESET", "ECONNREFUSED", "ENOTFOUND", "ETIMEDOUT", "getaddrinfo",
    "fetch failed", "Could not resolve host", "502 Bad Gateway",
    "503 Service Unavailable", "504 Gateway Timeout", "overloaded_error",
    "network error",
)
CRASH_TAIL_CHARS = 16384


def killed_by_signal(exit_code, timed_out):
    return not timed_out and exit_code is not None and (
        exit_code < 0 or exit_code >= 128)


def transport_failure(exit_code, output):
    if exit_code is None or exit_code == 0:
        return None
    tail = output[-4000:].casefold()
    return next((sig for sig in TRANSPORT_SIGNATURES
                 if sig.casefold() in tail), None)


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
