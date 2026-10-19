"""Bucket evidence images fetched as the factory, and the reviewer's brief."""

import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from holophyte.pr import evidence_fetch, mockup
from holophyte.review.briefs import evidence_brief
from tests.test_pr_media import PNG


class _Host(BaseHTTPRequestHandler):
    """Answers like the media host: 403 to urllib's default agent."""

    def do_GET(self):
        agent = self.headers.get("User-Agent", "")
        self.server.requests.append((self.path, agent))
        if self.path in self.server.missing:
            self.send_error(404)
        elif self.path in self.server.forbidden or agent.startswith("Python-urllib"):
            self.send_error(403)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG)))
            self.end_headers()
            self.wfile.write(PNG)

    def log_message(self, *args):
        pass


class _ServedCase(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Host)
        self.server.requests = []
        self.server.missing, self.server.forbidden = set(), set()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def section(self, *names):
        lines = ["## Evidence", ""]
        for name in names:
            lines += [f"- {name} — captured", f"![{name}]({self.base}/{name}.png)"]
        return "\n".join(lines)


class EvidenceFetchTests(_ServedCase):
    def test_download_passes_a_host_that_refuses_urllibs_default_agent(self):
        link = mockup.media_links(self.section("01-board"))[0]
        self.assertEqual(mockup._download(link, self.base), PNG)
        section = self.section("01-board")
        self.assertEqual(evidence_fetch.unfetched(section, self.base), {})
        self.server.missing.add("/01-board.png")
        self.assertEqual(evidence_fetch.unfetched(section, self.base),
                         {f"{self.base}/01-board.png": "HTTP 404"})


class EvidenceBriefTests(_ServedCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.enterContext(patch.dict(os.environ,
                                     HOLOPHYTE_HOME=str(self.repo / ".state")))
        for args in (["init", "-q", "-b", "main"],
                     ["-c", "user.email=t@example.invalid", "-c", "user.name=T",
                      "commit", "-qm", "base", "--allow-empty"]):
            subprocess.run(["git", *args], cwd=self.repo, check=True)
        self.config = {"merge": {"mode": "pr"}}
        self.target = SimpleNamespace(path=self.repo, config=lambda: self.config,
                                      config_path=self.repo / "config.toml")

    def brief(self, section):
        # pr_media.prepare's own capture and publishing are covered in test_pr_media.
        with patch("holophyte.pr.pr_media.prepare", return_value=section):
            return evidence_brief(self.target, self.repo, "KO-1")

    def test_brief_lists_the_local_copy_and_names_an_unfetchable_image(self):
        self.config["merge"]["media_bucket"] = {
            "endpoint": "https://objects.example.invalid", "bucket": "evidence",
            "public_base": self.base}
        self.assertTrue(mockup.clear_captured(self.repo))
        (self.repo / mockup.CAPTURED / "01-board.png").write_bytes(PNG)
        self.server.forbidden.add("/01-board.png")
        brief = self.brief(self.section("01-board"))
        self.assertIn(f"- `{mockup.CAPTURED}/01-board.png`", brief)
        self.assertIn(f"The factory could not fetch {self.base}/01-board.png "
                      "(HTTP 403).", brief)
        self.assertIn("write `could not fetch URL: REASON` before the CRITERION "
                      "lines, never as a finding", brief)
        self.assertNotIn("Missing or failed visual evidence counts against the "
                         "candidate", brief)

    def test_brief_fetches_nothing_without_a_media_bucket(self):
        self.server.forbidden.add("/01-board.png")
        brief = self.brief(self.section("01-board"))
        self.assertEqual(self.server.requests, [])
        self.assertIn(f"{self.base}/01-board.png", brief)
        self.assertNotIn("The factory could not fetch", brief)


if __name__ == "__main__":
    unittest.main()
