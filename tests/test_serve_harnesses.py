"""`GET /harnesses`: each registered harness, the binary it would run and
whether that file exists, the seats on it, their newest recorded probe and
the models it offers, read through the daemon from real files and a real
`PATH` lookup.

Run: python3 -m unittest discover -s tests -p 'test_serve_harnesses.py' -v
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from holophyte.agents.harness import ADAPTERS  # noqa: E402
from holophyte.agents.probe_log import last_probes, record_probe  # noqa: E402
from holophyte.agents.probes import ProbeResult  # noqa: E402
from tests.serve_contract_fixture import normalize_contract  # noqa: E402
from tests.serve_fixture import ServeTestCase  # noqa: E402

FIXTURE = HERE / "fixtures" / "serve" / "harnesses.json"
PINNED_ROOT = "/srv/holo"


class HarnessesTests(ServeTestCase):
    maxDiff = None

    def setUp(self):
        super().setUp()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.codex = self.stub(self.root / "tools" / "codex")
        self.on_path = self.stub(self.bin / "claude")
        self.missing = self.root / "gone" / "cursor-agent"
        path = patch.dict(os.environ, {"PATH": str(self.bin)})
        path.start()
        self.addCleanup(path.stop)

    @staticmethod
    def stub(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        return path

    def config(self, agents=""):
        return (f'[harnesses]\ncodex = "{self.codex}"\n'
                f'cursor = "{self.missing}"\n{agents}')

    def answer(self):
        code, _, body = self.request("GET", "/harnesses")
        self.assertEqual(code, 200)
        self.assertEqual([entry["name"] for entry in body["harnesses"]],
                         list(ADAPTERS))
        return {entry["name"]: entry for entry in body["harnesses"]}

    def record(self, seat, ok, ended):
        record_probe(self.project, ProbeResult(
            ["claude"], 0 if ok else 1, "ready" if ok else "quota exhausted",
            90, seat=seat), False, ended - 1, ended)

    def two_claude_seats(self):
        self.start(self.config(
            '[agents.implementer]\nharness = "claude"\n'
            'model = "claude-model-under-test"\n'
            '[agents.trimmer]\nharness = "claude"\nmodel = "sonnet"\n'))
        self.record("implementer", True, 1_700_000_000)
        self.record("trimmer", False, 1_700_000_100)

    def test_a_binary_is_found_at_its_configured_path_or_on_path(self):
        self.start(self.config())
        found = self.answer()
        self.assertEqual(
            {name: (entry["found"], entry["path"], entry["configured"])
             for name, entry in found.items()},
            {"claude": (True, str(self.on_path), False),
             "claude-sdk": (True, str(self.on_path), False),
             "codex": (True, str(self.codex), True),
             "cursor": (False, str(self.missing), True),
             "devin": (False, None, False),
             "t3": (False, None, False)})
        self.assertEqual(found["codex"]["binary"], str(self.codex))
        self.assertEqual(found["claude"]["binary"], "claude")

    def test_a_configured_file_that_cannot_run_is_not_found(self):
        self.codex.chmod(0o644)
        self.start(self.config())
        codex = self.answer()["codex"]
        self.assertEqual((codex["found"], codex["path"]),
                         (False, str(self.codex)))

    def test_the_newest_probe_of_a_harness_seats_is_served_without_a_process(self):
        self.two_claude_seats()
        failed = last_probes(self.project)["trimmer"]["primary"]
        with patch("subprocess.Popen", side_effect=AssertionError("spawned")):
            found = self.answer()
        self.assertEqual(found["claude"]["seats"], ["implementer", "trimmer"])
        self.assertEqual(found["claude"]["probe"], {
            "ok": False, "at_ms": 1_700_000_100_000, "why": failed["why"],
            "seat": "trimmer"})
        self.assertIsNotNone(failed["why"])
        self.assertEqual((found["codex"]["seats"], found["codex"]["probe"]),
                         ([], None))

    def test_a_command_string_seat_and_its_fallback_probe_count(self):
        self.start(self.config(
            '[agents]\nreviewer = "codex exec -m gpt-6-astra"\n'
            f'reviewer_fallback = "{self.root}/opt/cursor-agent -p"\n'))
        record_probe(self.project, ProbeResult(
            ["cursor-agent"], 0, "ready", 90, seat="reviewer"), True, 9, 10)
        found = self.answer()
        self.assertEqual(found["codex"]["seats"], ["reviewer"])
        self.assertIsNone(found["codex"]["probe"])
        self.assertEqual(found["cursor"]["seats"], ["reviewer_fallback"])
        self.assertEqual(found["cursor"]["probe"], {
            "ok": True, "at_ms": 10_000, "why": None,
            "seat": "reviewer_fallback"})

    def test_a_seats_model_outside_the_curated_list_is_listed_as_configured(self):
        self.two_claude_seats()
        models = self.answer()["claude"]["models"]
        self.assertEqual(models,
                         [{"id": model, "source": "curated"}
                          for model in ADAPTERS["claude"].models]
                         + [{"id": "claude-model-under-test",
                             "source": "configured"}])
        self.assertIn("sonnet", ADAPTERS["claude"].models)

    def test_a_secret_quoted_in_a_probe_reason_is_redacted(self):
        self.start(self.config(
            '[agents.implementer]\nharness = "claude"\n'
            '[linear]\napi_key = "lin_api_harness_secret"\n'))
        snapshot = {"implementer": {"primary": {
            "ok": False, "at_ms": 5, "duration_ms": 1,
            "why": "exit 1: lin_api_harness_secret rejected"}}}
        (self.project.holo_dir / "route-probes.json").write_text(
            json.dumps(snapshot))
        self.answer()
        self.assertNotIn("lin_api_harness_secret", self.raw_body)

    def test_the_pinned_fixture_is_the_answer(self):
        self.two_claude_seats()
        self.answer()
        pinned = json.loads(self.raw_body.replace(str(self.root), PINNED_ROOT))
        self.assertEqual(normalize_contract(pinned),
                         json.loads(FIXTURE.read_text()))
