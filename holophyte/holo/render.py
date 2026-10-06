"""Shared pieces of the `holo` pages: clock times, ages, hashes and symbols."""
import os
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SHORT_HASH = 7
RESET = "\x1b[0m"
COLOURS = {
    "!": "\x1b[38;5;208m",
    ">": "\x1b[34m",
    "✓": "\x1b[32m",
    "✗": "\x1b[31m",
}


def zone(name):
    if name is None:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def moment(ms, tz=None):
    at = datetime.fromtimestamp(ms / 1000, tz)
    return at if tz is not None else at.astimezone()


def clock(ms, tz=None, seconds=False):
    return moment(ms, tz).strftime("%H:%M:%S %Z" if seconds else "%H:%M %Z")


def day(ms, tz=None):
    at = moment(ms, tz)
    return f"{at:%a %b} {at.day}"


def age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h"


def age_since(ms, now_ms):
    return age((now_ms - ms) // 1000)


def short_hash(sha):
    return sha[:SHORT_HASH] if sha else None


def colour_on(stream):
    if os.environ.get("NO_COLOR"):
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def symbol(mark, colour):
    return f"{COLOURS[mark]}{mark}{RESET}" if colour else mark
