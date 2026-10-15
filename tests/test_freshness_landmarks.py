"""The landmark check parks a ticket only for a landmark main has lost.

Run: python3 -m unittest discover -s tests -p 'test_freshness_landmarks.py' -v
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from loop_fixture import VALID_BODY  # noqa: E402 - after the sys.path insert

from holophyte.review.freshness import landmark_reasons  # noqa: E402

IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@example.invalid")


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def commit_main(repo, files):
    git(repo, "init", "-q", "-b", "main")
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, *IDENTITY, "commit", "-q", "-m", "fixture")


def notes_body(item):
    return VALID_BODY.replace("## Implementation notes\n\n* None.\n",
                              f"## Implementation notes\n\n* {item}\n")


class LibraryNameTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        commit_main(self.repo, {
            "holophyte/serve/views.py": "BACKLOG = 'backlog'\n",
            "holophyte/serve/contract.py": "from typing import Literal\n",
        })

    def test_a_library_name_found_elsewhere_on_main_is_no_stale_reason(self):
        body = notes_body("A column's `state` is a `Literal` of `BACKLOG` in"
                          " `holophyte/serve/views.py`.")
        reasons = landmark_reasons(self.repo, body)
        self.assertFalse([r for r in reasons if "`Literal`" in r], reasons)


if __name__ == "__main__":
    unittest.main()
