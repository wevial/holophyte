import re
from dataclasses import dataclass

LISTED_MAX = 40
_STAMP = re.compile(r"^\ufeff?\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ?")
_FAILURE = re.compile(
    r"(?:FAIL|ERROR): \S+ \(\S+\)"
    r"|(?:FAILED|ERROR) \S*::"
    r"|\d+\) \[[^\]]+\] › "
    r"|[\w.]*(?:Error|Exception):")
_DURATION = re.compile(
    r"\(\s*(?:\d+(?:\.\d+)?\s*(?:ms|s|m|h|sec|seconds?|min)\s*)+\)", re.I)
_RETRY = re.compile(r"\(?\bretry #\d+\)?", re.I)
_HEX = re.compile(r"[0-9a-fA-F]{7,}")
_CHARS = re.compile(r"\[\d+ chars\]")


def failure_lines(log):
    """A job log's test failure headers and error lines, first seen first."""
    found = (_STAMP.sub("", line).strip() for line in (log or "").splitlines())
    return list(dict.fromkeys(line for line in found if _FAILURE.match(line)))


def normalized(line):
    line = _CHARS.sub("[N chars]", _RETRY.sub("", _DURATION.sub("", line)))
    return " ".join(_HEX.sub("HEX", line).split())


@dataclass(frozen=True)
class Failure:
    """What a red read hands a check fix round: check names are kept apart
    from the signature because shard names change from run to run."""
    lines: tuple
    signature: frozenset
    names: frozenset

    @classmethod
    def read(cls, names, logs):
        lines = tuple(dict.fromkeys(
            line for log in logs for line in failure_lines(log)))
        return cls(lines, frozenset(map(normalized, lines)), frozenset(names))

    def repeated_round(self, handed):
        """The first earlier round (1-based) this failure repeats, or None."""
        for number, earlier in enumerate(handed, 1):
            if (self.signature <= earlier.signature if self.signature
                    else self.names <= earlier.names):
                return number
        return None
