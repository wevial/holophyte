from __future__ import annotations

import builtins
import contextlib
import os
import re
import tomllib

REDACTED = "[redacted]"
# Matched on the key's last segment, so `token_file` is not one.
SECRET_SUFFIXES = ("token", "key")
# A native board's ticket prefix is in every ticket id; hiding it protects nothing.
PUBLIC_PATHS = frozenset({("board", "key")})
ENV_SECRETS = ("LINEAR_API_KEY", "GH_TOKEN", "GITHUB_TOKEN",
               "HOLOPHYTE_MEDIA_ACCESS_KEY_ID", "HOLOPHYTE_MEDIA_SECRET_ACCESS_KEY")
BARE_KEY = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                     "0123456789_-")
WHITESPACE = " \t"


class RedactionError(Exception):
    pass


class Scan(Exception):
    pass


def is_secret(key):
    return key.endswith(SECRET_SUFFIXES)


def secret_at(path):
    return is_secret(path[-1]) and path not in PUBLIC_PATHS


def under_secret(path):
    return any(secret_at(path[:n + 1])
               for n, p in enumerate(path) if isinstance(p, str))


def spans(text):
    found = []
    try:
        _walk(text, found)
    except Scan:
        pass
    return found


def _walk(text, found):
    n = len(text)
    pos = 0
    table = ()
    # `[a.b]` under an `[[a]]` names the current element.
    arrays = {}
    while pos < n:
        pos = _skip_blank(text, pos)
        if pos >= n:
            break
        ch = text[pos]
        if ch == "#":
            pos = _end_of_line(text, pos)
        elif ch == "[":
            double = text.startswith("[[", pos)
            pos = _skip_ws(text, pos + (2 if double else 1))
            segments, pos = _key(text, pos)
            table = _header_path(segments, double, arrays)
            pos = _skip_ws(text, pos)
            close = "]]" if double else "]"
            if not text.startswith(close, pos):
                raise Scan(pos)
            pos = _end_of_line(text, pos + len(close))
        else:
            pos = _pair(text, pos, table, found)
            pos = _end_of_line(text, pos)
    return pos


def _header_path(segments, double, arrays):
    path = ()
    for i, segment in enumerate(segments):
        path += (segment,)
        if double and i == len(segments) - 1:
            arrays[path] = arrays.get(path, -1) + 1
        if path in arrays:
            path += (arrays[path],)
    return path


def _pair(text, pos, prefix, found):
    keys, pos = _key(text, pos)
    pos = _skip_ws(text, pos)
    if pos >= len(text) or text[pos] != "=":
        raise Scan(pos)
    pos = _skip_ws(text, pos + 1)
    path = prefix + keys
    start = pos
    # `[extra.api_key]`, `api_key.value = ...` and `api_key = {...}` are all secret.
    secret = under_secret(path)
    pos = _value(text, pos, path, None if secret else found)
    if secret:
        found.append((path, start, pos))
    return pos


def _key(text, pos):
    segments = []
    while True:
        pos = _skip_ws(text, pos)
        if pos >= len(text):
            raise Scan(pos)
        ch = text[pos]
        if ch in "\"'":
            end = _string(text, pos)
            segments.append(_parse("k = " + text[pos:end]))
            pos = end
        else:
            end = pos
            while end < len(text) and text[end] in BARE_KEY:
                end += 1
            if end == pos:
                raise Scan(pos)
            segments.append(text[pos:end])
            pos = end
        after = _skip_ws(text, pos)
        if after < len(text) and text[after] == ".":
            pos = after + 1
            continue
        return tuple(segments), pos


def _value(text, pos, path, found):
    n = len(text)
    if pos >= n:
        raise Scan(pos)
    ch = text[pos]
    if ch in "\"'":
        return _string(text, pos)
    if ch == "[":
        return _array(text, pos + 1, path, found)
    if ch == "{":
        return _inline_table(text, pos + 1, path, found)
    # A bare scalar runs to its separator: a date-time may hold a space.
    end = pos
    while end < n and text[end] not in ",]}#\r\n":
        end += 1
    while end > pos and text[end - 1] in WHITESPACE:
        end -= 1
    if end == pos:
        raise Scan(pos)
    return end


def _array(text, pos, path, found):
    n = len(text)
    index = 0
    while True:
        pos = _skip_blank(text, pos)
        while pos < n and text[pos] == "#":
            pos = _skip_blank(text, _end_of_line(text, pos))
        if pos >= n:
            raise Scan(pos)
        if text[pos] == "]":
            return pos + 1
        if text[pos] == ",":
            pos += 1
            continue
        pos = _value(text, pos, path + (index,), found)
        index += 1


def _inline_table(text, pos, path, found):
    n = len(text)
    while True:
        pos = _skip_ws(text, pos)
        if pos >= n:
            raise Scan(pos)
        if text[pos] == "}":
            return pos + 1
        if text[pos] == ",":
            pos += 1
            continue
        pos = _pair(text, pos, path, found if found is not None else [])


def _string(text, pos):
    quote = text[pos]
    n = len(text)
    if text.startswith(quote * 3, pos):
        pos += 3
        while pos < n:
            if quote == '"' and text[pos] == "\\":
                pos += 2
                continue
            if text.startswith(quote * 3, pos):
                pos += 3
                # Up to two more quotes belong to the string's contents.
                extra = 0
                while extra < 2 and pos < n and text[pos] == quote:
                    pos += 1
                    extra += 1
                return pos
            pos += 1
        raise Scan(pos)
    pos += 1
    while pos < n:
        if quote == '"' and text[pos] == "\\":
            pos += 2
            continue
        if text[pos] == quote:
            return pos + 1
        if text[pos] in "\r\n":
            raise Scan(pos)
        pos += 1
    raise Scan(pos)


