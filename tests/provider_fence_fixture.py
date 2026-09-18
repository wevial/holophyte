"""Shared fence round trips for the file and Linear provider conformance tests."""
import tempfile
from pathlib import Path

import store
import store.read
import ticket_template


class FenceConformanceMixin:
    """A provider fixture supplies seed, edit, and provider."""

    def test_valid_verify_fences_mirror_as_ready(self):
        self.seed("KO-1", verify="echo ok\necho done")
        bare = self.provider.fetch_task("KO-1")
        with tempfile.TemporaryDirectory() as tmp:
            conn = store.open(Path(tmp) / "store.sqlite3")
            self.addCleanup(conn.close)
            project = store.ensure_project(conn, "test-team", tmp)
            for tag in ("", "sh", "shell-session extra"):
                with self.subTest(tag=tag):
                    body = bare["body"].replace("```\n", f"```{tag}\n", 1)
                    parsed = ticket_template.parse(body)
                    problems = ticket_template.validate(parsed)
                    self.assertEqual(ticket_template.blocking(problems), [])
                    self.assertEqual(problems, [
                        f"advisory: verify fence carries a language tag ({tag}); "
                        "the factory ignores it"
                    ] if tag else [])
                    self.edit("KO-1", body)
                    task = self.provider.fetch_task("KO-1")
                    self.assertEqual(task["verify"], bare["verify"])
                    self.assertEqual(task["verify"].splitlines(),
                                     parsed.verify_commands)
                    store.mirror_ticket(
                        conn, project, task["issue_id"], task["id"], task["title"],
                        acceptance_criteria=task["criteria"],
                        verification_commands=(task["verify"] or "").splitlines(),
                        body=body)
                    row = store.read.ticket_by_identifier(conn, "KO-1")
                    self.assertEqual(row.verificationCommands, ("echo ok", "echo done"))
                    self.assertEqual(row.status, "ready")

    def test_contract_fences_accept_info_strings(self):
        self.seed("KO-1", verify="echo ok")
        bare = self.provider.fetch_task("KO-1")["body"]
        for tag in ("", "sh", "text extra"):
            with self.subTest(tag=tag):
                body = bare.replace("## Implementation notes", "## Contract checks\n\n"
                                    f"```{tag}\nREADME.md: Holophyte\n```\n\n"
                                    "## Implementation notes")
                problems = ticket_template.validate(ticket_template.parse(body))
                self.assertEqual(problems, [
                    f"advisory: contract checks fence carries a language tag ({tag}); "
                    "the factory ignores it"
                ] if tag else [])
                self.edit("KO-1", body)
                self.assertEqual(self.provider.fetch_task("KO-1")["contracts"],
                                 [("README.md", "Holophyte")])
