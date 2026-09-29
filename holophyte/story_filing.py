"""Filing a validated story directory on a native board in one transaction."""
import json
import re
import time
from contextlib import closing
from pathlib import Path

import store
import store.board
import store.stories
import store.tickets
import story_template
import ticket_template

STORY_HEADER_RE = re.compile(r"^Story:[ \t]*(\S+)")
TICKET_HEADER_RE = re.compile(r"^Ticket:[ \t]*(\S+)")
ESTIMATE_SECTION = "Estimate & dependencies"


class StoryRefused(ValueError):
    """The story was not filed and nothing was written; `lines` says why."""

    def __init__(self, lines):
        self.lines = list(lines)
        super().__init__(self.lines[0])


def story_directory(project, slug):
    return Path(project.holo_dir) / "stories" / slug


def file_story(board, project, slug, priority=None):
    """File story `slug` on the native `board`; answer each filed ticket as
    (identifier, title, role), the parent first, or raise `StoryRefused`."""
    directory = story_directory(project, slug)
    body_path = directory / story_template.BODY
    filed = _header(body_path)
    if filed is not None:
        raise StoryRefused([f"{body_path} is already filed as {filed}; "
                            "change a filed story with --update"])
    problems = ticket_template.blocking(
        story_template.validate_story(directory, repo=str(project.path)))
    if problems:
        raise StoryRefused(f"{directory}: {problem}" for problem in problems)
    text = body_path.read_text()
    children = _in_order(story_template.parse_children(directory))
    from holophyte.runs import open_store
    with closing(open_store(project)) as conn:
        project_id = store.tickets.ensure_project(conn, board.team,
                                                  project.path)
        try:
            with store.transaction(conn):
                answer = _file_rows(conn, project_id, board.key, directory,
                                    text, children, priority)
        except StoryRefused:
            raise
        except ValueError as refused:
            raise StoryRefused(getattr(refused, "problems",
                                       [str(refused)])) from None
    body_path.write_text(f"Story: {answer[0][0]}\n{text}")
    for child, (identifier, _title, _role) in zip(children, answer[1:]):
        path = directory / story_template.CHILDREN / f"{child.name}.md"
        path.write_text(f"Ticket: {identifier}\n{path.read_text()}")
    return answer


def _header(path):
    if not path.is_file():
        return None
    match = STORY_HEADER_RE.match(path.read_text())
    return match.group(1) if match else None


def _in_order(children):
    """`children` after the siblings they depend on, else in name order."""
    slugs = {child.slug for child in children}
    placed, ordered, waiting = set(), [], list(children)
    while waiting:
        child = next(child for child in waiting
                     if all(dep in placed for dep in child.depends_on
                            if dep in slugs))
        waiting.remove(child)
        placed.add(child.slug)
        ordered.append(child)
    return ordered


def _file_rows(conn, project_id, key, directory, text, children, priority):
    story = story_template.parse_story(text)
    parent = store.board.file_ticket(conn, project_id, key, text,
                                     column="backlog", priority=priority)
    answer = [(parent, ticket_template.parse(text).title, "story")]
    identifiers, rows = {}, []
    for child in children:
        _check_merged(conn, project_id, child, identifiers)
        body = story_template.HEADER_RE.sub(
            "", (directory / story_template.CHILDREN
                 / f"{child.name}.md").read_text(), count=1)
        identifier = store.board.file_ticket(
            conn, project_id, key, _resolve(body, identifiers),
            column="backlog", priority=priority)
        identifiers[child.slug] = identifier
        answer.append((identifier, child.ticket.title, child.role))
        rows.append((_ticket_id(conn, project_id, identifier), child.role,
                     child.witnesses))
    store.stories.file_story(conn, _ticket_id(conn, project_id, parent),
                             _witnesses(directory, story), rows,
                             standing_orders=story.standing_orders)
    return answer


