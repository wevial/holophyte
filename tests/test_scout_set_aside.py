"""Setting a done scout's action item aside and bringing it back: `POST
/projects/NAME/actions/scout-set-aside` on a host daemon over a real
temporary store, read back through `GET /scouts` and `GET /scouts/ID`.

Run: python3 -m unittest discover -s tests -p 'test_scout_set_aside.py' -v
"""
from holophyte.config.project import Project
from store.scouts import (
    create_scout,
    end_scout,
    file_scout_action,
    move_scout,
    record_scout_actions,
)
from tests.host_fixture import HostFixture
from tests.test_serve_board import NATIVE, OWN, ServeBoardCase

SET_ASIDE = "/projects/native/actions/scout-set-aside"
SCOUTS = "/projects/native/scouts"


class ScoutSetAsideTests(ServeBoardCase):
    """`native`, whose store holds one done scout with three unfiled
    action items, served by a host daemon with actions on."""

    def setUp(self):
        HostFixture.setUp(self)
        self.paths = {"native": self.repo("native")}
        own = self.root / "own.token"
        own.write_text(OWN + "\n")
        own.chmod(0o600)
        Project.locate(self.paths["native"]).config_path.write_text(
            NATIVE + f'[serve]\ntoken_file = "{own}"\n')
        self.cli("project", "add", str(self.paths["native"]))
        with self.store("native") as (conn, project):
            self.scout = create_scout(conn, project, "Map the merge gate",
                                      "recon", 30)
            move_scout(conn, self.scout, "planning", base_sha="a" * 40)
            move_scout(conn, self.scout, "swarming")
            record_scout_actions(conn, self.scout, 1, [
                {"title": f"Item {n}", "why": "found", "kind": "fix",
                 "body": f"# Item {n}\n"} for n in (1, 2, 3)])
            end_scout(conn, self.scout, "done", "answered")
        self.start_host()

    def set_aside(self, item, aside):
        status, answer, _ = self.request(
            "POST", SET_ASIDE, {"scout": self.scout, "item": item,
                                "aside": aside}, revision=None)
        self.assertEqual(status, 200, answer)
        return answer

    def to_file(self):
        status, answer, _ = self.request("GET", SCOUTS, revision=None)
        self.assertEqual(status, 200, answer)
        (row,) = answer["scouts"]
        return row["to_file"]

    def served_aside(self):
        status, answer, _ = self.request("GET", f"{SCOUTS}/{self.scout}",
                                         revision=None)
        self.assertEqual(status, 200, answer)
        return [action["set_aside_ms"] for action in answer["actions"]]

    def test_setting_item_two_aside_serves_its_time_and_drops_to_file(self):
        self.assertEqual(self.to_file(), 3)
        answer = self.set_aside(2, True)
        self.assertEqual((answer["ok"], answer["scout"], answer["item"]),
                         (True, self.scout, 2))
        self.assertIsInstance(answer["set_aside_ms"], int)
        self.assertEqual(self.served_aside(),
                         [None, answer["set_aside_ms"], None])
        self.assertEqual(self.to_file(), 2)

    def test_aside_false_clears_the_time_and_restores_to_file(self):
        self.set_aside(2, True)
        answer = self.set_aside(2, False)
        self.assertEqual((answer["ok"], answer["set_aside_ms"]), (True, None))
        self.assertEqual(self.served_aside(), [None, None, None])
        self.assertEqual(self.to_file(), 3)

    def test_a_filed_or_unknown_item_is_refused_and_nothing_changes(self):
        with self.store("native") as (conn, _):
            file_scout_action(conn, self.scout, 1, "NAT-7")
        before = (self.served_aside(), self.to_file())
        for item, aside, detail in (
                (1, True, "item 1 is already filed as NAT-7"),
                (1, False, "item 1 is already filed as NAT-7"),
                (4, True, f"scout {self.scout} has no item 4")):
            with self.subTest(item=item, aside=aside):
                answer = self.set_aside(item, aside)
                self.assertEqual((answer["ok"], answer["scout"],
                                  answer["item"]), (False, self.scout, item))
                self.assertIn(detail, answer["detail"])
                self.assertEqual((self.served_aside(), self.to_file()),
                                 before)
