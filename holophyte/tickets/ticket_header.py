"""The `+++` TOML header a ticket can open with: its typed fields, each checked."""
import datetime
import json
import re
import tomllib

FENCE = "+++"
FIELDS = ("ui_change", "blast_radius", "estimate", "depends_on", "orchestration",
          "implementer", "mockup", "evidence")
MOCKUP_FIELDS = ("url", "approved", "note", "boards")
BOARD_FIELDS = ("url", "caption")
EVIDENCE_FIELDS = ("file", "state")
MAX_EVIDENCE = 6
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
CAPTION_RE = re.compile(r"[^\[\]\n]*")
SLUG = r"[a-z0-9][a-z0-9-]*"
MOCKUP_MEDIA_RE = re.compile(r"https?://[^/?#\s]+/[^?#\s]*\.(png|zip)")
MOCKUP_MEDIA_KINDS = {"png": "snapshot", "zip": "bundle"}
BOLD_FIELDS = {"UI change": "ui_change", "Blast radius": "blast_radius"}
LISTED = r"(?:[-*+]\s+)?"
APPROVAL_RE = re.compile(rf"{LISTED}Approved (\d{{4}}-\d{{2}}-\d{{2}}):\s*(.*)")
LINK_LINE_RE = re.compile(rf"{LISTED}!?\[([^\[\]\n]*)\]\(<?([^()\s<>]*)>?\)")
BARE_LINE_RE = re.compile(rf"{LISTED}(\S+)")
EVIDENCE_FILE_RE = re.compile(rf"`(\d{{2}}-{SLUG}\.(?:png|webm))`:\s*(.*)")
SLUG_WORDS = 3
TABLES = ("mockup", "boards", "evidence")
BESIDE = (
    (lambda t, body: _field_line(body, "UI change"),
     "'**UI change:**' line beside a header: give ui_change in the header only"),
    (lambda t, body: _field_line(body, "Blast radius"),
     "'**Blast radius:**' line beside a header: give blast_radius in the header only"),
    (lambda t, body: "Estimate & dependencies" in t.order,
     "'## Estimate & dependencies' beside a header: give estimate and depends_on "
     "in the header only"),
    (lambda t, body: "Mock-up" in t.order,
     "'## Mock-up' beside a header: give [mockup] in the header only"),
    (lambda t, body: "Evidence" in t.order,
     "'## Evidence' beside a header: give [[evidence]] entries in the header only"),
)


def split(text):
    """`text` without its header, and None or the header's (fields, problem)."""
    from holophyte.tickets import ticket_template as tt
    lines = text.splitlines(keepends=True)
    opening = next((index for index, line in enumerate(lines)
                    if line.rstrip("\r\n") == FENCE or tt.H1_RE.match(line)), None)
    if opening is None or lines[opening].rstrip("\r\n") != FENCE:
        return text, None
    closing = next((index for index in range(opening + 1, len(lines))
                    if lines[index].rstrip("\r\n") == FENCE), None)
    if closing is None:
        return ("".join(lines[:opening] + lines[opening + 1:]),
                (None, "header has no closing '+++' line"))
    rest = "".join(lines[:opening] + lines[closing + 1:])
    try:
        return rest, (tomllib.loads("".join(lines[opening + 1:closing])), None)
    except tomllib.TOMLDecodeError as error:
        return rest, (None, f"header is not valid TOML: {error}")


def apply(t, header, body):
    if header is None:
        return
    fields, problem = header
    t.header_problems = [message for present, message in BESIDE if present(t, body)]
    if problem:
        t.header_problems.append(problem)
        return
    values, problems = _checked(fields)
    t.header_problems += problems
    t.ui_change = values.get("ui_change")
    t.blast_radius = values.get("blast_radius")
    t.estimate_min = values.get("estimate")
    t.depends_on = values.get("depends_on", [])
    t.orchestration = values.get("orchestration")
    t.implementer = values.get("implementer")
    entries = values.get("evidence", [])
    t.evidence_states = [entry["state"] for entry in entries]
    t.evidence_files = [entry["file"] for entry in entries]
    if "mockup" in values:
        _set_mockup(t, values["mockup"])


def _field_line(body, key):
    from holophyte.tickets import ticket_template as tt
    in_fence = False
    for line in body.splitlines():
        in_fence ^= bool(tt.FENCE_RE.match(line.strip()))
        found = not in_fence and tt.BOLD_KEY_RE.match(line.strip())
        if found and found.group(1) == key:
            return True
    return False