def _skip_ws(text, pos):
    while pos < len(text) and text[pos] in WHITESPACE:
        pos += 1
    return pos


def _skip_blank(text, pos):
    while pos < len(text) and text[pos] in " \t\r\n":
        pos += 1
    return pos


def _end_of_line(text, pos):
    while pos < len(text) and text[pos] not in "\r\n":
        pos += 1
    return pos


def _parse(fragment):
    try:
        return tomllib.loads(fragment)["k"]
    except (tomllib.TOMLDecodeError, KeyError):
        return None


def _rewrite(text, replacements):
    out = []
    at = 0
    for start, end, new in sorted(replacements):
        out.append(text[at:start])
        out.append(new)
        at = end
    out.append(text[at:])
    return "".join(out)


def secret_leaves(document):
    leaves = {}

    def walk(node, prefix, under):
        for key, value in node.items():
            path = prefix + (key,)
            secret = under or secret_at(path)
            if isinstance(value, dict):
                walk(value, path, secret)
            elif isinstance(value, list) and any(
                    isinstance(item, (dict, list)) for item in value):
                walk_list(value, path, secret)
            elif secret:
                leaves[path] = value

    def walk_list(items, prefix, under):
        for index, item in enumerate(items):
            path = prefix + (index,)
            if isinstance(item, dict):
                walk(item, path, under)
            elif isinstance(item, list):
                walk_list(item, path, under)
            elif under:
                leaves[path] = item

    walk(document, (), False)
    return leaves


def redact(text):
    """A parsable text whose redaction still shows a secret is refused, not served."""
    quoted = '"' + REDACTED + '"'
    redacted = _rewrite(text, [(s, e, quoted) for _, s, e in spans(text)])
    try:
        original = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return redacted
    try:
        result = secret_leaves(tomllib.loads(redacted))
    except tomllib.TOMLDecodeError as bad:
        raise RedactionError(f"redaction left the file unparsable: {bad}")
    for path in secret_leaves(original):
        # A leaf under a secret-named table is gone with it: the ancestor holds it.
        if not any(result.get(path[:n]) == REDACTED
                   for n in range(len(path), 0, -1)):
            raise RedactionError(
                f"{describe(path)}: the value would still be readable;"
                " not served")
    return redacted


def describe(path):
    parts = [str(p) for p in path if not isinstance(p, int)]
    if len(parts) == 1:
        return parts[0]
    return f"[{'.'.join(parts[:-1])}] {parts[-1]}"


def restore(text, current):
    """A placeholder is put back by its indexed path, not by its order in the text."""
    held = {path: current[start:end] for path, start, end in spans(current)}
    missing = []
    replacements = []
    for path, start, end in spans(text):
        if _parse("k = " + text[start:end]) != REDACTED:
            continue
        if path not in held:
            missing.append(describe(path))
            continue
        replacements.append((start, end, held[path]))
    if missing:
        raise ValueError(
            f"{', '.join(missing)}: {REDACTED} stands for a value the current"
            " file does not hold; write the value")
    return _rewrite(text, replacements)


# The value runs to the end of the line, so a string with a space is gone whole.
PROSE_PAIR = re.compile(
    r"""(?im)(["']?[\w.-]*(?:token|key)["']?\s*[=:]\s*)(?!\s*$)[^\r\n]+""")


# Kept for the process lifetime, as a rotated source's old values can still be
# echoed. Never persist this registry.
_environment_values = frozenset()


def register_values(values):
    global _environment_values
    held = set(values)
    held.update(value[1:-1] for value in tuple(held)
                if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0])
    _environment_values = _environment_values | frozenset(v for v in held if v)


@contextlib.contextmanager
def values_held(values):
    global _environment_values
    before = _environment_values
    register_values(values)
    added = _environment_values - before
    try:
        yield
    finally:
        _environment_values = before | (_environment_values - added)


def redact_values(text):
    for value in sorted(_environment_values, key=len, reverse=True):
        text = text.replace(value, REDACTED)
    return text


def redact_document(value):
    """Redact strings before JSON encoding so escapes cannot hide a value."""
    if isinstance(value, str):
        return redact_values(value)
    if isinstance(value, dict):
        return {key: redact_document(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_document(item) for item in value]
    return value


def safe_print(*args, **kwargs):
    builtins.print(*(redact_values(str(arg)) for arg in args), **kwargs)


def known_secrets(document, environ=None):
    environ = os.environ if environ is None else environ
    values = [str(v) for v in secret_leaves(document or {}).values()]
    values += [environ.get(name, "") for name in ENV_SECRETS]
    return frozenset(v for v in values if v.strip()) | _environment_values


def redact_prose(text, secrets=(), *, assignments=True):
    """Outbound payloads disable assignment matching to preserve unrelated links."""
    text = redact_values(text)
    for value in sorted(secrets, key=len, reverse=True):
        text = text.replace(value, REDACTED)
    return (PROSE_PAIR.sub(lambda m: m.group(1) + REDACTED, text)
            if assignments else text)


def outbound(text, secrets=()):
    return redact_prose(text, secrets, assignments=False)