def update_story(board, project, slug, identifier, revision, priority=None):
    directory = story_directory(project, slug)
    body_path = directory / story_template.BODY
    filed = _header(body_path)
    if filed != identifier:
        raise StoryRefused([f"{body_path} is filed as {filed}, not "
                            f"{identifier}" if filed else f"{body_path} is not "
                            "filed; file it with --file-story SLUG"])
    headers = {child.name: _child_header(directory, child)
               for child in story_template.parse_children(directory)}
    from holophyte.runs import open_store
    with closing(open_store(project)) as conn:
        project_id = store.tickets.ensure_project(conn, board.team,
                                                  project.path)
        _check_filed(conn, project_id, identifier, revision, directory,
                     headers)
        problems = ticket_template.blocking(
            story_template.validate_story(directory, repo=str(project.path)))
        if problems:
            raise StoryRefused(f"{directory}: {problem}"
                               for problem in problems)
        text = body_path.read_text().partition("\n")[2]
        children = _in_order(story_template.parse_children(directory))
        try:
            with store.transaction(conn):
                parent_id = _check_filed(conn, project_id, identifier,
                                         revision, directory, headers)
                lines, filed = _update_rows(
                    conn, project_id, board.key, directory, parent_id,
                    identifier, revision, text, children, headers, priority)
        except StoryRefused:
            raise
        except ValueError as refused:
            raise StoryRefused(getattr(refused, "problems",
                                       [str(refused)])) from None
    for name, new in filed:
        path = directory / story_template.CHILDREN / f"{name}.md"
        path.write_text(f"Ticket: {new}\n{path.read_text()}")
    return lines


def _child_header(directory, child):
    match = TICKET_HEADER_RE.match(
        (directory / story_template.CHILDREN / f"{child.name}.md").read_text())
    return match.group(1) if match else None


def _update_rows(conn, project_id, key, directory, parent_id, identifier,
                 revision, text, children, headers, priority):
    before = _plan_state(conn, parent_id)
    lines, filed, rows = [], [], []
    now = int(time.time() * 1000)
    if before[0] != text:
        store.board.edit_ticket(conn, project_id, identifier, text, revision,
                                now=now)
    identifiers = {child.slug: headers[child.name] for child in children
                   if headers[child.name]}
    known = {child.slug for child in children} | set(identifiers.values())
    for child in children:
        _check_merged(conn, project_id, child, known)
        body = _resolve(story_template.HEADER_RE.sub(
            "", (directory / story_template.CHILDREN
                 / f"{child.name}.md").read_text(), count=1), identifiers)
        header = headers[child.name]
        if header is None:
            header = store.board.file_ticket(conn, project_id, key, body,
                                             column="backlog",
                                             priority=priority)
            identifiers[child.slug] = header
            filed.append((child.name, header))
            lines.append(f"filed {header}: {child.ticket.title} "
                         f"({child.role}, Backlog)")
        else:
            (stored, current) = conn.execute(
                "SELECT body, revision FROM tickets WHERE projectId = ?"
                " AND linearIdentifier = ?", (project_id, header)).fetchone()
            if stored != body:
                new = store.board.edit_ticket(conn, project_id, header, body,
                                              current)
                lines.append(f"updated {header} (revision {new})")
        rows.append((_ticket_id(conn, project_id, header), child.role,
                     child.witnesses))
    story = story_template.parse_story(text)
    witnesses = _witnesses(directory, story)
    after = (text, *_rows_state(conn, witnesses, rows, story.standing_orders))
    state = store.stories.story(conn, parent_id).state
    if after != before:
        state = store.stories.replan_story(conn, parent_id, witnesses, rows,
                                           story.standing_orders, now=now)
    (current,) = conn.execute("SELECT revision FROM tickets WHERE id = ?",
                              (parent_id,)).fetchone()
    lines.append(f"story {identifier} is {state} at revision {current}")
    return lines, filed


