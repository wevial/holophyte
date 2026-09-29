"""Filing a validated story directory on a native board in one transaction."""
import re
from contextlib import closing
from pathlib import Path

import store
import store.board
import store.stories
import store.tickets
import story_template
import ticket_template

STORY_HEADER_RE = re.compile(r"^Story:[ \t]*(\S+)")
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
    witnesses = [{"key": witness.key, "criterion": witness.outcome,
                  "file": witness.file, "command": witness.command,
                  "source": (directory / story_template.WITNESSES
                             / witness.file).read_text()}
                 for witness in story.witnesses]
    store.stories.file_story(conn, _ticket_id(conn, project_id, parent),
                             witnesses, rows,
                             standing_orders=story.standing_orders)
    return answer


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
