"""Filing a validated story directory on a native or store-mode Linear board."""
import json
import re
import shutil
import tempfile
import time
from contextlib import closing
from pathlib import Path

import store
import store.board
import store.stories
import store.tickets
import story_template
import ticket_template
from holophyte.board.projection import mirror_task
from holophyte.config.config_tables import board_config
from provider import FiledWithoutBlockers

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
    """File story `slug` on `board`; answer each filed ticket as (identifier,
    title, role), the parent first, or raise `StoryRefused`."""
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
    from holophyte.loop.runs import open_store
    with closing(open_store(project)) as conn:
        project_id = store.tickets.ensure_project(conn, board.team,
                                                  project.path)
        if getattr(board, "native", False):
            answer = _in_transaction(conn, _file_rows, conn, project_id,
                                     board.key, directory, text, children,
                                     priority)
        else:
            answer = _file_on_board(board, conn, project, project_id,
                                    directory, text, children, priority)
    body_path.write_text(f"Story: {answer[0][0]}\n{text}")
    for child, (identifier, _title, _role) in zip(children, answer[1:]):
        path = directory / story_template.CHILDREN / f"{child.name}.md"
        path.write_text(f"Ticket: {identifier}\n{path.read_text()}")
    return answer


def _in_transaction(conn, write, *args):
    try:
        with store.transaction(conn):
            return write(*args)
    except StoryRefused:
        raise
    except ValueError as refused:
        raise StoryRefused(getattr(refused, "problems",
                                   [str(refused)])) from None


def _file_on_board(board, conn, project, project_id, directory, text,
                   children, priority):
    slugs = {child.slug for child in children}
    issue_ids = {}
    for child in children:
        issue_ids.update(_merged_issue_ids(conn, project_id, child, slugs))
    label = board_config(project).label
    created = []
    try:
        parent = _create(board, created, text, priority)
        tasks = [(board.fetch_task(parent), None)]
        identifiers = {}
        for child in children:
            body = _resolve(_child_body(directory, child), identifiers)
            identifier = _create(board, created, body, priority,
                                 parent=tasks[0][0]["issue_id"])
            task = board.fetch_task(identifier)
            if label is not None:
                board.label_issue(task["issue_id"], label)
                task = board.fetch_task(identifier)
            identifiers[child.slug] = identifier
            issue_ids[child.slug] = task["issue_id"]
            tasks.append((task, [issue_ids[dep] for dep in child.depends_on
                                 if dep in issue_ids]))
    except Exception as refused:
        raise StoryRefused([f"the board refused the story: {refused}",
                            _to_cancel(created)]) from None
    try:
        return _in_transaction(conn, _mirror_rows, conn, project_id,
                               directory, text, children, tasks)
    except Exception as refused:
        lines = getattr(refused, "lines",
                        [f"the store refused the story: {refused}"])
        raise StoryRefused([*lines, _to_cancel(created)]) from refused


def _to_cancel(created):
    return ("already created, to cancel on the board: "
            + (", ".join(created) or "none"))


def _create(board, created, body, priority, parent=None):
    ticket = ticket_template.parse(body)
    try:
        identifier = board.file(ticket.title, body, ticket.estimate_min,
                                "Backlog", priority=priority,
                                blockers=ticket.depends_on or [],
                                parent=parent)
    except FiledWithoutBlockers as refused:
        created.append(refused.identifier)
        raise
    created.append(identifier)
    return identifier


def _mirror_rows(conn, project_id, directory, text, children, tasks):
    (parent_task, _), *child_tasks = tasks
    parent = mirror_task(conn, project_id, parent_task, specced=False)
    answer = [(parent_task["id"], ticket_template.parse(text).title, "story")]
    rows = []
    for child, (task, depends_on) in zip(children, child_tasks):
        ticket_id = mirror_task(conn, project_id, task, depends_on=depends_on)
        answer.append((task["id"], child.ticket.title, child.role))
        rows.append((ticket_id, child.role, child.witnesses))
    story = story_template.parse_story(text)
    store.stories.file_story(conn, parent, _witnesses(directory, story), rows,
                             standing_orders=story.standing_orders)
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
        _merged_issue_ids(conn, project_id, child, identifiers)
        body = _child_body(directory, child)
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


