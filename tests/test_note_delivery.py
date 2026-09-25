"""In store mode the host sweep's observation posts each pending note,
oldest first, its last line naming the note's id, and `/tickets/KO-n`
serves the notes with their post state (KO-747): `observe_board()` and
`ticket_detail()` on a real store with a file board."""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sweep_fixture import MINUTE, T0, SweepTestCase  # noqa: E402

import store  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.board_sync import observe_board  # noqa: E402
from holophyte.serve import ticket_detail  # noqa: E402
from provider import FileProvider  # noqa: E402

ASK = 10 * MINUTE
STORE_MODE = ('[board]\nmode = "store"\nproject_id = "project-1"\n'
              'team = "team-1"\n')


class LostResponseBoard(FileProvider):
    """Keeps the comment, then raises, as a response lost on the way back."""

    def comment(self, task_id, body):
        super().comment(task_id, body)
        raise TimeoutError("the response was lost")


class UnaskableBoard(FileProvider):
    def states(self, identifiers):
        raise ConnectionError("the board is down")


class NoteDeliveryTests(SweepTestCase):
    def setUp(self):
        super().setUp()
        self.configure(STORE_MODE)
        self.files = self.root / "board"
        self.files.mkdir()
        self.ticket = self.a_ticket("KO-1")

    def a_ticket(self, identifier):
        (self.files / f"{identifier}.md").write_text(f"# {identifier}\n")
        return store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id=identifier,
            linear_identifier=identifier, title=identifier,
            acceptance_criteria=["Given the ticket, then it is worked"],
            verification_commands=["echo ok"], board_state="Todo")

    def a_note(self, ticket, text, at):
        return store.record_note(self.conn, ticket, "ledger", text,
                                 dedup_key=text, now=at)

    def observe(self, board, at):
        out = io.StringIO()
        observe_board(self.project, self.conn, self.project_id, board, at,
                      out, {}, ASK)
        return out.getvalue()

    def comments(self, identifier):
        """The comments the file board holds on `identifier`, in order."""
        path = self.files / f"{identifier}.comments.md"
        if not path.exists():
            return []
        return [c.strip() for c in path.read_text().split("## ")[1:]]

    def post_state(self, note):
        return self.conn.execute(
            "SELECT postedAt, postError FROM ticketNotes WHERE id = ?",
            (note,)).fetchone()

    def test_pending_notes_are_posted_once_oldest_first(self):
        later = self.a_note(self.ticket, "the second round", T0 + 2000)
        earlier = self.a_note(self.ticket, "the first round", T0 + 1000)
        board = FileProvider(self.files)
        self.observe(board, T0 + MINUTE)
        first, second = self.comments("KO-1")
        self.assertIn("the first round", first)
        self.assertEqual(first.splitlines()[-1], f"holophyte-note: {earlier}")
        self.assertIn("**2023-11-14T22:13:21Z**", first)
        self.assertIn("the second round", second)
        self.assertEqual(second.splitlines()[-1], f"holophyte-note: {later}")
        for note in (earlier, later):
            self.assertIsNotNone(self.post_state(note)[0])
        self.observe(board, T0 + 2 * ASK)
        self.assertEqual(len(self.comments("KO-1")), 2)

    def test_a_lost_response_waits_and_posts_again_with_the_id_line(self):
        note = self.a_note(self.ticket, "the round", T0)
        out = self.observe(LostResponseBoard(self.files), T0 + MINUTE)
        posted, error = self.post_state(note)
        self.assertIsNone(posted)
        self.assertEqual(error, "the response was lost")
        self.assertIn(f"note {note}", out)
        self.observe(FileProvider(self.files), T0 + 2 * ASK)
        comments = self.comments("KO-1")
        self.assertEqual(len(comments), 2)
        self.assertEqual({c.splitlines()[-1] for c in comments},
                         {f"holophyte-note: {note}"})
        posted, error = self.post_state(note)
        self.assertIsNotNone(posted)
        self.assertIsNone(error)

    def test_an_unasked_board_or_a_gone_ticket_posts_nothing(self):
        note = self.a_note(self.ticket, "the round", T0)
        self.observe(UnaskableBoard(self.files), T0 + MINUTE)
        self.assertEqual(self.comments("KO-1"), [])
        self.assertIsNone(self.post_state(note)[0])

        gone = self.a_ticket("KO-2")
        (self.files / "KO-2.md").unlink()
        store.set_gone_since(self.conn, gone, T0)
        gone_note = self.a_note(gone, "the gone round", T0)
        self.observe(FileProvider(self.files), T0 + MINUTE)
        self.assertIsNone(self.post_state(gone_note)[0])
        self.assertIsNotNone(self.post_state(note)[0])

    def test_the_ticket_route_serves_the_notes_oldest_first(self):
        later = self.a_note(self.ticket, "the second round", T0 + 2000)
        earlier = self.a_note(self.ticket, "the first round", T0 + 1000)
        self.observe(LostResponseBoard(self.files), T0 + MINUTE)
        code, body = ticket_detail(self.project, "KO-1")
        self.assertEqual(code, 200)
        first, second = body["notes"]
        self.assertEqual(
            (first["id"], first["at"], first["kind"], first["text"],
             first["posted_ms"], first["post_error"]),
            (earlier, T0 + 1000, "ledger", "the first round", None,
             "the response was lost"))
        self.assertEqual((second["id"], second["posted_ms"],
                          second["post_error"]), (later, None, None))
        self.assertEqual(first["author"], "factory")

        self.observe(FileProvider(self.files), T0 + 2 * ASK)
        _, body = ticket_detail(self.project, "KO-1")
        self.assertEqual([n["id"] for n in body["notes"]], [earlier, later])
        self.assertTrue(all(n["posted_ms"] is not None
                            and n["post_error"] is None
                            for n in body["notes"]))
