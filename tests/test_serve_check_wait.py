"""The settings API exposes the effective check wait without writing it."""
import tomllib

from tests import test_serve_config


class CheckWaitSettingsTests(test_serve_config.ServeTestCase):
    config = test_serve_config.ConfigEditTests.config
    TOKEN = test_serve_config.ConfigEditTests.TOKEN
    BEARER = test_serve_config.ConfigEditTests.BEARER
    SECRET = test_serve_config.ConfigEditTests.SECRET
    HOOK_TOKENS = test_serve_config.ConfigEditTests.HOOK_TOKENS
    on_disk = test_serve_config.ConfigEditTests.on_disk

    def test_default_is_visible_but_only_explicit_edits_are_written(self):
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        code, _, body = self.request("GET", "/config", self.BEARER)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["values"]["merge"]["check_wait_sec"], 1800)
        self.assertNotIn("check_wait_sec", body["text"])
        self.assertEqual(self.on_disk(), before)
        for edit, expected in (({"loop.workers": 3}, 1800),
                               ({"merge.check_wait_sec": 3600}, 3600),
                               ({"merge.check_wait_sec": 60}, 60)):
            with self.subTest(edit=edit):
                code, _, body = self.request(
                    "PUT", "/config", self.BEARER, body={"patch": edit})
                self.assertEqual(code, 200, body)
                code, _, body = self.request("GET", "/config", self.BEARER)
                self.assertEqual(code, 200, body)
                self.assertEqual(body["values"]["merge"]["check_wait_sec"], expected)
                written = tomllib.loads(self.on_disk()).get("merge", {})
                if "merge.check_wait_sec" in edit:
                    self.assertEqual(written["check_wait_sec"], expected)
                else:
                    self.assertNotIn("check_wait_sec", written)
