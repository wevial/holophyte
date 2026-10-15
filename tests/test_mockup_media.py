"""The Mock-up section's media links: the grammar, the filing checks, the
download into the task worktree and the implementer's brief."""

import io
import os
import stat
import subprocess
import sys
import threading
import time
import unittest
import zipfile
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from fake_agent import APPROVE, Commit  # noqa: E402 - after the sys.path insert
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    VALID_BODY,
    LoopFixture,
    StubProvider,
    a_task,
)

import store  # noqa: E402 - after the sys.path insert above
import store.board  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
import ticket_template as tt  # noqa: E402 - after the sys.path insert above
from holophyte.board import projection  # noqa: E402 - after the sys.path insert
from holophyte.pr import mockup  # noqa: E402 - after the sys.path insert above
from tests.test_ticket_template import (  # noqa: E402 - after the insert
    APPROVAL,
    with_ui_change,
)

ARTIFACT = "https://claude.ai/artifact/3f2a9c"
BASE = "https://media.example.org/mockups"


def mockup_problems(mockup):
    problems = tt.validate(tt.parse(with_ui_change("major", mockup)))
    return [p for p in tt.blocking(problems) if "'## Mock-up'" in p]


class GrammarTests(unittest.TestCase):
    def test_a_design_link_with_snapshot_and_bundle_links_validates(self):
        for links in (f"![Board 17.1]({BASE}/a.png)\n![Gate]({BASE}/b.png)\n"
                      f"[Boards]({BASE}/boards.zip)",
                      f"{BASE}/a.png\n{BASE}/b.png\n{BASE}/boards.zip"):
            with self.subTest(links=links):
                self.assertEqual(mockup_problems(
                    f"{ARTIFACT}\n\n{APPROVAL}\n\n{links}"), [])

    def test_a_gif_link_is_refused_as_neither_kind(self):
        for link in (f"![Board]({BASE}/a.gif)", f"{BASE}/a.gif"):
            with self.subTest(link=link):
                self.assertEqual(mockup_problems(
                    f"{ARTIFACT}\n\n{APPROVAL}\n\n{link}"),
                    ["'## Mock-up' link is not a claude.ai artifact, a "
                     "Lotuspod page, a .png snapshot or a .zip mock-up "
                     f"bundle: {BASE}/a.gif"])

    def test_two_design_links_are_refused_by_count(self):
        self.assertEqual(mockup_problems(
            f"{ARTIFACT}\nhttps://claude.ai/artifact/77aa\n\n{APPROVAL}"),
            ["'## Mock-up' holds 2 design links; give exactly one"])


PNG = b"\x89PNG\r\n\x1a\n"


def bundle(members):
    """A zip of `members`: name to bytes, or a ZipInfo to bytes."""
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return data.getvalue()


BOARDS = {"project/canvas.json": b'{"boards": ["17-1", "17-2"]}',
          "project/17-1.dc.html": b"<div>Read first</div>",
          "project/17-2.dc.html": b"<div>Gate</div>",
          "project/17-1.png": PNG + b"board 17.1",
          "project/17-2.png": PNG + b"board 17.2"}


@contextmanager
def media_server(routes):
    """Serve `routes`, a path to (status, headers, body or a writer of it), or
    to (None, {}, a writer of the raw response); every other path is 404."""
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            status, headers, payload = routes.get(self.path, (404, {}, b""))
            try:
                if status is None:
                    return payload(self.wfile)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                payload(self.wfile) if callable(payload) else self.wfile.write(payload)
            except ConnectionError:
                pass

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def png(payload):
    return (200, {"Content-Type": "image/png"}, payload)


def zipped(payload):
    return (200, {"Content-Type": "application/zip"}, payload)


def major_body(mockup_section):
    return VALID_BODY.replace(
        "**How:**", "**UI change:** major\n\n**How:**").replace(
        "## In scope", f"## Mock-up\n\n{mockup_section}\n\n## In scope")


def merge_toml(base, ui_paths=True):
    ui = 'ui_paths = ["web/**"]\nui_capture = "true"\n' if ui_paths else ""
    return (f"[merge]\n{ui}\n[merge.media_bucket]\n"
            f'endpoint = "{base.split("/mockups")[0]}"\nbucket = "mockups"\n'
            f'public_base = "{base}"\n')