def mockup_media_kind(url):
    media = MOCKUP_MEDIA_RE.fullmatch(url)
    return MOCKUP_MEDIA_KINDS[media.group(1)] if media else None


def _checked(fields):
    """The fields that pass their checks, and a problem for each that does not."""
    from holophyte.tickets import ticket_template as tt
    checks = {
        "ui_change": lambda v: _choice("ui_change", v, tt.UI_CHANGE_VALUES[::-1]),
        "blast_radius": lambda v: _choice("blast_radius", v,
                                          tt.BLAST_RADIUS_VALUES),
        "estimate": _estimate_problems,
        "depends_on": _depends_problems,
        "orchestration": lambda v: _choice("orchestration", v,
                                           tt.ORCHESTRATION_MODES),
        "implementer": _implementer_problems,
        "mockup": _mockup_problems,
        "evidence": _evidence_problems,
    }
    values, problems = {}, _unknown("", fields, FIELDS)
    if "estimate" not in fields:
        problems.append("header estimate is missing: give whole minutes from "
                        f"1 to {tt.MAX_ESTIMATE_MIN}")
    for name, check in checks.items():
        if name in fields:
            found = check(fields[name])
            problems += found
            if not found:
                values[name] = fields[name]
    return values, problems


def _show(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def _unknown(prefix, table, known):
    return [f'header field "{prefix}{key}" is not a ticket field'
            for key in table if key not in known]


def _choice(name, value, choices):
    if value in choices:
        return []
    listed = ", ".join(f'"{choice}"' for choice in choices[:-1])
    return [f'header {name} must be {listed} or "{choices[-1]}", '
            f"not {_show(value)}"]


def _estimate_problems(value):
    from holophyte.tickets import ticket_template as tt
    if type(value) is int and 1 <= value <= tt.MAX_ESTIMATE_MIN:
        return []
    return [f"header estimate must be whole minutes from 1 to "
            f"{tt.MAX_ESTIMATE_MIN}, not {_show(value)}"]


def _depends_problems(value):
    from holophyte.tickets import ticket_template as tt
    if isinstance(value, list) and all(
            isinstance(key, str) and tt.LINEAR_ID_RE.fullmatch(key)
            for key in value):
        return []
    return [f"header depends_on must be a list of ticket keys, not {_show(value)}"]


def _implementer_problems(value):
    from holophyte.tickets import ticket_template as tt
    if isinstance(value, str) and tt.IMPLEMENTER_NAME_RE.fullmatch(value):
        return []
    return ["header implementer must name one profile of letters, digits, _ "
            f"and -, not {_show(value)}"]


def _approved(value):
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, str) and DATE_RE.fullmatch(value):
        try:
            return datetime.date.fromisoformat(value).isoformat()
        except ValueError:
            return None
    return None


def _mockup_problems(value):
    from holophyte.tickets import ticket_template as tt
    if not isinstance(value, dict):
        return [f"header mockup must be a [mockup] table, not {_show(value)}"]
    problems = _unknown("mockup.", value, MOCKUP_FIELDS)
    url, note = value.get("url"), value.get("note")
    if url is None:
        problems.append("header mockup.url is missing: link the approved mock-up")
    elif not (isinstance(url, str) and tt.MOCKUP_URL_RE.fullmatch(url)):
        problems.append("header mockup.url is not a claude.ai artifact or a "
                        f"Lotuspod page: {_show(url)}")
    if "approved" not in value:
        problems.append("header mockup.approved is missing: give the date the "
                        "mock-up was approved")
    elif _approved(value["approved"]) is None:
        problems.append("header mockup.approved must be a date like 2026-10-19, "
                        f"not {_show(value['approved'])}")
    if not (isinstance(note, str) and note.strip()):
        problems.append("header mockup.note must say what was approved, "
                        f"not {_show(note)}")
    return problems + _boards_problems(value.get("boards", []))


