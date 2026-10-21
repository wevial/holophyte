"""`/status` serves the story witness set the loop's witness step is
running, read from its record beside the store, and null when none runs."""
import json
import subprocess
import sys
from pathlib import Path

from holophyte.story.witness_running import mark_running, running_path
from tests.serve_fixture import ServeTestCase

ROOT = Path(__file__).resolve().parents[1]


def worked(body):
    return body["workers"], [(run["id"], run["ticket"], run["phase"])
                             for run in body["runs"]]


class StatusWitnessesTests(ServeTestCase):
    def status(self):
        code, _, body = self.request("GET", "/status")
        self.assertEqual(code, 200, body)
        return body

    def test_a_running_set_is_served_beside_the_unchanged_runs(self):
        self.seed()
        self.start("[loop]\nworkers = 2\n")
        without = self.status()
        mark_running(self.project, "DEMO-20", ["W3", "W4", "W5", "W6"])
        started = json.loads(running_path(self.project).read_text())["started_ms"]

        body = self.status()
        self.assertEqual(body["witnesses"], {
            "story": "DEMO-20", "keys": ["W3", "W4", "W5", "W6"],
            "started_ms": started, "elapsed_ms": body["now"] - started})
        self.assertEqual(worked(body), worked(without))

    def test_a_record_whose_writer_exited_is_served_as_null(self):
        self.seed()
        self.start()
        subprocess.run(
            [sys.executable, "-c",
             "import sys, types\n"
             "from holophyte.story.witness_running import mark_running\n"
             "mark_running(types.SimpleNamespace(holo_dir=sys.argv[1]),"
             " 'DEMO-20', ['W1'])\n",
             str(self.project.holo_dir)],
            cwd=ROOT, check=True, timeout=60)
        self.assertTrue(running_path(self.project).exists())

        self.assertIsNone(self.status()["witnesses"])

    def test_no_record_is_served_as_null(self):
        self.seed()
        self.start()

        self.assertIsNone(self.status()["witnesses"])

    def test_a_record_that_does_not_parse_is_served_as_null(self):
        self.seed()
        self.start()
        running_path(self.project).write_text("{")

        self.assertIsNone(self.status()["witnesses"])
