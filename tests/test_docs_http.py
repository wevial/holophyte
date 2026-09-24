"""Keep the HTTP reference naming every key the pinned daemon answers carry."""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "serve"
REFERENCE = ROOT / "docs" / "reference" / "http.md"


def section(document, heading):
    """The `## ` section whose heading starts with `heading`, so
    `` `GET /runs/N` `` never matches `` `GET /runs/N/files` ``."""
    for part in re.split(r"^## ", document, flags=re.MULTILINE)[1:]:
        if part.startswith(heading):
            return part
    raise AssertionError(f"no section headed {heading}")


def keys(value):
    """Every object key of a decoded JSON value, at every depth."""
    if isinstance(value, dict):
        return set(value) | {k for child in value.values() for k in keys(child)}
    if isinstance(value, list):
        return {k for child in value for k in keys(child)}
    return set()


def assert_covers(case, text, fixture):
    body = json.loads((FIXTURES / fixture).read_text())
    # The host root's `sweep.projects` is keyed by project name: data, not
    # a field to document.
    names = {project["name"] for project in body.get("projects", [])
             if isinstance(project, dict)}
    missing = sorted(key for key in keys(body) - names
                     if f"`{key}`" not in text and f'"{key}"' not in text)
    case.assertEqual(missing, [], f"{fixture} keys undocumented: {missing}")


class HttpReferenceTests(unittest.TestCase):
    def setUp(self):
        self.document = REFERENCE.read_text()

    def test_status_section_names_every_pinned_key(self):
        assert_covers(self, section(self.document, "`GET /status`"),
                      "status.json")

    def test_run_detail_section_names_every_pinned_key(self):
        assert_covers(self, section(self.document, "`GET /runs/N`"),
                      "run-detail.json")

    def test_host_sections_name_every_key_the_host_root_answers(self):
        assert_covers(self, section(self.document, "Host `GET /status`"),
                      "host-status.json")
        assert_covers(self, section(self.document, "Host `GET /attention`"),
                      "host-attention.json")

    def test_dropping_a_documented_key_names_it(self):
        text = section(self.document, "`GET /status`")
        dropped = text.replace("`stop_requested`", "").replace(
            '"stop_requested"', "")
        with self.assertRaisesRegex(AssertionError, "stop_requested"):
            assert_covers(self, dropped, "status.json")

    def test_pr_open_follows_the_recorded_park_kind(self):
        text = section(self.document, "`GET /attention`")
        self.assertTrue("parkKind" in text,
                        "GET /attention does not name parkKind")
        self.assertFalse("whose question opens with" in text,
                         "GET /attention still classifies by question wording")


if __name__ == "__main__":
    unittest.main()