def mockup_section(base, links):
    return f"{ARTIFACT}\n\n{APPROVAL}\n\n" + "\n".join(
        link.replace("BASE", base) for link in links)


class MockupEventsFixture(LoopFixture):
    def events(self):
        return [summary for (summary,) in self.read(
            "SELECT summary FROM runEvents WHERE kind = 'mockup' ORDER BY seq")]


class FilingTests(LoopFixture):
    def problems(self, mockup_section):
        return [p for p in store.board.ticket_problems(
            major_body(mockup_section), self.target)
            if "Mock-up" in p or "UI change" in p]

    def test_a_major_ticket_needs_a_snapshot_under_the_bucket(self):
        self.configure(merge_toml(BASE))
        design = f"{ARTIFACT}\n\n{APPROVAL}"
        self.assertEqual(self.problems(design), [
            "'**UI change:** major' needs at least one snapshot or mock-up "
            "bundle under '## Mock-up': export the approved boards as PNGs, "
            "or as a .zip of their .dc.html sources, canvas.json and PNGs, "
            f"upload them under {BASE}, and add each below the Approved line "
            "as ![caption](URL)"])
        self.assertEqual(self.problems(
            f"{design}\n\n![Board 17.1]({BASE}/17-1.png)"), [])
        foreign = "https://elsewhere.example.org/mockups/17-1.png"
        self.assertEqual(self.problems(f"{design}\n\n![Board]({foreign})"), [
            "'## Mock-up' media link is not under the project's media bucket "
            f"(public_base {BASE}): {foreign}"])

    def test_a_bracketed_path_under_the_bucket_files(self):
        self.configure(merge_toml(BASE))
        self.assertEqual(self.problems(
            f"{ARTIFACT}\n\n{APPROVAL}\n\n![Board]({BASE}//[board/a.png)"), [])

    def test_the_repo_check_prints_the_refusal_and_exits_1(self):
        self.configure(merge_toml(BASE))
        path = self.target.parent / "ticket.md"
        path.write_text(major_body(f"{ARTIFACT}\n\n{APPROVAL}"))
        checked = subprocess.run(
            [sys.executable, str(HERE.parent / "ticket_template.py"),
             "--repo", str(self.target), str(path)],
            capture_output=True, text=True)
        self.assertEqual(checked.returncode, 1, checked.stdout + checked.stderr)
        self.assertIn("needs at least one snapshot or mock-up bundle", checked.stdout)
        self.assertIn(f"upload them under {BASE}", checked.stdout)


