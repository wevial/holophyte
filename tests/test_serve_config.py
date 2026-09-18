"""`GET /config` and `PUT /config` (`holophyte.serve_config`): the file
redacted on the way out, the secret put back and the document held to the
loader on the way in, and the `patch` form edited in place.

Run: python3 -m unittest discover -s tests -p 'test_serve_config*' -v
"""
from __future__ import annotations

import difflib
import io
import os
import stat
import sys
import tomllib
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import test_serve  # noqa: E402 - after the insert; TokenTests' TOKEN and BEARER
from serve_fixture import ServeTestCase  # noqa: E402 - after the insert

import holophyte.agents  # noqa: E402 - after the sys.path insert above
import holophyte.config  # noqa: E402 - after the sys.path insert above
import holophyte.config_tables  # noqa: E402 - after the sys.path insert above
import holophyte.serve  # noqa: E402 - after the sys.path insert above
import holophyte.serve_config  # noqa: E402 - after the sys.path insert above
import holophyte.target  # noqa: E402 - after the sys.path insert above
import store.read  # noqa: E402 - after the sys.path insert above


class ConfigEditTests(ServeTestCase):
    """Authenticated config reads, validation, and persisted edits."""

    TOKEN = test_serve.TokenTests.TOKEN
    BEARER = test_serve.TokenTests.BEARER
    SECRET = "lin_api_0123456789abcdef"

    HOOK_TOKENS = ("hook_0123", "hook_4567")

    def config(self, extra="", loop="[loop]\nworkers = 2\n"):
        """A file `config.check_document()` accepts as written: `[serve]`
        and `[loop]` are loader-read tables, `[linear]` and `[[hooks]]`
        (an array of tables) are left alone by this version and hold the
        secrets, `[worktree] setup` is a table the loader parses."""
        path = self.root / "serve.token"
        path.write_text(self.TOKEN + "\n")
        path.chmod(0o600)
        return (f'[serve]\ntoken_file = "{path}"\n{extra}'
                f'\n{loop}\n[worktree]\nsetup = ["make deps"]\n'
                f'\n[linear]\napi_key = "{self.SECRET}"  # board\n'
                f'\n[[hooks]]\ntoken = "{self.HOOK_TOKENS[0]}"\n'
                f'[[hooks]]\ntoken = "{self.HOOK_TOKENS[1]}"\n')

    def redacted(self, text):
        """`text` with every secret the fixture placed replaced, the
        expected reply computed from the fixture's own literal secrets
        rather than from the redaction under test."""
        for secret in (self.SECRET, *self.HOOK_TOKENS):
            text = text.replace(f'"{secret}"', '"[redacted]"')
        return text

    def assert_loader_valid(self, text):
        """`text`, on disk, is a document the loop's startup accepts."""
        (self.db.parent / "config.toml").write_text(text)
        tgt = holophyte.target.Target.locate(self.target)
        self.assertIsNone(holophyte.config.check_document(tgt))

    def on_disk(self):
        return (self.db.parent / "config.toml").read_text()

    def test_without_the_opt_in_both_routes_are_404_with_the_token(self):
        self.seed()
        before = self.config()
        self.start(before, host="0.0.0.0")
        code, _, body = self.request("GET", "/config", self.BEARER)
        self.assertEqual((code, body["error"]), (404, "not found"))
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": "[loop]\nworkers = 3\n"})
        self.assertEqual((code, body["error"]), (404, "not found"))
        self.assertEqual(self.on_disk(), before)
        self.assertEqual(list(self.db.parent.glob("config.toml.bak-*")), [])

    def test_status_advertises_config_edit(self):
        # KO-358: the console's settings sheet reads `config_edit` from
        # `/status` before it offers a Save.
        self.seed()
        self.start(self.config("config_edit = true\n"))
        code, _, body = self.request("GET", "/status")
        self.assertEqual(code, 200)
        self.assertIs(body["config_edit"], True)

    def test_get_redacts_secret_values_and_keeps_the_token_file_path(self):
        """The route over a file startup accepts: the reply is the file
        with the two secret shapes -- a nested key and an array-of-tables
        entry -- redacted and the `token_file` path, comment and layout
        byte for byte as written."""
        self.seed()
        before = self.config("config_edit = true\n")
        self.assert_loader_valid(before)
        self.start(before)
        code, _, body = self.request("GET", "/config")
        self.assertEqual((code, body), (401, {}))
        code, _, body = self.request("GET", "/config", self.BEARER)
        self.assertEqual(code, 200)
        for secret in (self.SECRET, *self.HOOK_TOKENS, self.TOKEN):
            self.assertNotIn(secret, self.raw_body, secret)
        expected = self.redacted(before)
        self.assertNotEqual(expected, before)
        self.assertEqual(body["text"], expected)
        self.assertIn(f'token_file = "{self.root / "serve.token"}"',
                      body["text"])
        self.assertEqual(body["text"].count("[redacted]"), 3)
        self.assertEqual(body["path"], str(self.db.parent / "config.toml"))
        self.assertEqual(body["applies"], "next loop start")

    def test_a_document_startup_refuses_for_its_carry_is_400(self):
        """`[worktree] carry = ["../outside"]` passes no startup; the
        first candidate omitted the check and wrote it (review, P1)."""
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        text = before.replace('setup = ["make deps"]',
                              'setup = ["make deps"]\ncarry = ["../outside"]')
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": text})
        self.assertEqual(code, 400, body)
        self.assertIn("[worktree] carry", body["error"])
        self.assertEqual(self.on_disk(), before)

    def test_a_table_that_is_not_a_table_is_400_naming_it(self):
        """`worktree = "invalid"` reached the loader's first `.get()` as
        a string: a traceback in the handler and a dropped connection
        instead of the 400 (review, P2). Now the loader's own sentence."""
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        for text, table in (
            ('worktree = "invalid"\n'
             + before.replace('[worktree]\nsetup = ["make deps"]\n', ""),
             "[worktree]"),
            ("agents = 3\n" + before, "[agents]"),
        ):
            with self.subTest(table=table):
                code, _, body = self.request("PUT", "/config", self.BEARER,
                                             body={"text": text})
                self.assertEqual(code, 400, body)
                self.assertIn(f"{table} must be a table", body["error"])
                self.assertEqual(self.on_disk(), before)

    def test_a_document_the_loader_refuses_is_400_and_leaves_the_file(self):
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before, host="0.0.0.0")
        code, _, body = self.request(
            "PUT", "/config", self.BEARER,
            body={"text": before.replace("workers = 2", "workers = 0")})
        self.assertEqual(code, 400)
        self.assertIs(body["ok"], False)
        self.assertIn("[loop] workers", body["error"])
        self.assertEqual(self.on_disk(), before)
        self.assertEqual(list(self.db.parent.glob("config.toml.bak-*")), [])
        conn = store.read.open_readonly(self.db)
        try:
            self.assertEqual(store.read.ledger(conn, self.run), [])
        finally:
            conn.close()

    def test_a_valid_put_keeps_the_secret_backs_up_and_records(self):
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        _, _, shown = self.request("GET", "/config", self.BEARER)
        edited = shown["text"].replace("workers = 2", "workers = 3")
        self.assertIn("[redacted]", edited)
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": edited})
        self.assertEqual(code, 200, body)
        self.assertIs(body["ok"], True)
        after = self.on_disk()
        self.assertIn("workers = 3", after)
        self.assertIn(f'api_key = "{self.SECRET}"  # board', after)
        self.assertNotIn("[redacted]", after)
        backup = Path(body["backup"])
        self.assertEqual(backup.parent, self.db.parent)
        self.assertTrue(backup.name.startswith("config.toml.bak-"))
        self.assertEqual(backup.read_text(), before)
        conn = store.read.open_readonly(self.db)
        try:
            rows = conn.execute(
                'SELECT runId, source, "trigger", "action" FROM interventions'
                " WHERE action != 'migrate'").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [(self.run, "human", "manual", "config_edit")])
        self.assertEqual(body["recorded"], self.run)
        tgt = holophyte.target.Target.locate(self.target)
        self.assertEqual(holophyte.config_tables.loop_config(tgt).workers, 3)

    def test_settings_sheet_pr_keys_are_accepted_and_persisted(self):
        self.seed()
        before = self.config("config_edit = true\n") + (
            '\n[board]\nproject_id = "project"\nteam = "team"\n')
        self.start(before)
        for key, value in (
            ("merge.human_threads", "act"),
            ("merge.pr_style", "Explain the user impact."),
            ("merge.pr_rounds", 3),
            ("merge.pr_poll_sec", 60),
            ("merge.pr_quiet_sec", 120),
            ("merge.check_wait_sec", 3600),
            ("board.label", "holophyte"),
        ):
            with self.subTest(key=key):
                code, _, body = self.request(
                    "PUT", "/config", self.BEARER,
                    body={"patch": {key: value}})
                self.assertEqual(code, 200, body)
                self.assertIs(body["ok"], True)
                table, name = key.split(".")
                _, _, shown = self.request("GET", "/config", self.BEARER)
                self.assertEqual(shown["values"][table][name], value)
                self.assertEqual(tomllib.loads(self.on_disk())[table][name],
                                 value)

    def test_retired_pr_text_is_refused_without_changing_the_file(self):
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        code, _, body = self.request(
            "PUT", "/config", self.BEARER,
            body={"patch": {"merge.pr_text": "written"}})
        self.assertEqual(code, 400, body)
        self.assertIn("pr_text", body["error"])
        self.assertEqual(self.on_disk(), before)

    def test_a_redacted_value_the_file_never_held_is_400(self):
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        text = before + '\n[other]\ntoken = "[redacted]"\n'
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": text})
        self.assertEqual(code, 400)
        self.assertIn("[other] token", body["error"])
        self.assertEqual(self.on_disk(), before)

    def shapes_config(self):
        path = self.root / "serve.token"
        path.write_text(self.TOKEN + "\n")
        path.chmod(0o600)
        return (
            f'[serve]\ntoken_file = "{path}"\nconfig_edit = true\n'
            '[plain]\ntoken = "S-serve"\n'
            '[quoted]\n"api key" = "S-quoted" # comment\n'
            '[dotted]\nkeep.name = "shown"\n'
            "[inline]\nboard = { api_key = 'S-inline', team = \"t\" }\n"
            '[multi]\ntoken = """\nline one\nline two"""\n'
            "[literal]\nkey = 'S-literal'\n"
            '[[many]]\ntoken = "S-first"\n[[many]]\ntoken = "S-second"\n')

    def test_get_redacts_every_toml_shape_a_secret_can_take(self):
        """Quoted, dotted and inline-table keys, multi-line and literal
        strings, arrays of tables: `tomllib` over the shown text is the
        oracle -- every secret leaf reads `[redacted]`, nothing else moved."""
        self.seed()
        before = self.shapes_config().replace(
            'keep.name = "shown"', 'keep.name = "shown"\nkeep.api_key = "S-dotted"')
        self.assert_loader_valid(before)
        self.start(before)
        code, _, body = self.request("GET", "/config", self.BEARER)
        self.assertEqual(code, 200, body)
        for secret in ("S-serve", "S-quoted", "S-dotted", "S-inline",
                       "line one", "S-literal", "S-first", "S-second",
                       self.TOKEN):
            self.assertNotIn(secret, self.raw_body, secret)
        shown = tomllib.loads(body["text"])
        expected = tomllib.loads(before)
        self.assertEqual(shown["plain"]["token"], "[redacted]")
        self.assertEqual(shown["quoted"]["api key"], "[redacted]")
        self.assertEqual(shown["dotted"]["keep"]["api_key"], "[redacted]")
        self.assertEqual(shown["inline"]["board"]["api_key"], "[redacted]")
        self.assertEqual(shown["multi"]["token"], "[redacted]")
        self.assertEqual(shown["literal"]["key"], "[redacted]")
        self.assertEqual([m["token"] for m in shown["many"]],
                         ["[redacted]", "[redacted]"])
        # The rest of the document is untouched, comment included.
        self.assertEqual(shown["serve"]["token_file"],
                         expected["serve"]["token_file"])
        self.assertEqual(shown["inline"]["board"]["team"], "t")
        self.assertEqual(shown["dotted"]["keep"]["name"], "shown")
        self.assertIn('"api key" = "[redacted]" # comment', body["text"])

    def test_a_placeholder_with_a_comment_or_other_quoting_is_restored(self):
        """`api_key = "[redacted]" # kept` and `'[redacted]'` are the
        placeholder too: what is written carries the secret, not them."""
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        edited = before.replace(
            f'api_key = "{self.SECRET}"  # board',
            "api_key = '[redacted]' # kept")
        edited += '\n[extra]\nnote = "x"\n'
        edited = edited.replace('\n[extra]', '\n[linear.more]\n'
                                'key = "[redacted]" # also kept\n[extra]')
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": edited})
        # `[linear.more] key` was never held: refused, named, nothing written.
        self.assertEqual(code, 400, body)
        self.assertIn("[linear.more] key", body["error"])
        self.assertEqual(self.on_disk(), before)
        edited = before.replace(
            f'api_key = "{self.SECRET}"  # board',
            "api_key = '[redacted]' # kept")
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": edited})
        self.assertEqual(code, 200, body)
        after = self.on_disk()
        self.assertIn(f'api_key = "{self.SECRET}" # kept', after)
        self.assertNotIn("[redacted]", after)
        self.assertEqual(tomllib.loads(after)["linear"]["api_key"],
                         self.SECRET)

    def test_a_document_startup_refuses_for_its_board_is_400(self):
        """`[board] project_id = 123` passes no startup; it passes no PUT."""
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        text = before + '\n[board]\nproject_id = 123\nteam = "T"\n'
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": text})
        self.assertEqual(code, 400, body)
        self.assertIn("[board] project_id", body["error"])
        self.assertEqual(self.on_disk(), before)

    def test_a_secret_inside_an_array_is_redacted_and_restored_in_place(self):
        """`items = [{token = "S"}, 1]` is a secret in an array element; the
        earlier walk stepped over arrays and served it. Redacted on the way
        out, and put back by its position on the way in, whatever the value
        beside it became."""
        self.seed()
        before = self.config("config_edit = true\n") + (
            '\n[extra]\nitems = [{token = "S-array", n = 1}, 1,'
            ' [{key = "S-nested"}]]\n')
        self.start(before)
        code, _, body = self.request("GET", "/config", self.BEARER)
        self.assertEqual(code, 200, body)
        self.assertNotIn("S-array", self.raw_body)
        self.assertNotIn("S-nested", self.raw_body)
        shown = tomllib.loads(body["text"])["extra"]["items"]
        self.assertEqual(shown[0], {"token": "[redacted]", "n": 1})
        self.assertEqual(shown[2], [{"key": "[redacted]"}])
        edited = body["text"].replace("n = 1", "n = 2")
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": edited})
        self.assertEqual(code, 200, body)
        after = tomllib.loads(self.on_disk())["extra"]["items"]
        self.assertEqual(after, [{"token": "S-array", "n": 2}, 1,
                                 [{"key": "S-nested"}]])

    def test_a_placeholder_in_an_array_of_tables_takes_its_own_entry(self):
        """Two `[[many]]` entries, the first token rewritten by hand and the
        second left as the placeholder: the second gets its own secret back,
        not the first's. Values are matched by array position, not by the
        order the placeholders happen to appear."""
        self.seed()
        before = self.config("config_edit = true\n") + (
            '\n[[many]]\ntoken = "S-first"\n[[many]]\ntoken = "S-second"\n')
        self.start(before)
        edited = before.replace('token = "S-first"', 'token = "S-new"').replace(
            'token = "S-second"', 'token = "[redacted]"')
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": edited})
        self.assertEqual(code, 200, body)
        self.assertEqual([m["token"] for m in tomllib.loads(self.on_disk())["many"]],
                         ["S-new", "S-second"])

    def test_an_unquotable_agent_command_is_400_naming_the_key(self):
        """`implementer = "echo '"` has no closing quotation: 400 with the
        key in the sentence, nothing written -- not a request that dies."""
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        text = before + "\n[agents]\nimplementer = \"echo '\"\n"
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": text})
        self.assertEqual(code, 400, body)
        self.assertIn("[agents] implementer", body["error"])
        self.assertIn("quotation", body["error"])
        self.assertEqual(self.on_disk(), before)

    def test_a_relative_agent_command_path_is_400_as_at_startup(self):
        """Startup refuses `./worker` (rounds run in a worktree that does not
        exist yet); the document check holds the PUT to the same rule."""
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        text = before + '\n[agents]\nimplementer = "./worker --fast"\n'
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": text})
        self.assertEqual(code, 400, body)
        self.assertIn("[agents] implementer", body["error"])
        self.assertIn("relative", body["error"])
        self.assertEqual(self.on_disk(), before)

    def implementer_script(self, body):
        """A real route the daemon really runs for the probe: the fake
        under test is not the command, so the verdict is the process's."""
        path = self.root / "implementer.sh"
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
        return path

    def test_a_changed_implementer_is_probed_and_the_reply_says_it_answered(self):
        """`PUT /config` setting `[agents] implementer` runs the startup
        probe on the written document (KO-357): `probe.ok` with the exact
        command beside the write. A write that leaves the key alone carries
        `probe: null` -- nothing ran."""
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        path = self.implementer_script('echo "ready"\n')
        text = before + f'\n[agents]\nimplementer = "{path}"\n'
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": text})
        self.assertEqual(code, 200, body)
        self.assertIs(body["probe"]["ok"], True)
        self.assertEqual(body["probe"]["command"],
                         [str(path), holophyte.agents.PROBE_GOAL])
        self.assertEqual(self.on_disk(), text)
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": text.replace(
                                         "workers = 2", "workers = 3")})
        self.assertEqual(code, 200, body)
        self.assertIsNone(body["probe"])

    def test_a_route_that_does_not_answer_is_reported_but_the_write_lands(self):
        """The probe reports, it does not gate: the file and its backup are
        already in place, and the reply carries the exit code and the
        route's last lines so the operator can fix the key or restore."""
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        path = self.implementer_script("echo broken harness >&2\nexit 1\n")
        text = before + f'\n[agents]\nimplementer = "{path}"\n'
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": text})
        self.assertEqual(code, 200, body)
        self.assertIs(body["ok"], True)
        self.assertIs(body["probe"]["ok"], False)
        self.assertEqual(body["probe"]["returncode"], 1)
        self.assertIs(body["probe"]["timed_out"], False)
        self.assertIn("broken harness", "\n".join(body["probe"]["output"]))
        self.assertEqual(self.on_disk(), text)
        self.assertEqual(Path(body["backup"]).read_text(), before)

    def test_a_route_that_cannot_start_is_reported_but_the_write_lands(self):
        """A command that does not exist passes the document check (it is
        absolute) and fails only at launch. The write has already landed,
        so the reply must still carry `probe` -- naming the launch error --
        rather than the request failing after the file was replaced."""
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        missing = self.root / "no-such-harness"
        text = before + f'\n[agents]\nimplementer = "{missing}"\n'
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": text})
        self.assertEqual(code, 200, body)
        self.assertIs(body["ok"], True)
        self.assertIs(body["probe"]["ok"], False)
        self.assertIs(body["probe"]["timed_out"], False)
        self.assertIsNone(body["probe"]["returncode"])
        self.assertEqual(body["probe"]["command"],
                         [str(missing), holophyte.agents.PROBE_GOAL])
        self.assertIn("No such file", body["probe"]["launch_error"])
        self.assertEqual(self.on_disk(), text)
        self.assertEqual(Path(body["backup"]).read_text(), before)

    def test_the_backup_keeps_the_file_s_mode(self):
        """A mode-0600 file's backup holds the same secrets, so it is
        created 0600 too, whatever the umask says for a new file."""
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        path = self.db.parent / "config.toml"
        path.chmod(0o600)
        was = os.umask(0o022)
        self.addCleanup(os.umask, was)
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": before})
        self.assertEqual(code, 200, body)
        mode = stat.S_IMODE(Path(body["backup"]).stat().st_mode)
        self.assertEqual(oct(mode), oct(0o600))
        self.assertEqual(oct(stat.S_IMODE(path.stat().st_mode)), oct(0o600))

    def test_two_writes_in_one_second_keep_two_backups(self):
        """`write_config()` twice with the same clock: each previous text
        is in a backup of its own and the file is the second write's."""
        self.seed()
        first = self.config("config_edit = true\n")
        (self.db.parent / "config.toml").write_text(first)
        tgt = holophyte.target.Target.locate(self.target)
        when = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
        second = first.replace("workers = 2", "workers = 3")
        third = first.replace("workers = 2", "workers = 4")
        code, one = holophyte.serve_config.write_config(tgt, {"text": second}, when)
        self.assertEqual(code, 200, one)
        code, two = holophyte.serve_config.write_config(tgt, {"text": third}, when)
        self.assertEqual(code, 200, two)
        self.assertNotEqual(one["backup"], two["backup"])
        self.assertEqual(Path(one["backup"]).read_text(), first)
        self.assertEqual(Path(two["backup"]).read_text(), second)
        self.assertEqual(self.on_disk(), third)
        self.assertEqual(
            sorted(p.name for p in self.db.parent.glob("config.toml.*")),
            ["config.toml.bak-20260910T120000Z",
             "config.toml.bak-20260910T120000Z-2"])

    def test_config_edit_without_a_token_file_is_a_startup_error(self):
        self.seed()
        (self.db.parent / "config.toml").write_text(
            "[serve]\nconfig_edit = true\n")
        tgt = holophyte.target.Target.locate(self.target)
        with self.assertRaises(SystemExit) as raised:
            holophyte.serve.serve(tgt, "127.0.0.1:0", out=io.StringIO())
        message = str(raised.exception)
        self.assertIn("[serve] token_file", message)
        self.assertIn("config_edit", message)


