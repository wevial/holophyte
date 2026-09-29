"""A valid story directory: story.md, a witnesses tree and a children directory."""
from pathlib import Path

WITNESS_FILE = """import unittest


class Witness{n}Tests(unittest.TestCase):
    def test_outcome_{n}_holds(self):
        self.assertEqual(sorted([2, 1]), [1, 2])
"""


def witness_path(n):
    return f"tests/test_story_w{n}.py"


def write_story(parent, witnesses=2, standing_orders=1, extra="", name="story"):
    """Write the story under `parent`; `extra` goes before 'Open questions'."""
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