class FetchTests(MockupEventsFixture):
    """A real git task worktree, a real store run and a real HTTP server."""

    def setUp(self):
        super().setUp()
        self.wt = self.worktrees / "task"
        self.git("worktree", "add", "-q", "-b", "task/mockup", str(self.wt))
        self.conn = store.open(self.db)
        self.addCleanup(self.conn.close)
        project_id = store.tickets.ensure_project(self.conn, "team-1", self.target)
        ticket = store.tickets.mirror_ticket(
            self.conn, project_id, linear_issue_id="issue-1",
            linear_identifier="KO-1", title="ticket 1",
            acceptance_criteria=["Given a, when b, then c"],
            verification_commands=["true"], time_box_ms=60 * 60 * 1000)
        store.tickets.transition(self.conn, ticket, "in_flight")
        self.run_id = store.claim(self.conn, project_id, ticket)

    def fetch(self, routes, links):
        with media_server(routes) as (host, requests):
            base = f"{host}/mockups"
            self.configure(merge_toml(base, ui_paths=False))
            mockup.fetch(self.project, self.conn, self.run_id, self.wt,
                         major_body(mockup_section(base, links)))
        self.requests = requests
        return base

    def files(self, root):
        return sorted(str(path.relative_to(root)) for path in root.rglob("*")
                      if path.is_file() and ".git" not in path.parts
                      and not path.is_relative_to(self.db.parent))

    def assert_ignored(self):
        self.assertEqual(self.git("status", "--porcelain", cwd=self.wt), "")
        self.assertEqual(self.git("add", "-A", "--dry-run", cwd=self.wt), "")

    def test_snapshots_and_a_bundle_fill_the_ignored_mock_up_directory(self):
        first, second = PNG + b"board one", PNG + b"board two"
        self.fetch({"/mockups/one.png": png(first),
                    "/mockups/two.png": png(second),
                    "/mockups/boards.zip": zipped(bundle(BOARDS))},
                   ["![Read-first gate](BASE/one.png)", "BASE/two.png",
                    "[Contract boards](BASE/boards.zip)"])

        directory = self.wt / ".holophyte" / "mockup"
        self.assertEqual((directory / "01.png").read_bytes(), first)
        self.assertEqual((directory / "02.png").read_bytes(), second)
        for name, payload in BOARDS.items():
            self.assertEqual((directory / "bundle-03" / name).read_bytes(), payload)
        manifest = (directory / "manifest.json").read_text()
        for caption in ("Read-first gate", "two.png", "Contract boards"):
            self.assertIn(f'"caption": "{caption}"', manifest)
        self.assertEqual(self.events(), [
            "mock-up: 2 snapshots and 1 bundles (2 boards) in .holophyte/mockup"])
        self.assert_ignored()

    def test_an_unfetchable_link_is_a_warning_and_the_rest_still_arrive(self):
        oversized = PNG + b"\0" * (mockup.MOCKUP_IMAGE_MAX_MB * 1024 * 1024)
        base = self.fetch(
            {"/mockups/page.png": (200, {"Content-Type": "text/html"}, b"<html>"),
             "/mockups/huge.png": png(oversized),
             "/mockups/good.png": png(PNG + b"good")},
            ["BASE/missing.png", "BASE/page.png", "BASE/huge.png",
             "BASE/board\u20131.png", "BASE/good.png"])

        self.assertEqual(self.events(), [
            f"mock-up: {base}/missing.png not fetched: HTTP 404",
            f"mock-up: {base}/page.png not fetched: served as text/html, "
            "not image/png",
            f"mock-up: {base}/huge.png not fetched: larger than 10 MB",
            f"mock-up: {base}/board\u20131.png not fetched: cannot be "
            "requested (UnicodeEncodeError)",
            "mock-up: 1 snapshots and 0 bundles (0 boards) in .holophyte/mockup"])
        self.assertEqual((self.wt / ".holophyte/mockup/05.png").read_bytes(),
                         PNG + b"good")

    def test_a_trickling_link_is_cut_off_at_the_deadline(self):
        def trickle(stream):
            stream.write(PNG)
            for _ in range(60):
                stream.write(b"x")
                stream.flush()
                time.sleep(0.05)

        started = time.monotonic()
        with patch.object(mockup, "TIMEOUT_S", 0.3):
            base = self.fetch({"/mockups/slow.png": png(trickle),
                               "/mockups/good.png": png(PNG + b"good")},
                              ["BASE/slow.png", "BASE/good.png"])
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2)
        self.assertEqual(self.events()[0],
                         f"mock-up: {base}/slow.png not fetched: took longer "
                         "than 0.3 s")
        self.assertEqual((self.wt / ".holophyte/mockup/02.png").read_bytes(),
                         PNG + b"good")

    def test_trickling_headers_are_cut_off_at_the_deadline(self):
        def trickle(stream):
            stream.write(b"HTTP/1.0 200 OK\r\nContent-Type: image/png\r\nX-Slow: ")
            for _ in range(60):
                stream.write(b"x")
                stream.flush()
                time.sleep(0.05)

        started = time.monotonic()
        with patch.object(mockup, "TIMEOUT_S", 0.3):
            base = self.fetch({"/mockups/slow.png": (None, {}, trickle),
                               "/mockups/good.png": png(PNG + b"good")},
                              ["BASE/slow.png", "BASE/good.png"])
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2)
        self.assertEqual(self.events()[0],
                         f"mock-up: {base}/slow.png not fetched: took longer "
                         "than 0.3 s")
        self.assertTrue((self.wt / ".holophyte/mockup/02.png").exists())

    def test_a_bracketed_path_is_one_failed_link_not_a_failed_fetch(self):
        base = self.fetch({"/mockups/good.png": png(PNG + b"good")},
                          ["BASE//[board/a.png", "BASE/good.png"])
        self.assertEqual(self.events()[0],
                         f"mock-up: {base}//[board/a.png not fetched: HTTP 404")
        self.assertEqual((self.wt / ".holophyte/mockup/02.png").read_bytes(),
                         PNG + b"good")

    def test_a_run_without_a_store_fetches_and_warns_nowhere(self):
        self.configure(merge_toml(BASE, ui_paths=False))
        mockup.fetch(self.project, None, None, self.wt,
                     major_body(f"{ARTIFACT}\n\n{APPROVAL}"))
        self.assertEqual(self.events(), [])

    def test_a_hostile_bundle_or_redirect_is_skipped_and_writes_nothing_outside(self):
        link = zipfile.ZipInfo("project/17-1.dc.html")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        before = self.files(self.target.parent)
        base = self.fetch(
            {"/mockups/climb.zip": zipped(bundle({
                "project/canvas.json": b"{}",
                "../../../escape.html": b"<p>escaped</p>"})),
             "/mockups/link.zip": zipped(bundle({link: b"../../../../outside"})),
             "/mockups/away.png": (302, {"Location": "/elsewhere/away.png"}, b""),
             "/elsewhere/away.png": png(PNG + b"away")},
            ["BASE/climb.zip", "BASE/link.zip", "BASE/away.png"])

        self.assertEqual(self.events(), [
            f"mock-up: {base}/climb.zip not fetched: member "
            "'../../../escape.html' is not a plain relative name",
            f"mock-up: {base}/link.zip not fetched: member "
            "'project/17-1.dc.html' is not a regular file",
            f"mock-up: {base}/away.png not fetched: redirected off the media "
            f"bucket to {base.removesuffix('/mockups')}/elsewhere/away.png"])
        self.assertNotIn("/elsewhere/away.png", self.requests)
        self.assertEqual(sorted(set(self.files(self.target.parent)) - set(before)),
                         ["repo.worktrees/task/.holophyte/mockup/.gitignore",
                          "repo.worktrees/task/.holophyte/mockup/manifest.json"])
        self.assert_ignored()

    def test_a_committed_holophyte_symlink_gets_no_download(self):
        outside = self.target.parent / "outside"
        outside.mkdir()
        os.symlink("../../outside", self.wt / ".holophyte")
        self.git("add", ".holophyte", cwd=self.wt)
        self.git("commit", "-q", "-m", "link", cwd=self.wt)

        self.fetch({"/mockups/one.png": png(PNG + b"one")}, ["BASE/one.png"])

        self.assertEqual(self.events(),
                         ["mock-up: nothing fetched: .holophyte is a symlink"])
        self.assertEqual(self.requests, [])
        self.assertEqual(list(outside.iterdir()), [])


