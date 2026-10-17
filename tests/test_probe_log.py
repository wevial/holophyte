import json
import threading
from concurrent.futures import ThreadPoolExecutor
from time import time

from holophyte.agents.probe_log import last_probes, record_probe
from holophyte.agents.probes import ProbeResult, probe_implementer, probe_seat
from holophyte.config.project import Project
from tests.serve_fixture import ServeTestCase


class ProbeLogTests(ServeTestCase):
    def test_real_launch_failure_records_reason_without_executable_path(self):
        script = self.root / "private-agent-probe"
        script.write_text("#!/bin/sh\necho private-output\n")
        script.chmod(0o644)
        (self.db.parent / "config.toml").write_text(
            f'[agents]\nimplementer = "{script}"\n')
        project = Project.locate(self.target)
        probe = probe_implementer(project)
        self.assertFalse(probe.ok)
        self.assertIn("Permission denied", probe.launch_error)
        entry = last_probes(project)["implementer"]["primary"]
        self.assertFalse(entry["ok"])
        self.assertIn("could not start:", entry["why"])
        self.assertIn("Permission denied", entry["why"])
        raw = (project.holo_dir / "route-probes.json").read_text()
        for private in (str(script), script.name, "private-output"):
            self.assertNotIn(private, raw)

    def test_real_probe_replaces_result_and_status_serves_failure_and_fallback(self):
        self.seed()
        script = self.root / "agent-probe"
        script.write_text("#!/bin/sh\necho ready\n")
        script.chmod(0o755)
        self.start(f'[agents]\nimplementer = "{script}"\n'
                   'implementer_fallback = "backup-agent --model spare"\n')
        before = int(time() * 1000)
        self.assertTrue(probe_implementer(self.project).ok)
        entry = last_probes(self.project)["implementer"]["primary"]
        self.assertTrue(entry["ok"])
        self.assertIsNone(entry["why"])
        self.assertIsInstance(entry["duration_ms"], int)
        self.assertGreaterEqual(entry["duration_ms"], 0)
        self.assertGreaterEqual(entry["at_ms"], before)
        self.assertLessEqual(entry["at_ms"], int(time() * 1000))

        script.write_text("#!/bin/sh\necho private-output\nexit 1\n")
        self.assertFalse(probe_implementer(self.project).ok)
        failed = last_probes(self.project)["implementer"]["primary"]
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["why"], "exit 1")
        raw = (self.project.holo_dir / "route-probes.json").read_text()
        for private in (str(script), "ready", "private-output"):
            self.assertNotIn(private, raw)
        code, _, status = self.request("GET", "/status")
        self.assertEqual(code, 200)
        served = status["route_probes"]["implementer"]["primary"]
        self.assertFalse(served["ok"])
        self.assertEqual(served["why"], "exit 1")
        self.assertEqual(served["age_ms"], status["now"] - failed["at_ms"])
        self.assertEqual(status["route_probes"]["reviewer"],
                         {"primary": None, "fallback": None})
        self.assertEqual(status["route_labels"]["implementer_fallback"],
                         "backup-agent spare")
        self.assertIsNone(status["route_labels"]["adjudicator_fallback"])

    def test_concurrent_seats_survive_and_older_result_cannot_replace_latest(self):
        project = Project.locate(self.target)
        barrier = threading.Barrier(2)

        def record(seat):
            barrier.wait(timeout=5)
            record_probe(project, ProbeResult([], 1, "", 90, seat=seat),
                         False, 10, 12)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(record, ("implementer", "reviewer")))
        entries = last_probes(project)
        self.assertEqual(set(entries), {"implementer", "reviewer"})
        for seat in entries:
            self.assertEqual(entries[seat]["primary"],
                             {"ok": False, "at_ms": 12000,
                              "duration_ms": 2000, "why": "exit 1"})
        record_probe(project, ProbeResult([], 0, "ready", 90), False, 1, 2)
        self.assertFalse(last_probes(project)["implementer"]["primary"]["ok"])

    def test_fallback_reason_is_redacted_and_bounded_without_primary(self):
        (self.db.parent / "config.toml").write_text(
            '[agents]\nimplementer = "agent --token secret-value"\n')
        project = Project.locate(self.target)
        probe = ProbeResult(["private-command"], None, "private-output", 90,
                            launch_error="secret-value " + "x" * 250)
        record_probe(project, probe, True, 1, 1.25)
        entry = last_probes(project)["implementer"]["fallback"]
        self.assertEqual(entry["duration_ms"], 250)
        self.assertEqual(len(entry["why"]), 200)
        self.assertTrue(entry["why"].startswith("could not start: "))
        raw = (project.holo_dir / "route-probes.json").read_text()
        for private in ("secret-value", "private-command", "private-output"):
            self.assertNotIn(private, raw)

    def test_missing_unreadable_log_and_unprobed_seat_serve_null_entries(self):
        self.seed()
        self.start()
        path = self.project.holo_dir / "route-probes.json"
        self.assertIsNone(probe_seat(self.project, "implement", fallback=True))
        self.assertFalse(path.exists())
        for content in (None, "not json", json.dumps([1]),
                        json.dumps({"implementer": 1})):
            with self.subTest(content=content):
                if content is not None:
                    path.write_text(content)
                code, _, status = self.request("GET", "/status")
                self.assertEqual(code, 200)
                self.assertEqual(set(status["route_probes"]),
                                 set(status["active_routes"]))
                for seat in status["route_probes"].values():
                    self.assertEqual(seat, {"primary": None, "fallback": None})
