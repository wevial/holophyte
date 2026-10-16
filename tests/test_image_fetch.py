"""The proxied copy of each PR body image is compared with its own URL."""
import html
import re
import struct
import threading
import unittest
import urllib.parse
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from holophyte.loop.gates import InfraFailure
from holophyte.pr import image_fetch

LIMIT = 10 * 1024 * 1024
PULL = SimpleNamespace(repo="example/repo", number=7)
PROJECT = SimpleNamespace(config=lambda: {}, config_path="holophyte.toml")


def chunk(kind, data):
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data)))


def png_of(size):
    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    head = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
    end = chunk(b"IEND", b"")
    return head + chunk(b"IDAT", b"\0" * (size - len(head) - len(end) - 12)) + end


FULL = png_of(143026)
CUT = FULL[:65536]


class Images(BaseHTTPRequestHandler):
    """`/full.png` is the image; `/cut.png` and `/padded.png` its broken
    copies; `/camo/QUOTED` proxies the quoted URL as `served` says."""

    served = staticmethod(lambda canonical: FULL)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/camo/"):
            data = self.served(urllib.parse.unquote(path[len("/camo/"):]))
        else:
            data = {"/full.png": FULL, "/cut.png": CUT,
                    "/padded.png": CUT.ljust(len(FULL), b"\0")}[path]
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class ImageFetchTests(unittest.TestCase):
    def setUp(self):
        handler = type("Handler", (Images,), {})
        self.handler = handler
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.host = f"127.0.0.1:{server.server_address[1]}"
        self.base = f"http://{self.host}"

    def serve(self, served):
        self.handler.served = staticmethod(served)

    def fake_github(self, body):
        """Renders the body as GitHub does, its images through `/camo/`."""
        self.body, self.writes = body, []

        def rendered(target, pull):
            return "".join(
                f'<p><a href="{html.escape(url)}"><img src="{self.base}/camo/'
                f'{urllib.parse.quote(url, safe="")}" alt="{html.escape(alt)}"'
                f' data-canonical-src="{html.escape(html.unescape(url))}"'
                ' style="max-width: 100%;"></a></p>'
                for alt, url in re.findall(r"!\[([^\]]*)\]\(([^)\s]+)\)", self.body))

        def edit(target, pull, body):
            self.writes.append(body)
            self.body = body

        self.enterContext(patch.object(image_fetch, "PROXY_HOST", self.host))
        self.enterContext(patch.object(image_fetch.github, "pull_body_html",
                                       side_effect=rendered))
        self.enterContext(patch.object(image_fetch.github, "rest",
                                       side_effect=lambda *a: {"body": self.body}))
        self.enterContext(patch.object(image_fetch.github, "edit_pr_body",
                                       side_effect=edit))

    def test_a_cut_or_unterminated_proxied_png_is_short(self):
        full = f"{self.base}/full.png"
        self.assertEqual(image_fetch.short_by(f"{self.base}/cut.png", full, LIMIT),
                         (65536, 143026))
        self.assertIsNone(image_fetch.short_by(full, full, LIMIT))
        self.assertEqual(image_fetch.short_by(f"{self.base}/padded.png", full, LIMIT),
                         (143026, 143026))

    def test_a_short_image_is_relinked_once_when_the_new_url_serves_in_full(self):
        url = f"{self.base}/full.png"
        self.fake_github(f"## Evidence\n\n![screen]({url})\n\nLinear: KO-1\n")
        self.serve(lambda canonical: FULL if "v=" in canonical else CUT)
        lines = image_fetch.check(PROJECT, PULL, self.body)
        self.assertEqual(self.writes,
                         [f"## Evidence\n\n![screen]({url}?v=2)\n\nLinear: KO-1\n"])
        self.assertEqual(len(lines), 1)
        self.assertIn("served 65536 of 143026 bytes", lines[0])
        self.assertIn(f"re-linked as {url}?v=2", lines[0])

    def test_an_image_still_short_after_two_relinks_is_left_and_named(self):
        url = f"{self.base}/full.png"
        self.fake_github(f"![screen]({url})\n")
        self.serve(lambda canonical: CUT)
        lines = image_fetch.check(PROJECT, PULL, self.body)
        self.assertEqual(self.writes, [f"![screen]({url}?v=2)\n",
                                       f"![screen]({url}?v=3)\n"])
        self.assertEqual(sum("re-linked" in line for line in lines), 2)
        self.assertIn(f"{url}?v=3; still short after 2 re-links", lines[-1])

    def test_a_relink_bumps_or_appends_the_v_query(self):
        for url, expected in [("https://b/x.png", "https://b/x.png?v=2"),
                              ("https://b/x.png?a=1", "https://b/x.png?a=1&v=2"),
                              ("https://b/x.png?a=1&v=4", "https://b/x.png?a=1&v=5")]:
            with self.subTest(url=url):
                self.assertEqual(image_fetch.relinked(url), expected)

    def test_an_unreadable_body_skips_the_check_and_writes_nothing(self):
        self.fake_github(f"![screen]({self.base}/full.png)\n")
        image_fetch.github.pull_body_html.side_effect = InfraFailure("gh api failed")
        self.assertEqual(image_fetch.check(PROJECT, PULL, self.body),
                         ["image fetch check skipped: gh api failed"])
        self.assertEqual(self.writes, [])

    def test_a_refused_relink_records_only_the_skip(self):
        url = f"{self.base}/full.png"
        for refused in (1, 2):
            with self.subTest(refused=refused):
                self.fake_github(f"![screen]({url})\n")
                self.serve(lambda canonical: CUT)
                edit = image_fetch.github.edit_pr_body.side_effect

                def refuse(target, pull, body, edit=edit, refused=refused):
                    if len(self.writes) + 1 == refused:
                        raise InfraFailure("gh pr edit failed")
                    edit(target, pull, body)

                image_fetch.github.edit_pr_body.side_effect = refuse
                self.assertEqual(image_fetch.check(PROJECT, PULL, self.body),
                                 ["image fetch check skipped: gh pr edit failed"])
                self.assertEqual(len(self.writes), refused - 1)

    def test_each_image_moves_one_version_per_relink(self):
        url = f"{self.base}/full.png"
        self.fake_github(f"![a]({url}) ![b]({url}?v=2)\n")
        self.serve(lambda canonical: CUT)
        image_fetch.check(PROJECT, PULL, self.body)
        self.assertEqual(self.writes, [f"![a]({url}?v=2) ![b]({url}?v=3)\n",
                                       f"![a]({url}?v=3) ![b]({url}?v=4)\n"])

    def test_an_entity_escaped_url_is_relinked_in_its_own_encoding(self):
        url = f"{self.base}/full.png?a=1&amp;b=2"
        self.fake_github(f"![screen]({url})\n")
        self.serve(lambda canonical: FULL if "v=" in canonical else CUT)
        lines = image_fetch.check(PROJECT, PULL, self.body)
        self.assertEqual(self.writes, [f"![screen]({url}&amp;v=2)\n"])
        self.assertEqual(len(lines), 1)


if __name__ == "__main__":
    unittest.main()
