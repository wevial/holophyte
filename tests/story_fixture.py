"""A valid story directory: story.md, a witnesses tree and a children directory."""
import re
from pathlib import Path

WITNESS_FILE = """import unittest


class Witness{n}Tests(unittest.TestCase):
    def test_outcome_{n}_holds(self):
        self.assertEqual(sorted([2, 1]), [1, 2])
"""


def witness_path(n):
    return f"tests/test_story_w{n}.py"


def write_story(parent, witnesses=2, standing_orders=1, extra="", name="story"):
    """Write the story under `parent` with one child completing every
    witness; `extra` goes before 'Open questions'."""
    directory = Path(parent) / name
    (directory / "children").mkdir(parents=True)
    lines = [f"- [ ] W{n}: outcome {n} holds "
             f"(a test in {witness_path(n)} witnesses outcome {n})"
             for n in range(1, witnesses + 1)]
    commands = [f"W{n}: python3 -m unittest tests.test_story_w{n}"
                for n in range(1, witnesses + 1)]
    orders = [f"- Standing order {n} is kept."
              for n in range(1, standing_orders + 1)]
    for n in range(1, witnesses + 1):
        path = directory / "witnesses" / witness_path(n)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(WITNESS_FILE.format(n=n))
    (directory / "witnesses").mkdir(exist_ok=True)
    if witnesses:
        keys = ", ".join(f"W{n}" for n in range(1, witnesses + 1))
        write_children(directory, [("all", f"completes {keys}", [], [])])
    (directory / "story.md").write_text("\n".join([
        "# Orders export as CSV", "",
        "## Summary", "", "Orders can be exported as CSV.", "",
        "## Goal", "", "An operator downloads every order as one CSV file.", "",
        "## Witnesses", "", *lines, "",
        "## Witness commands", "", "```", *commands, "```", "",
        "## Standing orders", "", *orders, "",
        "## Out of scope", "", "- Other export formats.", "",
        extra,
        "## Open questions", "", "- None", ""]))
    return directory


def child_body(slug, role, depends_on=(), notes=()):
    """A valid child ticket; `role` None leaves out its Story section."""
    module = f"tests/test_story_{slug.replace('-', '_')}.py"
    witnesses = re.findall(r"W(\d+)", role or "") if (
        role or "").startswith("completes") else []
    summary = f"Step {slug} of the orders export."
    if witnesses:
        summary += " It turns green the new " + ", ".join(
            f"`{witness_path(n)}`" for n in witnesses) + "."
    notes = [f"- {note}" for note in notes] or ["- Keep the CSV header."]
    story = ["## Story", "", f"Role: {role}", ""] if role else []
    return "\n".join([
        f"# Orders export step {slug}", "",
        "## Summary", "", summary, "",
        "## What / Why / How", "",
        f"**What:** Step {slug} of the CSV export.", "",
        "**Why:** Operators need the orders outside the app.", "",
        "**How:** Extend the existing export module.", "",
        "## In scope", "", f"- The new `{module}` pins this step.", "",
        "## Out of scope", "", "- Other export formats.", "",
        "## Acceptance criteria", "",
        f"- [ ] Given an order, when step {slug} runs, then the order is "
        "exported.", "",
        "## Verify command(s)", "", "```",
        f".venv/bin/python -m unittest {module[:-3].replace('/', '.')}",
        "```", "",
        "## Implementation notes", "", *notes, "",
        *story,
        "## Estimate & dependencies", "",
        f"Estimate: 20 min · Depends on: {', '.join(depends_on) or 'none'}", "",
        "## Open questions", "", "- None", ""])


def write_children(directory, children):
    """Replace the children with each (slug, role, depends_on, notes) as
    NN-slug.md, in order."""
    folder = Path(directory) / "children"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.md"):
        old.unlink()
    for number, (slug, role, depends_on, notes) in enumerate(children, 1):
        (folder / f"{number:02d}-{slug}.md").write_text(
            child_body(slug, role, depends_on, notes))
    return folder