def _boards_problems(boards):
    if not (isinstance(boards, list) and all(isinstance(b, dict) for b in boards)):
        return ["header mockup.boards must be [[mockup.boards]] entries, "
                f"not {_show(boards)}"]
    problems = []
    for index, board in enumerate(boards, 1):
        name = f"mockup.boards[{index}]"
        problems += _unknown(f"{name}.", board, BOARD_FIELDS)
        url, caption = board.get("url"), board.get("caption", "")
        if not (isinstance(url, str) and mockup_media_kind(url)):
            problems.append(f"header {name}.url is not a .png snapshot or a "
                            f".zip bundle: {_show(url)}")
        elif set("()") & set(url):
            problems.append(f"header {name}.url must not hold a parenthesis, "
                            f"which ends its markdown link: {_show(url)}")
        if not (isinstance(caption, str) and CAPTION_RE.fullmatch(caption)):
            problems.append(f"header {name}.caption must be one line without "
                            f"brackets, not {_show(caption)}")
    return problems


def _evidence_problems(value):
    if not (isinstance(value, list) and all(isinstance(e, dict) for e in value)):
        return [f"header evidence must be [[evidence]] entries, not {_show(value)}"]
    problems = []
    if len(value) > MAX_EVIDENCE:
        problems.append(f"header evidence has {len(value)} entries; at most "
                        f"{MAX_EVIDENCE}")
    for index, entry in enumerate(value, 1):
        name = f"evidence[{index}]"
        problems += _unknown(f"{name}.", entry, EVIDENCE_FIELDS)
        file, state = entry.get("file"), entry.get("state")
        if not (isinstance(file, str)
                and re.fullmatch(rf"{index:02d}-{SLUG}\.(?:png|webm)", file)):
            problems.append(f"header {name}.file must read {index:02d}-slug.png "
                            f"or {index:02d}-slug.webm, not {_show(file)}")
        if not (isinstance(state, str) and state.strip()):
            problems.append(f"header {name}.state must name the UI state to "
                            f"capture, not {_show(state)}")
    return problems


def _set_mockup(t, mockup):
    """The `## Mock-up` section a magic-line ticket would give for `mockup`."""
    from holophyte.tickets import ticket_template as tt
    note = " ".join(mockup["note"].split())
    t.mockup_source = "\n".join(
        [mockup["url"], f"Approved {_approved(mockup['approved'])}: {note}"]
        + [f"![{board.get('caption', '')}]({board['url']})"
           for board in mockup.get("boards", [])])
    t.mockup = tt.MD_LINK_RE.sub(r"\1", t.mockup_source)
    t.sections["Mock-up"] = t.mockup
    if "Mock-up" not in t.order:
        rank = tt.TEMPLATE_ORDER.index
        later = [index for index, name in enumerate(t.order)
                 if name in tt.TEMPLATE_ORDER and rank(name) > rank("Mock-up")]
        t.order.insert(later[0] if later else len(t.order), "Mock-up")


class Unconvertible(ValueError):
    pass


def converted(body):
    from holophyte.tickets import ticket_template as tt
    if split(body)[1] is not None:
        return body
    lines = body.splitlines(keepends=True)
    first = next((index for index, line in enumerate(lines)
                  if tt.H1_RE.match(line)), None)
    above, start = (0, 0) if first is None else (first, first + 1)
    fields, kept = {}, lines[above:start]
    movers = {tt.ESTIMATE: _estimate, "Mock-up": _mockup, "Evidence": _evidence}
    for name, part in _sections(lines[start:]):
        if name in movers:
            movers[name](fields, tt.COMMENT_RE.sub("", "".join(part[1:])))
        elif name == "What / Why / How":
            kept += _unfielded(fields, part)
        else:
            kept += part
    ending = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    header = "".join(row + ending for row in _header_rows(fields))
    return "".join(lines[:above]) + header + "".join(kept)


def _sections(lines):
    from holophyte.tickets import ticket_template as tt
    sections = [(None, [])]
    for line in lines:
        heading = tt.H2_RE.match(line)
        if heading:
            sections.append((heading.group(1).strip(), []))
        sections[-1][1].append(line)
    return sections


def _stated(text, unwrap=True):
    from holophyte.tickets import ticket_template as tt
    if unwrap:
        text = tt.MD_LINK_RE.sub(r"\1", text)
    return [line.strip() for line in text.splitlines() if line.strip()]


def _unfielded(fields, lines):
    from holophyte.tickets import ticket_template as tt
    kept, in_fence = [], False
    for line in lines:
        in_fence ^= bool(tt.FENCE_RE.match(line.strip()))
        if in_fence or not _bold_field(fields, line):
            kept.append(line)
    return kept


