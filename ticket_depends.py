"""Find and rewrite the `Depends on:` list of a ticket body's estimate line."""
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


def rewrite_depends(body, rewrite):
    """`body` with its estimate line's list `deps` made `rewrite(deps)`."""
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
