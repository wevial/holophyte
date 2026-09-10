"""Secret values in a `config.toml` text: found, redacted, put back.

`GET /config` (KO-356) shows the file to a bearer-holding client with every
secret value replaced by `REDACTED`, and `PUT /config` puts the current
value back wherever the client sends the placeholder. A secret is the
value of any key whose name ends in `token` or `key` -- `api_key`,
`token`, `"api key"` -- wherever the document puts it: a bare, quoted or
dotted key under a `[table]` or `[[array]]` header, or a pair inside an
inline table; `token_file`, a path, is not one.

The text is walked as TOML syntax, not as lines, so a value is replaced
whole whatever its shape: a basic or literal string, a multi-line string,
a number, an array, an inline table. Comments beside a value stay, since
only the value's span is touched. `tomllib` parses, so it cannot say
where a value sits in the text; `spans()` is the small scanner that can,
and the parsed document is the oracle `redact()` checks its work against:
a parsable text whose redaction still shows a secret is refused rather
than served.
"""

from __future__ import annotations

import tomllib

REDACTED = "[redacted]"
# A key is a secret's when its name ends this way; matched on the key's
# last segment (`linear.api_key` is `api_key`), so a path like
# `token_file` is not one.
SECRET_SUFFIXES = ("token", "key")
BARE_KEY = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                     "0123456789_-")
WHITESPACE = " \t"


class RedactionError(Exception):
    """`redact()` could not vouch for its output: a secret the parsed
    document holds is still readable in the redacted text."""


class Scan(Exception):
    """The scanner met text it cannot walk; carries the offset."""


def is_secret(key):
    return key.endswith(SECRET_SUFFIXES)


def spans(text):
    """`[(path, start, end)]` for every `key = value` pair of the TOML
    `text` whose key's last segment `is_secret()`, `path` the tuple of key
    segments from the enclosing header down (`("linear", "api_key")`),
    `text[start:end]` the value as written. Pairs of an inline table under
    a secret key are not listed separately: the table is the value.

    Text the scanner cannot walk -- a header without its `]`, a string
    without its closing quote -- ends the walk at that point, so a
    malformed file yields the pairs before the fault; `redact()` says
    whether that was enough."""
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
            table, pos = _key(text, pos)
            pos = _skip_ws(text, pos)
            close = "]]" if double else "]"
            if not text.startswith(close, pos):
                raise Scan(pos)
            pos = _end_of_line(text, pos + len(close))
        else:
            pos = _pair(text, pos, table, found)
            pos = _end_of_line(text, pos)
    return pos


def _pair(text, pos, prefix, found):
    """One `key = value` starting at `pos`; the offset after the value."""
    keys, pos = _key(text, pos)
    pos = _skip_ws(text, pos)
    if pos >= len(text) or text[pos] != "=":
        raise Scan(pos)
    pos = _skip_ws(text, pos + 1)
    path = prefix + keys
    start = pos
    secret = is_secret(path[-1])
    pos = _value(text, pos, path, None if secret else found)
    if secret:
        found.append((path, start, pos))
    return pos


def _key(text, pos):
    """A bare, quoted or dotted key at `pos`: `(segments, offset after)`."""
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
    """The value starting at `pos`; the offset after it. Pairs of an
    inline table are recorded into `found` under `path` when it is not
    None (the enclosing key was not itself a secret)."""
    n = len(text)
    if pos >= n:
        raise Scan(pos)
    ch = text[pos]
    if ch in "\"'":
        return _string(text, pos)
    if ch == "[":
        return _array(text, pos + 1, path)
    if ch == "{":
        return _inline_table(text, pos + 1, path, found)
    # A bare scalar -- number, boolean, date-time (which may hold a space)
    # -- runs to the separator that ends it, trailing blanks dropped.
    end = pos
    while end < n and text[end] not in ",]}#\r\n":
        end += 1
    while end > pos and text[end - 1] in WHITESPACE:
        end -= 1
    if end == pos:
        raise Scan(pos)
    return end


