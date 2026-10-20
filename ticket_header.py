"""The `+++` TOML header a ticket can open with: its typed fields, each checked."""
import datetime
import json
import re
import tomllib

import ticket_template as tt

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
BESIDE = (
    (lambda t: t.ui_change is not None,
     "'**UI change:**' line beside a header: give ui_change in the header only"),
    (lambda t: t.blast_radius is not None,
     "'**Blast radius:**' line beside a header: give blast_radius in the header only"),
    (lambda t: tt.ESTIMATE in t.order,
     "'## Estimate & dependencies' beside a header: give estimate and depends_on "
     "in the header only"),
    (lambda t: "Mock-up" in t.order,
     "'## Mock-up' beside a header: give [mockup] in the header only"),
    (lambda t: "Evidence" in t.order,
     "'## Evidence' beside a header: give [[evidence]] entries in the header only"),
)


def split(text):
    """`text` without its header, and None or the header's (fields, problem)."""
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


def apply(t, header):
    if header is None:
        return
    fields, problem = header
    t.header_problems = [message for present, message in BESIDE if present(t)]
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


def mockup_media_kind(url):
    media = MOCKUP_MEDIA_RE.fullmatch(url)
    return MOCKUP_MEDIA_KINDS[media.group(1)] if media else None


def _checked(fields):
    """The fields that pass their checks, and a problem for each that does not."""
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
    if type(value) is int and 1 <= value <= tt.MAX_ESTIMATE_MIN:
        return []
    return [f"header estimate must be whole minutes from 1 to "
            f"{tt.MAX_ESTIMATE_MIN}, not {_show(value)}"]


def _depends_problems(value):
    if isinstance(value, list) and all(
            isinstance(key, str) and tt.LINEAR_ID_RE.match(key)
            for key in value):
        return []
    return [f"header depends_on must be a list of ticket keys, not {_show(value)}"]


def _implementer_problems(value):
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
        if not isinstance(state, str):
            problems.append(f"header {name}.state must name the UI state to "
                            f"capture, not {_show(state)}")
    return problems


def _set_mockup(t, mockup):
    """The `## Mock-up` section a magic-line ticket would give for `mockup`."""
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
