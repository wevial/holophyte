"""A fix round refreshes the open pull request's body and evidence."""
from __future__ import annotations

import io
import sys
from contextlib import closing
from pathlib import Path
from time import monotonic
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
sys.path.insert(0, str(HERE))
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    BRANCH,
    MergeModeFixture,
)

import holophyte.pr.github  # noqa: E402 - after the sys.path insert above
import holophyte.pr.pr_media  # noqa: E402 - after the sys.path insert above
import holophyte.pr.pr_status  # noqa: E402 - after the sys.path insert above
import holophyte.pr.pullrequest  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import ticket_template  # noqa: E402 - after the sys.path insert above
from holophyte.loop import implement  # noqa: E402


class PullRequestRefreshTests(MergeModeFixture):
    def refresh(self, answer, answered="ADDRESS: replace correlated subquery"):
        with patch.object(implement, "_timed",
                          side_effect=answer if callable(answer) else
                          lambda *args, **kwargs: answer) as turn:
            holophyte.pr.pullrequest.refresh_pr_text(
                self.project, None, None, "KO-131", "add a thing", BRANCH,
                self.BODY, 60, self.target, 5,
                holophyte.pr.pr_status.parse_pr_url(self.URL), answered)
        return turn.call_args.args[-1]

    def test_replace_preserves_production_evidence_in_either_position(self):
        evidence = "## Evidence\n\nCaptured with `capture`.  \n\n![screen](https://example/screen.png)\n\n"
        link = "Linear: KO-131 (https://linear.app/example/KO-131)"
        tail = "\n\n<!-- bot -->\nAppended block.\n"
        base = holophyte.pr.github.pr_body_written(
            "Old description.", "KO-131", "https://linear.app/example/KO-131")
        production = holophyte.pr.pr_media.append(base, evidence[:-2]) + tail
        after = base + "\n\n" + evidence + tail
        for body, expected in [
            (production, "New description.\n\n" + evidence + link + tail),
            (after, "New description.\n\n" + link + "\n\n" + evidence + tail),
            (production.replace(link, "## Old details\n\nOutdated.\n\n" + link),
             "New description.\n\n" + evidence + link + tail),
        ]:
            with self.subTest(body=body):
                self.assertEqual(holophyte.pr.github.replace_pr_text(
                    body, "New description."), expected)

    def test_replace_without_evidence_matches_existing_layout(self):
        body = holophyte.pr.github.pr_body_written(
            "Old description.", "KO-131", None)
        body += "\n\n<!-- bot -->\nAppended block.\n"
        self.assertEqual(
            holophyte.pr.github.replace_pr_text(body, "New description.\n"),
            "New description.\n\nLinear: KO-131\n\n"
            "<!-- bot -->\nAppended block.\n")

    def test_refresh_omits_history_by_default_and_removes_existing_rounds(self):
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route()
        preserved = ("## Evidence\n\n![capture](https://example/screen.png)\n\n"
                     "Linear: KO-131 (https://linear.app/example/KO-131)\n\n"
                     "<!-- bot -->\nAppended block.\n")
        for history in ("", "## Changes since first review\n"
                        "- Round 1: Results arrive sooner.\n"
                        "- Round 2: Keep rows without matches.\n\n"):
            for summary in ("", "\n\n## Changes since first review\n- Faster."):
                with self.subTest(history=history, summary=summary):
                    self.pr_body.write_text(
                        "Old description.\n\n" + history + preserved)
                    self.refresh(("TITLE: Ignored\nNew description." + summary, False))
                    self.assertEqual(self.pr_body.read_text(),
                                     "New description.\n\n" + preserved)

    def test_refresh_preserves_metadata_and_accumulates_fix_rounds(self):
        self.configure('[merge]\nmode = "pr"\npr_changes_log = true\n')
        self.fake_route()
        original = holophyte.pr.pr_media.append(
            holophyte.pr.github.pr_body_written(
                "Original subquery description.", "KO-131",
                "https://linear.app/example/KO-131"),
            "## Evidence\n\n![capture](https://example/screen.png)")
        original += "\n\n<!-- greptile_comment -->\nBot's appended block.\n"
        preserved = original[original.index("## Evidence"):]
        self.pr_body.write_text(original)
        appended = "\n<!-- new bot -->\nAppended during writing.\n"
        def write_and_append(*args, **kwargs):
            self.pr_body.write_text(self.pr_body.read_text() + appended)
            return ("TITLE: Ignored title\nGrouped join description.\n\n"
                    "## Changes since first review\n- Results arrive sooner.", False)
        prompt = self.refresh(write_and_append)
        preserved += appended
        first = self.pr_body.read_text()
        self.assertTrue(first.endswith(preserved))
        self.assertTrue(first.startswith("Grouped join description."))
        self.assertIn("## Changes since first review\n- Round 1: "
                      "Results arrive sooner.", first)
        self.assertIn("the description as it stands", prompt)
        self.assertIn("Original subquery description.", prompt)
        self.assertIn("what this fix answered", prompt)
        self.assertIn("git diff main...HEAD", prompt)
        self.refresh(("TITLE: Still ignored\nJoin with null handling.\n\n"
                      "## Changes since first review\n- Keep rows without matches.",
                      False),
                     "ADDRESS: preserve null rows")
        second = self.pr_body.read_text()
        self.assertTrue(second.endswith(preserved))
        self.assertIn("## Changes since first review\n"
                      "- Round 1: Results arrive sooner.\n"
                      "- Round 2: Keep rows without matches.", second)
        edits = [c for c in self.recorded() if c.startswith("gh pr edit")]
        self.assertEqual(edits, [f"gh pr edit {self.URL} --body-file -"] * 2)

    def captured_pr(self, exit_code=0):
        """Evidence at A; the capture logs its sha, then fails or writes one."""
        self.captures = (script := self.db.parent / "capture.py").with_name("log")
        script.write_text(
            "import subprocess, sys\nfrom pathlib import Path\nhead = subprocess"
            ".check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()\n"
            f"open({str(self.captures)!r}, 'a').write(head + '\\n')\n"
            f"if {exit_code}: raise SystemExit({exit_code})\n"
            "Path(sys.argv[1], head[:12] + '.png').write_bytes(b'png')\n")
        self.configure('[merge]\nmode = "pr"\nui_paths = ["console/**"]\n'
                       f'ui_capture = "{sys.executable} {script}"\n')
        self.fake_route()
        self.enterContext(patch.object(holophyte.pr.pr_media, "repo_is_private",
                                       return_value=False))
        self.git("checkout", "-qb", BRANCH)
        a = self.commit_file("console/app.txt")[:12]
        self.old_evidence = (f"## Evidence\n\nCaptured at {a}\n\n"
                             f"![screen](https://example/{a}.png)")
        self.pr_body.write_text(holophyte.pr.pr_media.append(holophyte.pr.github.pr_body_written(
            "Old.", "KO-131", None), self.old_evidence))
        return a

    def commit_file(self, path):
        (self.target / path).parent.mkdir(exist_ok=True)
        (self.target / path).write_text(f"{path} at {monotonic()}\n")
        self.git("add", "-A")
        self.git("commit", "-qm", path)
        return self.git("rev-parse", "HEAD").strip()

    def test_a_fix_round_touching_ui_paths_replaces_the_evidence(self):
        a = self.captured_pr()
        b = self.commit_file("console/app.txt")
        self.refresh(("TITLE: Ignored\nNew description.", False))
        body = self.pr_body.read_text()
        self.assertEqual(self.captures.read_text().split(), [b])
        self.assertEqual(body.count("## Evidence"), 1)
        self.assertIn(f"## Evidence\n\nCaptured at {b[:12]}\n\n", body)
        self.assertIn(f"/pr-media/KO-131/{b[:12]}.png)", body)
        self.assertNotIn(a, body)

    def test_a_fix_round_outside_ui_paths_keeps_the_evidence(self):
        self.captured_pr()
        self.commit_file("README.md")
        self.refresh(("TITLE: Ignored\nNew description.", False))
        body = self.pr_body.read_text()
        self.assertFalse(self.captures.exists())
        self.assertEqual(holophyte.pr.github.split_pr_body(body)[2].rstrip(),
                         self.old_evidence)

    def test_a_recapture_at_the_same_commit_replaces_the_evidence(self):
        a = self.captured_pr()
        seen = holophyte.pr.pr_media.prepare(
            self.project, self.target, "KO-131", evidence_states=
            ticket_template.parse(self.BODY).evidence_states)
        self.assertNotEqual(seen.rstrip(), self.old_evidence)
        self.refresh(("TITLE: Ignored\nNew description.", False))
        evidence = holophyte.pr.github.split_pr_body(self.pr_body.read_text())[2]
        self.assertEqual(evidence.rstrip(), seen.rstrip())
        self.assertIn(f"Captured at {a}\n\n", evidence)
        self.assertEqual(self.captures.read_text().split(),
                         [self.git("rev-parse", "HEAD").strip()])

    def test_prose_written_at_head_still_takes_the_reviewed_recapture(self):
        a = self.captured_pr()
        head = self.git("rev-parse", "HEAD").strip()
        seen = holophyte.pr.pr_media.prepare(
            self.project, self.target, "KO-131", evidence_states=
            ticket_template.parse(self.BODY).evidence_states)
        conn = self.enterContext(closing(store.open(str(self.db))))
        project = store.tickets.ensure_project(conn, "team", str(self.target))
        run = store.claim(conn, project, store.tickets.mirror_ticket(
            conn, project, "issue", "KO-131", "add a thing",
            acceptance_criteria=["works"], verification_commands=["true"]))
        store.record_event(conn, run, "pr_text_sha", head)
        with patch.object(implement, "_timed") as turn:
            holophyte.pr.pullrequest.refresh_pr_text(
                self.project, conn, run, "KO-131", "add a thing", BRANCH,
                self.BODY, 60, self.target, 5,
                holophyte.pr.pr_status.parse_pr_url(self.URL), "answered",
                sha=head)
        turn.assert_not_called()
        body = self.pr_body.read_text()
        self.assertTrue(body.startswith("Old.\n\n"))
        self.assertEqual(holophyte.pr.github.split_pr_body(body)[2].rstrip(),
                         seen.rstrip())
        self.assertIn(f"Captured at {a}\n\n", seen)
        self.assertEqual(self.captures.read_text().split(), [head])

    def test_a_failed_recapture_keeps_the_old_evidence_marked_stale(self):
        a = self.captured_pr(exit_code=3)
        b = self.commit_file("console/app.txt")
        self.refresh(("TITLE: Ignored\nNew description.", False))
        evidence = holophyte.pr.github.split_pr_body(self.pr_body.read_text())[2]
        self.assertEqual(self.captures.read_text().split(), [b])
        notice, _, rest = evidence.removeprefix("## Evidence\n\n").partition("\n\n")
        self.assertEqual(f"## Evidence\n\n{rest}".rstrip(), self.old_evidence)
        for part in (a, b[:12], "failed (exit 3)"):
            self.assertIn(part, notice)

    def test_refresh_refusal_leaves_body_untouched(self):
        self.configure('[merge]\nmode = "pr"\npr_changes_log = true\n')
        self.fake_route()
        original = ("Good description.\n\nLinear: KO-131\n\n## Evidence\n"
                    "Capture\n<!-- bot -->tail\n")
        self.pr_body.write_text(original)
        for answer, reason in [(("", True), "ran out of time"),
                               (("No title line", False), "no `TITLE:` line"),
                               (("TITLE: Valid\nNew prose", False),
                                "missing behaviour summary"),
                               (("TITLE: Valid\nNew prose\n\n"
                                 "## Changes since first review\n- \t\n\n"
                                 "## Risks\nNone.", False),
                                "missing behaviour summary")]:
            with self.subTest(reason=reason), patch(
                    "sys.stdout", new_callable=io.StringIO) as out:
                self.refresh(answer)
                self.assertEqual(self.pr_body.read_text(), original)
                self.assertEqual(len(out.getvalue().splitlines()), 1)
                self.assertIn(reason, out.getvalue())
        self.assertFalse(any(c.startswith("gh pr edit") for c in self.recorded()))
