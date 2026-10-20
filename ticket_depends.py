"""Find and rewrite a ticket body's `Depends on:` estimate line or `depends_on`."""
import json
import tomllib

import ticket_header
import ticket_template

ESTIMATE_SECTION = "Estimate & dependencies"


def estimate_line(lines):
    """The index of the estimate line `ticket_template.parse()` reads."""
    first = next((index for index, line in enumerate(lines)
                  if ticket_template.H1_RE.match(line)), -1)
    section = None
    for index in range(first + 1, len(lines)):
        line = lines[index]
        heading = ticket_template.H2_RE.match(line)
        if heading and section == ESTIMATE_SECTION:
            return None
        elif heading:
            section = heading.group(1).strip()
        elif (section == ESTIMATE_SECTION
              and ticket_template.ESTIMATE_RE.match(line.strip())):
            return index
    return None


def _header(lines):
    opening = next((index for index, line in enumerate(lines)
                    if line.rstrip("\r\n") == ticket_header.FENCE
                    or ticket_template.H1_RE.match(line)), None)
    if opening is None or lines[opening].rstrip("\r\n") != ticket_header.FENCE:
        return None
    return next(((opening, index) for index in range(opening + 1, len(lines))
                 if lines[index].rstrip("\r\n") == ticket_header.FENCE), None)


def _top_level_keys(lines, opening, closing):
    keys, index = [], opening + 1
    while index < closing and not lines[index].lstrip().startswith("["):
        if not lines[index].strip() or lines[index].lstrip().startswith("#"):
            index += 1
            continue
        last, parsed = next(((end, table) for end in range(index, closing)
                             if (table := _parsed(lines[index:end + 1]))),
                            (closing - 1, {}))
        keys.extend((name, index, last) for name in parsed)
        index = last + 1
    return keys


def _parsed(lines):
    try:
        return tomllib.loads("".join(lines))
    except tomllib.TOMLDecodeError:
        return None


def _rewrite_header(body, lines, bounds, rewrite):
    fields = ticket_header.split(body)[1][0]
    if fields is None:
        return body
    deps = fields.get("depends_on", [])
    rewritten = rewrite(deps)
    if not deps and not rewritten:
        return body
    opening = lines[bounds[0]]
    ending = opening[len(opening.rstrip("\r\n")):]
    keys = _top_level_keys(lines, *bounds)
    found = [(first, last + 1) for name, first, last in keys if name == "depends_on"]
    end = max((last + 1 for _, _, last in keys), default=bounds[0] + 1)
    first, after = found[0] if found else (end, end)
    lines[first:after] = [f"depends_on = {json.dumps(rewritten)}{ending}"]
    return "".join(lines)


def rewrite_depends(body, rewrite):
    """`body` with its estimate line's list `deps` made `rewrite(deps)`."""
    header = body.splitlines(keepends=True)
    bounds = _header(header)
    if bounds:
        return _rewrite_header(body, header, bounds, rewrite)
    lines = body.split("\n")
    plain = ticket_template.MD_LINK_RE.sub(r"\1", body).split("\n")
    index = estimate_line(plain)
    if index is None:
        return body
    line = plain[index]
    match = ticket_template.ESTIMATE_RE.match(line.strip())
    deps = ticket_template._deps(match.group(2))
    rewritten = rewrite(deps)
    if not deps and not rewritten:
        return body
    start = line.index(match.group(0))
    lines[index] = (line[:start] + match.group(0)[:match.start(2)]
                    + (", ".join(rewritten) or "none"))
    return "\n".join(lines)