class ConfigPatchTests(ServeTestCase):
    """`PUT /config` with `{"patch": ...}` and `values` on `GET /config`
    (KO-364): the file edited in place with `tomlkit`, so what the
    console cannot rewrite -- a comment, a multi-line array, a
    triple-quoted string, a quoted table -- survives or is read as plain
    JSON. The fixture is `ConfigEditTests`' with comments around every
    value a patch touches and a multi-line `[worktree] setup`; the
    helpers are borrowed, the tests are not."""

    TOKEN = ConfigEditTests.TOKEN
    BEARER = ConfigEditTests.BEARER
    SECRET = ConfigEditTests.SECRET
    HOOK_TOKENS = ConfigEditTests.HOOK_TOKENS
    assert_loader_valid = ConfigEditTests.assert_loader_valid
    on_disk = ConfigEditTests.on_disk

    def config(self, extra="", loop="[loop]\nworkers = 2\n"):
        text = ConfigEditTests.config(self, extra, loop)
        text = text.replace("workers = 2\n", "workers = 2  # two seats\n")
        return text.replace(
            'setup = ["make deps"]\n',
            "# what a fresh worktree runs first\nsetup = [\n"
            '  "make deps",  # the toolchain\n]\n')

    def changed_lines(self, before, after):
        """The `-`/`+` lines of a zero-context unified diff, so a test
        states exactly which lines a patch may touch."""
        return [line for line in difflib.unified_diff(
                    before.splitlines(), after.splitlines(), n=0, lineterm="")
                if line[:1] in "+-" and not line.startswith(("+++", "---"))]

    def test_a_patch_changes_only_its_values_and_keeps_every_comment(self):
        self.seed()
        before = self.config("config_edit = true\n")
        self.assert_loader_valid(before)
        self.start(before)
        code, _, body = self.request(
            "PUT", "/config", self.BEARER,
            body={"patch": {"loop.workers": 3,
                            "worktree.setup": ["make deps", "make lint"]}})
        self.assertEqual(code, 200, body)
        after = self.on_disk()
        self.assertEqual(self.changed_lines(before, after),
                         ["-workers = 2  # two seats",
                          "+workers = 3  # two seats",
                          '+  "make lint",'])
        expected = tomllib.loads(before)
        expected["loop"]["workers"] = 3
        expected["worktree"]["setup"].append("make lint")
        self.assertEqual(tomllib.loads(after), expected)
        self.assertEqual(Path(body["backup"]).read_text(), before)
        conn = store.read.open_readonly(self.db)
        try:
            rows = conn.execute(
                'SELECT "action" FROM interventions'
                " WHERE action != 'migrate'").fetchall()
            notes = conn.execute(
                "SELECT summary FROM runEvents WHERE summary LIKE"
                " '%PUT /config patch%'").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [("config_edit",)])
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("loop.workers, worktree.setup", notes[0][0])

    def test_a_patch_into_an_inline_table_keeps_it_inline(self):
        """`loop = { workers = 2 }` is a table to the loader, so it is one
        to a patch: the value changes in place and the line keeps its
        braces, its other entries and its comment."""
        self.seed()
        before = ("loop = { workers = 2, tick_sec = 30 }  # one line\n"
                  + self.config("config_edit = true\n", loop=""))
        self.assert_loader_valid(before)
        self.start(before)
        code, _, body = self.request(
            "PUT", "/config", self.BEARER, body={"patch": {"loop.workers": 3}})
        self.assertEqual(code, 200, body)
        after = self.on_disk()
        self.assertEqual(self.changed_lines(before, after),
                         ["-loop = { workers = 2, tick_sec = 30 }  # one line",
                          "+loop = { workers = 3, tick_sec = 30 }  # one line"])

    def test_shortening_an_array_removes_only_the_entry_and_its_own_line(self):
        """An entry taken out of a multi-line array leaves with the
        inline comment on its line -- it described that entry -- and
        nothing else: the comment above the array, the one on the entry
        kept and a full-line comment between entries all survive."""
        self.seed()
        before = self.config("config_edit = true\n").replace(
            '  "make deps",  # the toolchain\n',
            '  "make deps",  # the toolchain\n  # then the checks\n'
            '  "make lint",  # the linter\n')
        self.assert_loader_valid(before)
        self.start(before)
        code, _, body = self.request(
            "PUT", "/config", self.BEARER,
            body={"patch": {"worktree.setup": ["make deps"]}})
        self.assertEqual(code, 200, body)
        after = self.on_disk()
        self.assertEqual(self.changed_lines(before, after),
                         ['-  "make lint",  # the linter'])
        self.assertEqual(tomllib.loads(after)["worktree"]["setup"],
                         ["make deps"])

    def test_a_patch_the_loader_refuses_is_400_naming_the_key(self):
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"patch": {"loop.workers": 0}})
        self.assertEqual(code, 400, body)
        self.assertIs(body["ok"], False)
        self.assertIn("[loop] workers", body["error"])
        self.assertEqual(self.on_disk(), before)
        self.assertEqual(list(self.db.parent.glob("config.toml.bak-*")), [])

    def test_a_patch_creates_a_missing_table_and_refuses_an_unknown_one(self):
        """`[report]` is not in the fixture and is created; `[linear]` is
        in the file but not a table the loader reads, `workers` names no
        table, and a float is no patch value: each 400 names the key."""
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        for edit, key in (({"linear.api_key": "x"}, "linear.api_key"),
                           ({"workers": 3}, "workers"),
                           ({"loop.workers": 2.5}, "loop.workers"),
                           ({"worktree.setup": ["ok", 1]}, "worktree.setup")):
            with self.subTest(key=key):
                code, _, body = self.request("PUT", "/config", self.BEARER,
                                             body={"patch": edit})
                self.assertEqual(code, 400, body)
                self.assertIn(key, body["error"])
                self.assertEqual(self.on_disk(), before)
        code, _, body = self.request(
            "PUT", "/config", self.BEARER,
            body={"patch": {"report.findings": "none"}})
        self.assertEqual(code, 200, body)
        after = self.on_disk()
        self.assertEqual(self.changed_lines(before, after),
                         ["+", "+[report]", '+findings = "none"'])
        tgt = holophyte.target.Target.locate(self.target)
        self.assertEqual(holophyte.config_tables.report_config(tgt).findings, "none")

    def test_get_values_reads_a_triple_quoted_string_and_a_quoted_table(self):
        self.seed()
        before = self.config("config_edit = true\n") + (
            '\n[agents]\nimplementer = """\nclaude -p"""\n'
            '\n["writer host"]\nname = "seat"\n')
        self.assert_loader_valid(before)
        self.start(before)
        code, _, body = self.request("GET", "/config", self.BEARER)
        self.assertEqual(code, 200, body)
        values = body["values"]
        self.assertEqual(values["agents"]["implementer"], "claude -p")
        self.assertEqual(values["writer host"], {"name": "seat"})
        self.assertEqual(values["worktree"]["setup"], ["make deps"])
        self.assertEqual(values["loop"]["workers"], 2)
        # Parsed from the redacted text: the secret is not in the values.
        self.assertEqual(values["linear"]["api_key"], "[redacted]")
        self.assertNotIn(self.SECRET, self.raw_body)

    def test_a_daemon_without_tomlkit_fails_at_start_naming_it(self):
        self.seed()
        (self.db.parent / "config.toml").write_text(
            self.config("config_edit = true\n"))
        tgt = holophyte.target.Target.locate(self.target)
        with patch.dict(sys.modules, {"tomlkit": None}):
            with self.assertRaises(SystemExit) as raised:
                holophyte.serve.serve(tgt, "127.0.0.1:0", out=io.StringIO())
        message = str(raised.exception)
        self.assertIn("tomlkit", message)
        self.assertIn("pip install --user -r requirements.txt", message)


if __name__ == "__main__":
    unittest.main()