def _bold_field(fields, line):
    from holophyte.tickets import ticket_template as tt
    found = tt.BOLD_KEY_RE.match(tt.MD_LINK_RE.sub(r"\1", line).strip())
    if not (found and found.group(1) in BOLD_FIELDS):
        return False
    value = tt._clean(found.group(2))
    choices = {"UI change": tt.UI_CHANGE_VALUES,
               "Blast radius": tt.BLAST_RADIUS_VALUES}[found.group(1)]
    fields[BOLD_FIELDS[found.group(1)]] = (value.lower() if value.lower() in choices
                                          else value)
    return True


def _estimate(fields, text):
    from holophyte.tickets import ticket_template as tt
    for line in _stated(text):
        estimate = tt.ESTIMATE_RE.match(line)
        mode = tt.ORCHESTRATION_RE.match(line)
        named = tt.IMPLEMENTER_RE.match(line)
        if estimate and "estimate" not in fields:
            fields["estimate"] = int(estimate.group(1))
            fields["depends_on"] = tt._deps(estimate.group(2))
        elif mode and "orchestration" not in fields:
            fields["orchestration"] = mode.group(1)
        elif named and "implementer" not in fields:
            fields["implementer"] = named.group(1)
        else:
            raise Unconvertible(line)


def _mockup(fields, text):
    lines = _stated(text, unwrap=False)
    mockup = fields.setdefault("mockup", {}) if lines else {}
    for line in lines:
        if not _mockup_line(mockup, line):
            raise Unconvertible(line)


def _mockup_line(mockup, line):
    from holophyte.tickets import ticket_template as tt
    approval = APPROVAL_RE.fullmatch(line)
    if approval and "approved" not in mockup:
        try:
            mockup["approved"] = datetime.date.fromisoformat(approval.group(1))
        except ValueError:
            return False
        mockup["note"] = approval.group(2).strip()
        return True
    link, bare = LINK_LINE_RE.fullmatch(line), BARE_LINE_RE.fullmatch(line)
    if link:
        caption, urls = link.group(1), [link.group(1), link.group(2)]
    elif bare and tt._mockup_urls(bare.group(1)):
        caption, urls = "", tt._mockup_urls(bare.group(1))[:1]
    else:
        return False
    if mockup_media_kind(urls[-1]):
        board = {"url": urls[-1], **({"caption": caption} if caption else {})}
        mockup.setdefault("boards", []).append(board)
        return True
    design = next((url for url in urls if tt.MOCKUP_URL_RE.fullmatch(url)), None)
    if design is None or "url" in mockup:
        return False
    mockup["url"] = design
    return True


def _evidence(fields, text):
    from holophyte.tickets import ticket_template as tt
    states = [state for state in tt._evidence_states(tt.MD_LINK_RE.sub(r"\1", text))
              if state.strip()]
    entries = fields.setdefault("evidence", [])
    for position, state in enumerate(states, len(entries) + 1):
        named = EVIDENCE_FILE_RE.fullmatch(state)
        entries.append({"file": named.group(1), "state": named.group(2)} if named
                       else {"file": f"{position:02d}-{_slug(state)}.png",
                             "state": state})


def _slug(state):
    words = [re.sub(r"[^a-z0-9]", "", word.lower()) for word in state.split()]
    return "-".join([word for word in words if word][:SLUG_WORDS]) or "state"


def _header_rows(fields):
    rows = [FENCE] + _rows(fields, FIELDS)
    mockup = fields.get("mockup")
    if mockup is not None:
        rows += ["[mockup]"] + _rows(mockup, MOCKUP_FIELDS)
        for board in mockup.get("boards", []):
            rows += ["[[mockup.boards]]"] + _rows(board, BOARD_FIELDS)
    for entry in fields.get("evidence", []):
        rows += ["[[evidence]]"] + _rows(entry, EVIDENCE_FIELDS)
    return rows + [FENCE]


def _rows(table, keys):
    return [f"{key} = {_toml(table[key])}" for key in keys
            if key in table and key not in TABLES]


def _toml(value):
    if isinstance(value, list):
        return "[" + ", ".join(map(_toml, value)) + "]"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    return value.isoformat() if isinstance(value, datetime.date) else str(value)