def _child_body(directory, child):
    return story_template.HEADER_RE.sub(
        "", (directory / story_template.CHILDREN
             / f"{child.name}.md").read_text(), count=1)


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
    from holophyte.loop.runs import open_store
    with closing(open_store(project)) as conn:
        project_id = store.tickets.ensure_project(conn, board.team,
                                                  project.path)
        native = getattr(board, "native", False)
        _check_filed(conn, project_id, identifier, revision, directory,
                     headers, native)
        children = _validated_children(project, directory, headers)
        text = body_path.read_text().partition("\n")[2]
        if native:
            lines, filed = _in_transaction(
                conn, _update_rows, conn, project_id, board.key, directory,
                identifier, revision, text, children, headers, priority)
        else:
            lines, filed = _update_on_board(
                board, conn, project, project_id, directory, identifier,
                revision, text, children, headers, priority)
    for name, new in filed:
        path = directory / story_template.CHILDREN / f"{name}.md"
        path.write_text(f"Ticket: {new}\n{path.read_text()}")
    return lines


def _update_on_board(board, conn, project, project_id, directory, identifier,
                     revision, text, children, headers, priority):
    slugs = {child.slug for child in children}
    issue_ids, stored = {}, {}
    for child in children:
        issue_ids.update(_merged_issue_ids(conn, project_id, child, slugs))
    for name in [identifier, *filter(None, headers.values())]:
        stored[name] = conn.execute(
            "SELECT body, linearIssueId FROM tickets WHERE projectId = ?"
            " AND linearIdentifier = ?", (project_id, name)).fetchone()
    for child in children:
        if headers[child.name]:
            issue_ids[child.slug] = stored[headers[child.name]][1]
    written = {"updated": [], "created": []}
    try:
        touched, kept = _board_writes(
            board, project, directory, identifier, text, children, headers,
            priority, stored, issue_ids, written)
    except Exception as refused:
        raise StoryRefused([f"the board refused the update: {refused}",
                            *_written(written)]) from None
    try:
        return _in_transaction(
            conn, _mirror_update, conn, project_id, directory, identifier,
            revision, text, children, headers, touched, kept)
    except Exception as refused:
        lines = getattr(refused, "lines",
                        [f"the store refused the update: {refused}"])
        raise StoryRefused([*lines, *_written(written)]) from refused


def _written(written):
    return ["already updated on the board: "
            + (", ".join(written["updated"]) or "none"),
            _to_cancel(written["created"])]


def _board_writes(board, project, directory, identifier, text, children,
                  headers, priority, stored, issue_ids, written):
    touched, kept = {}, []
    if stored[identifier][0] != text:
        touched[None] = _board_update(board, identifier, text, written, kept)
    label = board_config(project).label
    identifiers = {child.slug: headers[child.name] for child in children
                   if headers[child.name]}
    for child in children:
        body = _resolve(_child_body(directory, child), identifiers)
        header = headers[child.name]
        if header is None:
            header = _create(board, written["created"], body, priority,
                             parent=stored[identifier][1])
            task = board.fetch_task(header)
            if label is not None:
                board.label_issue(task["issue_id"], label)
                task = board.fetch_task(header)
            identifiers[child.slug] = header
            issue_ids[child.slug] = task["issue_id"]
        elif stored[header][0] != body:
            task = _board_update(board, header, body, written, kept)
        else:
            continue
        touched[child.name] = (task, [issue_ids[dep] for dep in
                                      child.depends_on if dep in issue_ids])
    return touched, kept


def _board_update(board, identifier, body, written, kept):
    ticket = ticket_template.parse(body)
    _added, extra = board.update(identifier, ticket.title, body,
                                 ticket.estimate_min,
                                 blockers=ticket.depends_on or [])
    written["updated"].append(identifier)
    kept.extend(f"{blocker} still blocks {identifier} on the board; remove "
                "that relation on Linear" for blocker in extra)
    return board.fetch_task(identifier)