class ImplementTurnTests(MockupEventsFixture):
    def run_major(self, links, routes=None):
        with media_server(routes or {}) as (host, _):
            base = f"{host}/mockups"
            self.configure(merge_toml(base))
            fake, _ = self.loop(Commit(), APPROVE, provider=StubProvider(
                dict(a_task(), body=major_body(mockup_section(base, links)))))
        return fake.turns[0]

    def test_an_already_filed_ticket_without_media_still_claims_and_is_warned(self):
        with media_server({}) as (host, _):
            self.configure(merge_toml(f"{host}/mockups"))
            body = major_body(f"{ARTIFACT}\n\n{APPROVAL}")
            self.assertEqual(projection.body_problems(
                {"body": body}, repo=self.target), [])
        turn = self.run_major([])
        self.assertEqual(turn.role, "implement")
        self.assertNotIn("Match it: layout, copy, states", turn.goal)
        self.assertEqual(self.events(), [
            "mock-up: no snapshot or bundle; the implementer has only the "
            "design link, which it cannot open"])

    def test_the_first_implement_goal_lists_the_downloaded_design(self):
        turn = self.run_major(
            ["![Read-first gate](BASE/one.png)", "![Signed state](BASE/two.png)",
             "[Contract boards](BASE/boards.zip)"],
            {"/mockups/one.png": png(PNG + b"one"),
             "/mockups/two.png": png(PNG + b"two"),
             "/mockups/boards.zip": zipped(bundle(BOARDS))})

        self.assertEqual(turn.role, "implement")
        for line in ("- `.holophyte/mockup/01.png`: Read-first gate",
                     "- `.holophyte/mockup/02.png`: Signed state",
                     "- `.holophyte/mockup/bundle-03/project/17-1.dc.html`: "
                     "Contract boards",
                     "- `.holophyte/mockup/bundle-03/project/17-2.dc.html`: "
                     "Contract boards"):
            self.assertIn(line, turn.goal)
        self.assertIn("Match it: layout, copy, states", turn.goal)
        self.assertIn("It is design source, not code to paste", turn.goal)


if __name__ == "__main__":
    unittest.main()
