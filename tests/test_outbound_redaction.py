"""Observe outbound payloads after verify output and writer replies enter them."""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from holophyte import agents, board, gates, loop, pr, redact
from holophyte.project import Project

SENTINEL = "outbound-sentinel-542"


class OutboundRedactionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.target = Project(
            path=self.root, holo_dir=self.root, store_path=self.root / "store.db",
            config_path=self.root / "config.toml", worktrees=self.root / "trees")
        self.enterContext(patch.object(redact, "_environment_values", frozenset()))
        self.enterContext(patch.dict(os.environ, {}, clear=True))

    def register(self, enabled):
        redact._environment_values = frozenset()
        if enabled is True:
            redact.register_values([SENTINEL])
        self.target.config_path.write_text(
            f'[service]\napi_key = "{SENTINEL}"\n' if enabled == "config" else "")
        self.target._config = None
        return "[redacted]" if enabled else SENTINEL

    def test_verify_output_reaches_both_agent_roles_and_routes_safely(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        redact.register_values([SENTINEL])
        command = 'printf "%s\\n" "$VERIFY_VALUE"'
        with patch.dict(os.environ, VERIFY_VALUE=SENTINEL):
            ok, output = gates.run_verify(command, self.root)
        self.assertTrue(ok)
        self.assertIn(SENTINEL, output)
        goal = "Judge this candidate.\n" + loop._verify_brief(command, ok, output)
        fake = self.root / "agent.py"
        fake.write_text("import json, sys\nprint(json.dumps(sys.argv[-1]))\n")
        for enabled in (False, True, "config"):
            replacement = self.register(enabled)
            expected = goal.replace(SENTINEL, replacement)
            for role in ("review", "adjudicate"):
                for configured in (True, False):
                    with self.subTest(redacted=enabled, role=role, argv=configured):
                        route = shlex.join([sys.executable, str(fake)])
                        routes = SimpleNamespace(
                            commands={role: route} if configured else {})
                        with (patch.object(agents, "routes", return_value=routes),
                              patch.object(agents, "publish_review_refs"),
                              patch.object(agents, "check_review_refs"),
                              patch.object(agents.review_runner, "run_review",
                                           return_value="VERDICT: APPROVE") as runner):
                            reply = agents.agent(self.target, role, goal, self.root,
                                                 base_sha="1" * 40,
                                                 candidate_sha="2" * 40)
                        received = (json.loads(reply) if configured
                                    else runner.call_args.kwargs["prompt"])
                        self.assertEqual(received, expected)

    def test_writer_reply_is_redacted_at_gh_create_and_refresh(self):
        url = "https://github.com/example/repo/pull/1"
        title, prose = pr.parse_pr_text(f"TITLE: Fix {SENTINEL}\nObserved {SENTINEL}.")
        body = pr.pr_body_written(prose, "KO-542", "https://linear.app/issue/KO-542")
        body += "\n\n## Evidence\n![demo](https://example.com/demo.png?key=public-image)\n"
        pull = SimpleNamespace(url=url, repo="example/repo", number=1)
        for enabled in (False, True, "config"):
            replacement = self.register(enabled)
            with (self.subTest(redacted=enabled),
                  patch.object(pr.shutil, "which", return_value="gh"),
                  patch.object(pr, "origin_url", return_value="https://github.com/example/repo"),
                  patch.object(pr.subprocess, "run",
                               return_value=subprocess.CompletedProcess(
                                   [], 0, url, "")) as gh):
                self.assertEqual(
                    pr.create_pull_request(self.target, "task", title, body), url)
                pr.edit_pr_body(self.target, pull, body)
                create, edit = gh.call_args_list
                argv = create.args[0]
                self.assertEqual(argv[argv.index("--title") + 1],
                                 title.replace(SENTINEL, replacement))
                for call in (create, edit):
                    self.assertEqual(call.kwargs["input"],
                                     body.replace(SENTINEL, replacement))

    def test_escalation_comment_redacts_failure_reason_before_provider(self):
        history = [(n, f"verify failed: {SENTINEL}") for n in (1, 2, 3)]
        expected = (
            "**Blocked after 3 failed runs.** Counted since the last recorded human "
            "intervention, if any; attempt numbers are lifetime. The factory will not "
            "claim this ticket again until a human moves it out of this state. What "
            "each counted attempt ended on:\n\n"
            f"- attempt 1: verify failed: {SENTINEL}\n"
            f"- attempt 2: verify failed: {SENTINEL}\n"
            f"- attempt 3: verify failed: {SENTINEL}")
        ticket = SimpleNamespace(status="in_flight", linearIssueId="issue",
                                 linearIdentifier="KO-542")
        for enabled in (False, True):
            replacement = self.register(enabled)
            provider = Mock()
            with (self.subTest(redacted=enabled),
                  patch.object(board.store.read, "ticket_by_id", return_value=ticket),
                  patch.object(board, "failure_history", return_value=history),
                  patch.object(board, "block_ticket", return_value=True)):
                self.assertTrue(board.escalate(object(), 1, provider))
            provider.comment.assert_called_once_with(
                "issue", expected.replace(SENTINEL, replacement))
        # Redact before a comment's length cap can split a registered value.
        self.assertNotIn(SENTINEL[:10], board.comment_body(SENTINEL, limit=10))


if __name__ == "__main__":
    unittest.main()