def _mirror_update(conn, project_id, directory, identifier, revision, text,
                   children, headers, touched, kept):
    parent_id = _check_filed(conn, project_id, identifier, revision,
                             directory, headers, native=False)
    before = _plan_state(conn, parent_id)
    if None in touched:
        mirror_task(conn, project_id, touched[None], specced=False)
    lines, filed, rows = [], [], []
    for child in children:
        if child.name in touched:
            task, depends_on = touched[child.name]
            ticket_id = mirror_task(conn, project_id, task,
                                    depends_on=depends_on)
        else:
            ticket_id = _ticket_id(conn, project_id, headers[child.name])
        if child.name in touched and headers[child.name] is None:
            filed.append((child.name, task["id"]))
            lines.append(f"filed {task['id']}: {child.ticket.title} "
                         f"({child.role}, Backlog)")
        elif child.name in touched:
            (new,) = conn.execute("SELECT revision FROM tickets WHERE id = ?",
                                  (ticket_id,)).fetchone()
            lines.append(f"updated {task['id']} (revision {new})")
        rows.append((ticket_id, child.role, child.witnesses))
    lines.extend(kept)
    lines.append(_replan(conn, parent_id, identifier, directory, text, rows,
                         before, int(time.time() * 1000)))
    return lines, filed


def _validated_children(project, directory, headers):
    slugs = {headers[child.name]: child.slug
             for child in story_template.parse_children(directory)
             if headers[child.name]}
    with tempfile.TemporaryDirectory() as scratch:
        plan = Path(scratch) / directory.name
        shutil.copytree(directory, plan)
        for path in (plan / story_template.CHILDREN).glob("*.md"):
            path.write_text(_resolve(path.read_text(), slugs))
        problems = ticket_template.blocking(
            story_template.validate_story(plan, repo=str(project.path)))
        if problems:
            raise StoryRefused(
                f"{directory}: {problem.replace(str(plan), str(directory))}"
                for problem in problems)
        return _in_order(story_template.parse_children(plan))


def _child_header(directory, child):
    match = TICKET_HEADER_RE.match(
        (directory / story_template.CHILDREN / f"{child.name}.md").read_text())
    return match.group(1) if match else None


def _update_rows(conn, project_id, key, directory, identifier, revision,
                 text, children, headers, priority):
    parent_id = _check_filed(conn, project_id, identifier, revision,
                             directory, headers)
    before = _plan_state(conn, parent_id)
    lines, filed, rows = [], [], []
    now = int(time.time() * 1000)
    body_revised = before[0] != text
    if body_revised:
        store.board.edit_ticket(conn, project_id, identifier, text, revision,
                                now=now)
    identifiers = {child.slug: headers[child.name] for child in children
                   if headers[child.name]}
    slugs = {child.slug for child in children}
    for child in children:
        _merged_issue_ids(conn, project_id, child, slugs)
        body = _resolve(_child_body(directory, child), identifiers)
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
    lines.append(_replan(conn, parent_id, identifier, directory, text, rows,
                         before, now))
    return lines, filed


def _replan(conn, parent_id, identifier, directory, text, rows, before, now):
    story = story_template.parse_story(text)
    witnesses = _witnesses(directory, story)
    after = (text, *_rows_state(conn, witnesses, rows, story.standing_orders))
    state = store.stories.story(conn, parent_id).state
    if after != before:
        state = store.stories.replan_story(conn, parent_id, witnesses, rows,
                                           story.standing_orders, now=now,
                                           body_revised=before[0] != text)
    (current,) = conn.execute("SELECT revision FROM tickets WHERE id = ?",
                              (parent_id,)).fetchone()
    return f"story {identifier} is {state} at revision {current}"


def _check_filed(conn, project_id, identifier, revision, directory, headers,
                 native=True):
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
                            + ("cancel a child with --cancel" if native else
                               "cancel the child on Linear and keep its "
                               "file")])
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


def _merged_issue_ids(conn, project_id, child, siblings):
    merged = {}
    for dep in child.depends_on:
        if dep in siblings or not ticket_template.LINEAR_ID_RE.match(dep):
            continue
        row = conn.execute("SELECT status, linearIssueId FROM tickets"
                           " WHERE projectId = ? AND linearIdentifier = ?",
                           (project_id, dep)).fetchone()
        if row is None or row[0] != "merged":
            state = "not in this project" if row is None else row[0]
            raise StoryRefused([f"child {child.name} depends on {dep}, which "
                                f"is {state}, not merged"])
        merged[dep] = row[1]
    return merged


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