def _array(text, pos, path):
    """The rest of an array whose `[` sits before `pos`: the offset after
    its `]`. Newlines and comments may sit between its values."""
    n = len(text)
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
        pos = _value(text, pos, path, None)


def _inline_table(text, pos, path, found):
    """The rest of an inline table whose `{` sits before `pos`: the offset
    after its `}`, its pairs recorded under `path` when `found` is given."""
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
    """A TOML string starting at `text[pos]`; the offset after it."""
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
    """The one value of a `k = ...` TOML fragment, None when it is not TOML."""
    try:
        return tomllib.loads(fragment)["k"]
    except (tomllib.TOMLDecodeError, KeyError):
        return None


def _rewrite(text, replacements):
    """`text` with each `(start, end, new)` span replaced, spans disjoint."""
    out = []
    at = 0
    for start, end, new in sorted(replacements):
        out.append(text[at:start])
        out.append(new)
        at = end
    out.append(text[at:])
    return "".join(out)


def secret_leaves(document):
    """`{path: value}` for every leaf of the parsed `document` whose key
    `is_secret()`, arrays of tables indexed into the path."""
    leaves = {}

    def walk(node, prefix):
        for key, value in node.items():
            path = prefix + (key,)
            if isinstance(value, dict):
                walk(value, path)
            elif isinstance(value, list) and value and \
                    all(isinstance(item, dict) for item in value):
                for index, item in enumerate(value):
                    walk(item, path + (index,))
            elif is_secret(key):
                leaves[path] = value
            # A secret-named key holding a table of pairs is the table's
            # pairs' business; `spans()` lists the whole table, so the
            # parsed check below finds the placeholder in their place.

    walk(document, ())
    return leaves


def redact(text):
    """`text` with every secret value replaced by a quoted `REDACTED`.

    The rewrite is checked against the parsed document when `text` parses:
    every secret leaf of the original must read `REDACTED` in the result,
    or `RedactionError` says which does not -- the scanner and `tomllib`
    disagreed about the text, and the text is not served. A `text` that
    does not parse has no oracle and gets the scanner's best walk: the
    loop would refuse the file too, so this is the shell's case."""
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
    for path, value in secret_leaves(original).items():
        if result.get(path) != REDACTED and not (
                isinstance(value, dict) and _all_redacted(value)):
            raise RedactionError(
                f"{describe(path)}: the value would still be readable;"
                " not served")
    return redacted


def _all_redacted(value):
    return all(v == REDACTED or (isinstance(v, dict) and _all_redacted(v))
               for v in value.values())


def describe(path):
    """`[table] key` for a path, `key` at the top, indices as `[n]`."""
    parts = [str(p) for p in path if not isinstance(p, int)]
    if len(parts) == 1:
        return parts[0]
    return f"[{'.'.join(parts[:-1])}] {parts[-1]}"


def restore(text, current):
    """`text` with every `REDACTED` value put back from `current`, the
    file's present text: what a round trip through the console page sends
    is the redacted text with edits, and a secret it never saw must come
    back as it was, not as the placeholder. A placeholder is any TOML
    string whose value is `REDACTED`, whatever its quoting, a comment
    beside it left alone. Under one path the n-th placeholder takes the
    n-th current value (`[[array]]` tables repeat a path). ValueError
    names a redacted key the current file has no value for."""
    held = {}
    for path, start, end in spans(current):
        held.setdefault(path, []).append(current[start:end])
    missing = []
    replacements = []
    for path, start, end in spans(text):
        if _parse("k = " + text[start:end]) != REDACTED:
            continue
        values = held.get(path)
        if not values:
            missing.append(describe(path))
            continue
        replacements.append((start, end, values.pop(0)))
    if missing:
        raise ValueError(
            f"{', '.join(missing)}: {REDACTED} stands for a value the current"
            " file does not hold; write the value")
    return _rewrite(text, replacements)
