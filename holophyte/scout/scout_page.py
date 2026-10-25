"""A done scout's report as Lotuspod markdown, its unfiled items a decisions table."""
from holophyte.scout.report import REVISE

FENCE = "```"


def one_line(text):
    return " ".join(str(text).splitlines())


def prose(text):
    return [f" {line}" if line.startswith(FENCE) else line
            for line in str(text).splitlines()]


def fenced(text, info=""):
    return [FENCE + info, *prose(text), FENCE]


def cell(text):
    return one_line(text).replace("|", "/")


def row(cells):
    return f"| {' | '.join(cell(text) for text in cells)} |"


def table(header, rows):
    return [row(header), row(["---"] * len(header)), *map(row, rows)]


def heading_line(scout):
    line = f"{scout['kind']} · base {(scout['base_sha'] or '-')[:7]}"
    subject = scout["subject"]
    if subject is not None:
        line += f" · about {subject['kind']}:{subject['ref']}"
    return line


def per_pass(reports, render):
    lines = []
    for entry in reports:
        rendered = render(entry["report"])
        if len(reports) > 1 and rendered:
            lines += [f"### Pass {entry['pass']}", ""]
        lines += rendered
    return lines


def evidence_lines(item):
    if "file" in item:
        unverified = ("" if item["verified"]
                      else f" unverified: {one_line(item['why'] or '-')}")
        return [f"- `{item['file']}:{item['line']}`{unverified}"]
    if "command" in item:
        return [f"- `{one_line(item['command'])}`", "",
                *fenced(item["output"])]
    if "url" in item:
        return [f"- {item['url']}"]
    return [f"- {one_line(item['text'])}"]


def findings_lines(reports):
    lines, number = [], 0
    for entry in reports:
        for finding in entry["report"]["findings"]:
            number += 1
            lines += [f"### F{number} {one_line(finding['title'])}", "",
                      f"Status: {finding['status']}", ""]
            for item in finding["evidence"]:
                lines += evidence_lines(item)
            lines.append("")
    return lines


def reproduction_lines(report):
    reproduction = report["reproduction"]
    if reproduction is None:
        return []
    rows = [[hypothesis[key] for key in ("hypothesis", "separates", "verdict")]
            for hypothesis in reproduction["hypotheses"]]
    return [f"Level: {reproduction['level']}", "",
            *table(("Hypothesis", "Separates", "Verdict"), rows), ""]


def action_lines(item):
    lines = [f"### A{item['position']} · {item['kind']} · "
             f"{one_line(item['title'])}", "", *prose(item["why"]), ""]
    if item["ticket"] is not None:
        lines += [f"Filed as {item['ticket']}", ""]
    if item["revises"] is not None:
        lines += [f"Revises {item['revises']['ticket']} at revision"
                  f" {item['revises']['revision']}", ""]
    return lines + [*fenced(item["body"], "markdown"), ""]


def decision_lines(actions):
    rows = [[f"A{item['position']}", item["title"], item["why"],
             "File / Look further / Drop"]
            for item in actions
            if item["ticket"] is None and item["kind"] != REVISE]
    if not rows:
        return []
    return ["## Decisions for the maintainer", "",
            *table(("#", "Question", "Context", "Options"), rows), ""]


def section(title, lines):
    return [f"## {title}", "", *lines]


def page_lines(body):
    scout, reports = body["scout"], body["reports"]
    lines = [f"# Scout {scout['id']}: {one_line(scout['ask'])}", "",
             heading_line(scout), ""]
    lines += section("Report", per_pass(
        reports, lambda report: [*prose(report["summary"]), ""]))
    lines += section("Findings", findings_lines(reports))
    if scout["kind"] == "bug":
        lines += section("Reproduction", per_pass(reports, reproduction_lines))
    lines += section("Action items", [line for item in body["actions"]
                                      for line in action_lines(item)])
    return lines + decision_lines(body["actions"])
