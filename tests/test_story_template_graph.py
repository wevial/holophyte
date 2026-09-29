"""`ticket_template.py --story DIR` validates the children, roles and graph."""
import unittest

from tests.story_fixture import witness_path, write_children
from tests.test_story_template import StoryCliCase


def plan():
    return [["a", "scaffolding", [], []],
            ["b", "advances W1", [], []],
            ["c", "completes W1", ["a", "b"], []],
            ["d", "completes W2", ["c"], []]]


class StoryGraphCase(StoryCliCase):
    def planned(self, children):
        directory = self.story()
        write_children(directory, children)
        return directory

    def with_change(self, index, field, value):
        children = plan()
        children[index][field] = value
        return self.planned(children)


class ValidPlanTests(StoryGraphCase):
    def test_four_children_serving_two_witnesses_are_ok(self):
        directory = self.planned(plan())
        result = self.run_cli(directory)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f"{directory}: OK"])


class ChildTests(StoryGraphCase):
    def test_child_without_verify_commands_is_refused(self):
        directory = self.planned(plan())
        child = directory / "children" / "02-b.md"
        text = child.read_text()
        start = text.index("## Verify command(s)")
        child.write_text(text[:start] + text[text.index("## Implementation"):])
        self.assert_refused(directory, "child 02-b is not a valid ticket: "
                            "missing section '## Verify command(s)'")

    def test_child_without_story_section_is_refused(self):
        self.assert_refused(self.with_change(0, 1, None),
                            "child 01-a has no '## Story' section")

    def test_eleven_children_are_refused(self):
        extra = [[f"s{n}", "scaffolding", [], []] for n in range(7)]
        self.assert_refused(self.planned(plan() + extra),
                            "the story has 11 children; the cap is 10")

    def test_two_children_sharing_a_slug_are_refused(self):
        children = plan()
        children[0][2] = ["c"]
        directory = self.planned(children + [["a", "scaffolding", [], []]])
        self.assert_refused(directory, "children 01-a and 05-a share the "
                            "slug a")


class RoleTests(StoryGraphCase):
    def test_witness_completed_by_two_children_is_refused(self):
        directory = self.planned(
            plan() + [["e", "completes W1", ["a", "b"], []]])
        self.assert_refused(directory, "witness W1 is completed by 2 "
                            "children: 03-c, 05-e")

    def test_story_with_no_children_is_refused(self):
        result = self.run_cli(self.planned([]))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.blockers(result), [
            "  - witness W1 has no completing child",
            "  - witness W2 has no completing child"])

    def test_witness_no_child_completes_is_refused(self):
        self.assert_refused(self.with_change(3, 1, "advances W2"),
                            "witness W2 has no completing child")

    def test_role_naming_an_unknown_witness_is_refused(self):
        self.assert_refused(self.with_change(1, 1, "advances W1, W9"),
                            "child 02-b's role names W9, which is no witness")

    def test_completing_child_not_calling_its_witness_new_is_refused(self):
        directory = self.planned(plan())
        child = directory / "children" / "04-d.md"
        text = child.read_text()
        self.assertIn(f"the new `{witness_path(2)}`", text)
        child.write_text(text.replace("the new `", "the `"))
        self.assert_refused(directory, "child 04-d completes W2 but does not "
                            f"call its witness file new: {witness_path(2)}")


class GraphTests(StoryGraphCase):
    def test_dependency_on_no_sibling_is_refused(self):
        self.assert_refused(self.with_change(3, 2, ["c", "zz"]),
                            "child 04-d depends on zz, which is no sibling")

    def test_two_children_depending_on_each_other_are_refused(self):
        self.assert_refused(self.with_change(0, 2, ["c"]),
                            "children 01-a, 03-c depend on each other in a "
                            "cycle")

    def test_completing_child_not_after_its_advancing_child_is_refused(self):
        self.assert_refused(self.with_change(2, 2, ["a"]),
                            "child 03-c completes W1 but does not depend on "
                            "02-b, which advances it")


class PlanAdvisoryTests(StoryGraphCase):
    def test_scaffolding_no_completing_child_follows_is_advised(self):
        self.assert_advised(self.with_change(2, 2, ["b"]),
                            "scaffolding child 01-a precedes no completing "
                            "child")

    def test_unordered_children_naming_one_file_are_advised(self):
        children = plan()
        for child in children[:2]:
            child[3] = ["Wire the export into `holophyte/cli.py`."]
        self.assert_advised(self.planned(children),
                            "children 01-a and 02-b both name "
                            "holophyte/cli.py with no dependency path")


if __name__ == "__main__":
    unittest.main()