def _check_filed(conn, project_id, identifier, revision, directory, headers):
    row = conn.execute("SELECT id, revision FROM tickets WHERE projectId = ?"
                       " AND linearIdentifier = ?",
                       (project_id, identifier)).fetchone()
    stored = row and store.stories.story(conn, row[0])
    if not stored or stored.ticketId != row[0]:
        raise StoryRefused([f"{identifier} is not a story's parent in this "
                            "project"])
    if row[1] != revision:
        raise StoryRefused([f"{identifier} is at revision {row[1]}, not "
                            f"{revision}; nothing changed"])
    named = [header for header in headers.values() if header]
    children = {child for (child,) in conn.execute(
        "SELECT linearIdentifier FROM tickets WHERE id IN (SELECT ticketId"
        " FROM storyChildren WHERE storyId = ?)", (row[0],))}
    for header in named:
        if header not in children:
            raise StoryRefused([f"a child file names {header}, which is not "
                                f"a child of story {identifier}"])
        if named.count(header) > 1:
            raise StoryRefused([f"two child files name {header}"])
    missing = sorted(children - set(named))
    if missing:
        raise StoryRefused([f"child {missing[0]} of story {identifier} has no "
                            f"file in {directory / story_template.CHILDREN}; "
                            "cancel a child with --cancel"])
    return row[0]


def _plan_state(conn, parent_id):
    stored = store.stories.story(conn, parent_id)
    (body,) = conn.execute("SELECT body FROM tickets WHERE id = ?",
                           (parent_id,)).fetchone()
    keys = {}
    for child in stored.children:
        keys.setdefault(child.ticketId, (child.role, []))[1].extend(
            [child.witnessKey] if child.witnessKey else [])
    witnesses = [witness._asdict() for witness in stored.witnesses]
    return (body, *_rows_state(
        conn, witnesses, [(ticket_id, role, child_keys)
                          for ticket_id, (role, child_keys) in keys.items()],
        stored.standingOrders))


def _rows_state(conn, witnesses, rows, standing_orders):
    return (sorted(tuple(witness[field] for field in
                         store.stories.WITNESS_FIELDS)
                   for witness in witnesses),
            {ticket_id: (role, sorted(keys)) for ticket_id, role, keys in rows},
            {ticket_id: json.loads(conn.execute(
                "SELECT dependsOn FROM tickets WHERE id = ?",
                (ticket_id,)).fetchone()[0]) for ticket_id, _, _ in rows},
            list(standing_orders))


def _witnesses(directory, story):
    return [{"key": witness.key, "criterion": witness.outcome,
             "file": witness.file, "command": witness.command,
             "source": (directory / story_template.WITNESSES
                        / witness.file).read_text()}
            for witness in story.witnesses]


def _check_merged(conn, project_id, child, siblings):
    for dep in child.depends_on:
        if dep in siblings or not ticket_template.LINEAR_ID_RE.match(dep):
            continue
        row = conn.execute("SELECT status FROM tickets WHERE projectId = ?"
                           " AND linearIdentifier = ?",
                           (project_id, dep)).fetchone()
        if row is None or row[0] != "merged":
            state = "not in this project" if row is None else row[0]
            raise StoryRefused([f"child {child.name} depends on {dep}, which "
                                f"is {state}, not merged"])


def _resolve(body, identifiers):
    """`body` with its `Depends on:` sibling slugs made their identifiers."""
    lines = body.split("\n")
    plain = ticket_template.MD_LINK_RE.sub(r"\1", body).split("\n")
    index = _estimate_line(plain)
    if index is None:
        return body
    line = plain[index]
    match = ticket_template.ESTIMATE_RE.match(line.strip())
    deps = [identifiers.get(dep, dep)
            for dep in ticket_template._deps(match.group(2))]
    if deps:
        start = line.index(match.group(0))
        lines[index] = (line[:start] + match.group(0)[:match.start(2)]
                        + ", ".join(deps))
    return "\n".join(lines)


def _estimate_line(lines):
    """The index of the estimate line `ticket_template.parse()` reads."""
    titled, section = False, None
    for index, line in enumerate(lines):
        heading = ticket_template.H2_RE.match(line)
        if not titled:
            titled = bool(ticket_template.H1_RE.match(line))
        elif heading and section == ESTIMATE_SECTION:
            return None
        elif heading:
            section = heading.group(1).strip()
        elif (section == ESTIMATE_SECTION
              and ticket_template.ESTIMATE_RE.match(line.strip())):
            return index
    return None


def _ticket_id(conn, project_id, identifier):
    (ticket_id,) = conn.execute(
        "SELECT id FROM tickets WHERE projectId = ? AND linearIdentifier = ?",
        (project_id, identifier)).fetchone()
    return ticket_id
