"""A pull request's diff size, preview link and Evidence screenshots, from
the reconcile's GraphQL read to the `pr` facts `/attention` serves."""
import unittest
from pathlib import Path
from unittest.mock import patch

import store
import store.read
import store.schema
import store.tickets
from holophyte.host import reconcile
from holophyte.pr import pr_media, pr_status
from tests.phase_fixture import park_run
from tests.serve_fixture import MIN, ServeTestCase

URL = "https://github.com/example/repo/pull/2170"
SHOTS = (("01 split view", "https://media.example/01-split-view.png"),
         ("02 custom slot", "https://media.example/02-custom-slot.png"),
         ("03 saved document", "https://media.example/03-saved-document.png"))
BODY = ("Splits the editor so a custom slot sits beside the document.\n\n"
        "![sketch](https://media.example/sketch.png)\n\n"
        "Ticket: KO-7\n\n## Evidence\n\n"
        + "".join(f"![{caption}]({url})\n" for caption, url in SHOTS))


def deployment(state, url):
    return {"latestStatus": {"state": state, "environmentUrl": url}}


SIZED = {"state": "OPEN", "merged": False, "mergeCommit": None,
         "mergedBy": None, "updatedAt": "2026-10-19T10:00:00Z",
         "title": "Split the editor", "isDraft": False,
         "additions": 412, "deletions": 88, "changedFiles": 9, "body": BODY,
         "head": {"nodes": [{"commit": {"deployments": {"nodes": [
             deployment("SUCCESS", "https://preview.example"),
             deployment("FAILURE", "https://broken.example")]}}}]}}
BARE = {"state": "OPEN", "merged": False, "mergeCommit": None,
        "mergedBy": None, "updatedAt": "2026-10-19T10:00:00Z",
        "title": "Split the editor", "isDraft": False,
        "body": "Splits the editor.\n\nTicket: KO-7\n",
        "head": {"nodes": [{"commit": {"deployments": {"nodes": []}}}]}}


def answer(node):
    return {"repository": {"pullRequest": node}}


class PullStatusMediaTests(unittest.TestCase):
    def read(self, node):
        with patch.object(pr_status, "graphql", return_value=answer(node)):
            return pr_status.pull_status(None, pr_status.parse_pr_url(URL))

    def test_size_newest_successful_preview_and_evidence_images_are_read(self):
        status = self.read(SIZED)
        self.assertEqual((status.additions, status.deletions,
                          status.changed_files), (412, 88, 9))
        self.assertEqual(status.preview_url, "https://preview.example")
        self.assertEqual(status.screenshots, SHOTS)

    def test_no_deployment_evidence_or_size_reads_as_absent(self):
        status = self.read(BARE)
        self.assertIsNone(status.preview_url)
        self.assertEqual(status.screenshots, ())
        self.assertEqual((status.additions, status.deletions,
                          status.changed_files), (None, None, None))

    def test_escaped_captions_and_parenthesized_urls_survive_the_parse(self):
        urls = {Path("01-panel.png"): "https://media.example/panel_(2).png"}
        lines, _ = pr_media._state_media(["Panel [open]"], urls)
        body = "Ticket: KO-7\n\n## Evidence\n\n" + "\n".join(lines)
        status = self.read({**BARE, "body": body})
        self.assertEqual(status.screenshots, (
            ("Panel [open]", "https://media.example/panel_(2).png"),))


    def test_evidence_video_links_are_read_beside_its_images(self):
        body = ("Splits the editor; see [demo](https://media.example/demo.webm)."
                "\n\nTicket: KO-7\n\n## Evidence\n\n"
                + "".join(f"![{caption}]({url})\n" for caption, url in SHOTS[:2])
                + "[video-01.webm](https://media.example/a/video-01.webm)\n"
                "[clip.webm](https://github.com/o/r/blob/pr-media/clip.webm"
                "?raw=true)\n[notes](https://media.example/notes.html)\n")
        status = self.read({**BARE, "body": body})
        self.assertEqual(status.screenshots, SHOTS[:2])
        self.assertEqual(getattr(status, "videos", None), (
            ("video-01.webm", "https://media.example/a/video-01.webm"),
            ("clip.webm",
             "https://github.com/o/r/blob/pr-media/clip.webm?raw=true")))


class AttentionMediaTests(ServeTestCase):
    def setUp(self):
        super().setUp()
        self.seed()
        self.conn = store.open(str(self.db))
        self.addCleanup(self.conn.close)
        ticket = store.read.ticket_by_identifier(self.conn, "KO-7")
        store.tickets.transition(self.conn, ticket.id, "blocked_on_operator")
        self.conn.execute("UPDATE tickets SET blockedQuestion = ? WHERE id = ?",
                          (f"PR open: {URL}\nreview requested", ticket.id))
        self.conn.commit()
        park_run(self.conn, self.run, "awaiting_merge_approval", "PR open",
                 candidate_sha="a" * 40, pr_url=URL,
                 park_kind="pull_request", now=self.now - MIN)

    def pull_request_facts(self):
        _, _, body = self.request("GET", "/attention")
        (item,) = [item for item in body["items"] if item["kind"] == "pr_open"]
        return item["pr"]

    def test_a_reconciled_read_serves_size_preview_and_screenshots(self):
        self.start(config='[merge]\npr_poll_sec = 3600\n')
        project = store.tickets.ensure_project(self.conn, "team-1", self.target)
        with patch.object(pr_status, "graphql", return_value=answer(SIZED)), \
                patch.object(reconcile, "time", return_value=self.now / 1000):
            reconcile._reconcile_pull_requests(self.project, self.conn, project,
                                               None, failed_asked={})
        facts = self.pull_request_facts()
        self.assertEqual(
            {key: facts[key] for key in ("additions", "deletions",
                                         "changed_files", "preview_url")},
            {"additions": 412, "deletions": 88, "changed_files": 9,
             "preview_url": "https://preview.example"})
        self.assertEqual(facts["screenshots"],
                         [{"caption": caption, "url": url}
                          for caption, url in SHOTS])

    def test_a_store_at_the_previous_version_migrates_and_serves_none(self):
        added = ("prSeenAdditions", "prSeenDeletions", "prSeenChangedFiles",
                 "prSeenPreviewUrl", "prSeenScreenshots")
        for column in added:
            self.conn.execute(f"ALTER TABLE runs DROP COLUMN {column}")
        self.conn.execute(
            f"PRAGMA user_version = {store.schema.SCHEMA_VERSION - 1}")
        self.conn.commit()
        store.open(str(self.db)).close()
        columns = {row[1] for row in
                   self.conn.execute("PRAGMA table_info(runs)")}
        self.assertLessEqual(set(added), columns)
        self.start()
        facts = self.pull_request_facts()
        self.assertEqual(
            {key: facts[key] for key in ("additions", "deletions",
                                         "changed_files", "preview_url",
                                         "screenshots")},
            {"additions": None, "deletions": None, "changed_files": None,
             "preview_url": None, "screenshots": []})
