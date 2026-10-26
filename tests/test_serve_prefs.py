"""The host daemon's console preferences: `GET /prefs` and `PUT /prefs`
at its root, asked over HTTP of a real `HostServer` on loopback, the
document read back from `console-prefs.json` in the temporary home.

Run: python3 -m unittest discover -s tests -p 'test_serve_prefs.py' -v
"""
import http.client
import json
import os
import stat
import threading
import unittest
from pathlib import Path

import holophyte.serve.server
from holophyte.config.project import Project
from tests.test_serve_host import ALPHA_TOKEN, MACHINE, HostServeCase, bearer

FIXTURE = Path(__file__).parent / "fixtures" / "serve" / "prefs.json"
DAY_LOG = json.loads(FIXTURE.read_text())["day_log"]


class PrefsCase(HostServeCase):
    def prefs_file(self):
        return self.home / "console-prefs.json"

    def start_writable(self):
        self.host_config(machine_token_file=self.machine(), actions=True)
        self.start()

    def send(self, raw, headers=None):
        """`(status, decoded JSON body)` for a `PUT /prefs` of raw bytes."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("PUT", "/prefs", body=raw, headers=headers or {})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()


class PrefsReadWriteTests(PrefsCase):
    def test_an_empty_home_answers_both_null_and_creates_no_file(self):
        self.start()
        self.assertEqual(self.request("GET", "/prefs"),
                         (200, {"day_log": None, "updated_ms": None}))
        self.assertFalse(self.prefs_file().exists())

    def test_a_put_day_log_is_answered_read_back_and_kept_owner_only(self):
        self.start_writable()
        code, body = self.request("PUT", "/prefs", bearer(MACHINE),
                                  {"day_log": DAY_LOG})
        self.assertEqual(code, 200, body)
        self.assertEqual(body["day_log"], DAY_LOG)
        self.assertIs(type(body["updated_ms"]), int)
        pinned = json.loads(FIXTURE.read_text())
        self.assertEqual({**body, "updated_ms": pinned["updated_ms"]}, pinned)
        self.assertEqual(self.request("GET", "/prefs"), (200, body))
        stored = json.loads(self.prefs_file().read_text())
        self.assertEqual(stored["day_log"], DAY_LOG)
        self.assertEqual(stat.S_IMODE(os.stat(self.prefs_file()).st_mode),
                         0o600)

    def test_an_empty_put_keeps_the_day_log_and_a_null_removes_it(self):
        self.start_writable()
        self.request("PUT", "/prefs", bearer(MACHINE), {"day_log": DAY_LOG})
        code, body = self.request("PUT", "/prefs", bearer(MACHINE), {})
        self.assertEqual((code, body["day_log"]), (200, DAY_LOG))
        self.assertEqual(self.request("GET", "/prefs")[1]["day_log"], DAY_LOG)
        code, _ = self.request("PUT", "/prefs", bearer(MACHINE),
                               {"day_log": None})
        self.assertEqual(code, 200)
        self.assertIsNone(self.request("GET", "/prefs")[1]["day_log"])

    def test_an_unparseable_file_is_named_and_a_put_replaces_it(self):
        self.start_writable()
        self.prefs_file().write_text("not json {")
        code, body = self.request("GET", "/prefs")
        self.assertEqual((code, body["day_log"], body["updated_ms"]),
                         (200, None, None))
        self.assertIn(str(self.prefs_file()), body["error"])
        code, body = self.request("PUT", "/prefs", bearer(MACHINE),
                                  {"day_log": DAY_LOG})
        self.assertEqual(code, 200, body)
        self.assertNotIn("error", body)
        self.assertEqual(json.loads(self.prefs_file().read_text())["day_log"],
                         DAY_LOG)


class PrefsRefusalTests(PrefsCase):
    def test_each_malformed_body_is_400_naming_its_field_and_writes_nothing(self):
        self.start_writable()
        self.request("PUT", "/prefs", bearer(MACHINE), {"day_log": DAY_LOG})
        before = self.prefs_file().read_bytes()

        def day_log(**fields):
            return json.dumps({"day_log": {**DAY_LOG, **fields}}).encode()
        many = {f"item-{n}": True for n in range(65)}
        cases = (
            ("array", b"[]", "body"),
            ("unknown key", b'{"theme": "dark"}', "theme"),
            ("preset", day_log(preset="loud"), "day_log.preset"),
            ("not boolean", day_log(items={"merged": "yes"}),
             "day_log.items.merged"),
            ("space in id", day_log(items={"a b": True}), "day_log.items"),
            ("65 items", day_log(items=many), "day_log.items"),
            ("over 16 KiB", b'{"day_log": null}' + b" " * 16 * 1024, "body"),
        )
        for name, raw, field in cases:
            with self.subTest(name):
                code, body = self.send(raw, bearer(MACHINE))
                self.assertEqual(code, 400, body)
                self.assertIn(field, body["error"])
                self.assertEqual(self.prefs_file().read_bytes(), before)
        del many["item-64"]
        self.assertEqual(self.send(day_log(items=many), bearer(MACHINE))[0],
                         200)

    def test_a_put_without_the_machine_token_is_401_and_writes_nothing(self):
        self.config("alpha", '[serve]\ntoken_file = "{}"\n'.format(
            self.token_file(self.root / "alpha.token", ALPHA_TOKEN)))
        self.start_writable()
        for headers in (None, bearer(ALPHA_TOKEN)):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("PUT", "/prefs", headers,
                                              {"day_log": DAY_LOG}), (401, {}))
        self.assertFalse(self.prefs_file().exists())

    def test_a_put_with_actions_off_is_404(self):
        self.host_config(machine_token_file=self.machine())
        self.start()
        code, _ = self.request("PUT", "/prefs", bearer(MACHINE),
                               {"day_log": DAY_LOG})
        self.assertEqual(code, 404)
        self.assertFalse(self.prefs_file().exists())


class PrefsElsewhereTests(PrefsCase):
    def test_a_project_daemon_and_a_project_prefix_have_no_prefs(self):
        self.start()
        self.assertEqual(self.request("GET", "/projects/alpha/prefs")[0], 404)
        server = holophyte.serve.server.make_server(
            Project.locate(self.paths["alpha"]), "127.0.0.1", 0,
            console_dir=self.root / "no-console")
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]
        self.assertEqual(self.request("GET", "/prefs")[0], 404)


if __name__ == "__main__":
    unittest.main()
