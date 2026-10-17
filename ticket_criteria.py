import re

import ticket_template

_ID = re.compile(r"^\s*\*\*C([1-9][0-9]*)\*\*\s*")
_SPAN = re.compile(r"`([^`\n]+)`")
_CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`).*?(?<!`)\1(?!`)", re.DOTALL)
_EDIT = re.compile(
    r"^\s*(?:(REPLACE)\s+C([1-9][0-9]*)\s*:\s*(\S.*?)|"
    r"(DROP)\s+C([1-9][0-9]*)(?:\s*:\s*(.*?))?|"
    r"(ADD)\s*:\s*(\S.*?))\s*$", re.IGNORECASE)


def amendment_edits(note):
    edits, other = [], []
    for line in note.splitlines():
        match = _EDIT.fullmatch(line)
        if match is None:
            other.append(line)
        elif match[1]:
            edits.append(("REPLACE", int(match[2]), match[3]))
        elif match[4]:
            edits.append(("DROP", int(match[5]), match[6] or ""))
        else:
            edits.append(("ADD", None, match[8]))
    return edits, "\n".join(other) if edits else note


def split_id(text):
    match = _ID.match(text)
    if match:
        return int(match.group(1)), text[match.end():]
    return None, text


def witnesses(texts, commands):
    from holophyte.review.witness_precheck import named_files

    lines = {line.strip() for command in commands
             for line in command.splitlines() if line.strip()}
    result = []
    for text, files in zip(texts, named_files(texts)):
        if files:
            result.append(("test", ",".join(files)))
        elif command := next((span for span in _SPAN.findall(text)
                              if span in lines), None):
            result.append(("verify", command))
        else:
            result.append(("reviewer_read", None))
    return result


def witness_problems(ticket):
    texts = ticket.acceptance + ticket.acceptance_done + ticket.acceptance_other
    kinds = witnesses(texts, ticket.verify_commands)
    return [f"criterion {number} reads as a post-merge or operator witness; "
            "name a test file or verify command"
            for number, (text, (kind, _target)) in enumerate(zip(texts, kinds), 1)
            if kind == "reviewer_read" and any(
                phrase in _CODE_SPAN.sub(" ", text).lower()
                for phrase in ticket_template.OPERATOR_WITNESS_PHRASES)]
