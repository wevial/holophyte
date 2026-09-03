"""SwiftBar drawer v0: `contrib/swiftbar/holophyte.10s.py` over `/status`.

The fixtures under `tests/fixtures/drawer/` are `/status` answers with a
fixed `now`. The script is exercised both through `--render` as a subprocess
(the exact bytes an operator sees) and through its imported pure functions;
the live path is exercised against a loopback daemon serving one fixture
beside a closed loopback port.

Run: python3 -m unittest discover -s tests -p 'test_drawer*' -v
"""
from __future__ import annotations

import base64
import http.server
import importlib.util
import io
import json
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "contrib" / "swiftbar" / "holophyte.10s.py"
FIXTURES = ROOT / "tests" / "fixtures" / "drawer"

spec = importlib.util.spec_from_file_location("drawer", SCRIPT)
drawer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drawer)


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def entry(name):
    return {"name": name, "url": name, "status": fixture(name)}


def render_cli(*names):
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--render",
         *(str(FIXTURES / f"{n}.json") for n in names)],
        capture_output=True, text=True, check=True, cwd=ROOT)
    return out.stdout


def closed_loopback_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RenderTests(unittest.TestCase):
    def test_working_and_idle_targets_render_green_with_no_needs_you(self):
        lines = render_cli("working", "idle").splitlines()
        self.assertIn("templateImage=", lines[0])
        self.assertTrue(lines[0].startswith("●"))
        self.assertIn(f"color={drawer.GREEN}", lines[0])
        self.assertNotIn("NEEDS YOU", "\n".join(lines))
        text = "\n".join(lines)
        self.assertIn("working · writer", text)
        self.assertIn("idle · writer", text)
        self.assertIn("KO-232 · reviewing", text)
        self.assertIn("idle · queue empty", text)
        detail = next(x for x in lines if x.lstrip().startswith("round"))
        self.assertIn("12m of 30m", detail)
        self.assertIn("heartbeat 4s", detail)
        supervisors = [x for x in lines
                       if x.lstrip().startswith("supervisor live |")]
        self.assertEqual(len(supervisors), 2)
        header_index = lines.index(next(x for x in lines if "working · writer" in x))
        self.assertLess(header_index, lines.index("KO-232 · reviewing"))

    def test_stale_heartbeat_is_amber_and_named_in_needs_you(self):
        lines = render_cli("stale_heartbeat", "idle").splitlines()
        self.assertIn(f"color={drawer.AMBER}", lines[0])
        needs = lines.index(next(x for x in lines if x.startswith("NEEDS YOU")))
        block = lines.index(next(x for x in lines if "stale_heartbeat · writer" in x))
        self.assertLess(needs, block)
        row = lines[needs + 1]
        self.assertIn("KO-232", row)
        self.assertIn("heartbeat", row)
        self.assertIn(f"color={drawer.AMBER}", row)

    def test_stale_supervisor_is_amber_in_needs_you(self):
        rows, level = drawer.attention([entry("stale_supervisor"), entry("idle")])
        self.assertEqual(level, drawer.ATTENTION)
        self.assertEqual([(t, c) for t, c in rows],
                         [("stale_supervisor · supervisor stale · 4m", drawer.AMBER)])

    def test_all_idle_shows_no_dot(self):
        rows, level = drawer.attention([entry("idle")])
        self.assertEqual((rows, level), ([], drawer.IDLE))
        first = render_cli("idle").splitlines()[0]
        self.assertNotIn("●", first)
        self.assertNotIn("color=", first)
        self.assertIn("templateImage=", first)
        self.assertIn("1 targets · 1 host ·", render_cli("idle").splitlines()[-1])

    def test_detail_rows_are_inline_under_their_target_not_submenus(self):
        lines = render_cli("idle").splitlines()
        # `---` is SwiftBar's title/body delimiter and separator syntax, not
        # the `--` submenu prefix; a plugin cannot render without it.
        submenu = [x for x in lines if x.startswith("--") and x != "---"]
        self.assertEqual(submenu, [])
        main_row = lines.index(next(x for x in lines if x.startswith("idle · writer")))
        supervisor = lines[main_row + 2]
        self.assertEqual(lines[main_row + 1], "idle · queue empty")
        self.assertTrue(supervisor.startswith(" "), supervisor)
        self.assertIn("supervisor live", supervisor)
        self.assertIn("trim=false", supervisor)

    def test_supervisor_row_shows_the_age_only_when_stale_or_none(self):
        def row(name, **sup):
            e = entry(name)
            e["status"]["supervisor"] = {**e["status"]["supervisor"], **sup}
            return drawer.target_block(e)[-1]

        live = row("idle")
        stale = row("stale_supervisor", heartbeat_age_ms=420000)
        none = row("supervisor_none")
        self.assertEqual(live.split(" | ")[0].strip(), "supervisor live")
        self.assertEqual(stale.split(" | ")[0].strip(), "supervisor stale · 7m")
        self.assertEqual(none.split(" | ")[0].strip(), "supervisor none")
        self.assertNotIn(f"color={drawer.AMBER}", live)
        self.assertIn(f"color={drawer.AMBER}", stale)
        self.assertIn(f"color={drawer.AMBER}", none)
        # The CLI path agrees with the pure one, and no live row carries a
        # number after the state word.
        text = render_cli("idle", "stale_supervisor", "supervisor_none")
        self.assertIn("   supervisor live | ", text)
        amber = f"trim=false size=11 color={drawer.AMBER}"
        self.assertIn(f"   supervisor stale · 4m | {amber}", text)
        self.assertIn(f"   supervisor none | {amber}", text)
        self.assertNotRegex(text, r"supervisor live · [0-9]")

    def test_supervisor_and_idle_ages_use_whole_units_like_the_daemon(self):
        # The daemon's tables print `2h` and `3d`, not `2h00m` or `72h00m`.
        def sup(ms):
            e = entry("stale_supervisor")
            e["status"]["supervisor"]["heartbeat_age_ms"] = ms
            return drawer.target_block(e)[-1].split(" | ")[0].strip()

        self.assertEqual(sup(12000), "supervisor stale · 12s")
        self.assertEqual(sup(7200000), "supervisor stale · 2h")
        self.assertEqual(sup(3 * 86400000), "supervisor stale · 3d")
        e = drawer.fixture_entry(FIXTURES / "idle_last_merge.json")
        e["status"]["now"] = 1756899280000 + 2 * 3600000
        self.assertEqual(drawer.target_block(e)[1],
                         "idle · last merge KO-237 · 2h ago")
        # The attention row for a stale supervisor reads the same way.
        stale = entry("stale_supervisor")
        stale["status"]["supervisor"] = {"state": "stale",
                                         "heartbeat_age_ms": 7200000}
        rows, _ = drawer.attention([stale])
        self.assertEqual(rows[-1][0], "stale_supervisor · supervisor stale · 2h")

    def test_idle_row_names_the_last_merge_or_says_nothing_merged(self):
        merged = render_cli("idle_last_merge").splitlines()
        nothing = render_cli("idle_nothing_merged").splitlines()
        self.assertIn("idle · last merge KO-237 · 12m ago", merged)
        self.assertNotIn("KO-239", "\n".join(merged))  # newer, but failed
        self.assertIn("idle · nothing merged yet", nothing)
        # Rows the daemon sends today carry no `ended_ms`: the ticket alone.
        e = drawer.fixture_entry(FIXTURES / "idle_last_merge.json")
        for r in e["runs"]["rows"]:
            del r["ended_ms"]
        self.assertEqual(drawer.target_block(e)[1], "idle · last merge KO-237")
        # A `/runs` that did not answer falls back to the old row.
        e["runs"] = {"unreachable": True, "error": "timed out"}
        self.assertEqual(drawer.target_block(e)[1], "idle · queue empty")

    def test_footer_counts_targets_and_distinct_hosts(self):
        def status(host):
            return {"name": host, "url": host,
                    "status": {**fixture("idle"), "host": host}}
        one = drawer.render([status("writer-1"), status("writer-1")], 1756900000000)
        self.assertIn("2 targets · 1 host ·", one[-1])
        two = drawer.render([status("writer-1"), status("writer-2")], 1756900000000)
        self.assertIn("2 targets · 2 hosts ·", two[-1])
        self.assertIn("2 targets · 1 host ·", render_cli("idle", "idle"))
        self.assertIn("2 targets · 2 hosts ·",
                      render_cli("idle", "idle_second_host"))

    def test_title_embeds_pdf_then_1x_png_then_nothing(self):
        def embedded(line):
            return base64.b64decode(line.split("templateImage=")[1].split()[0])
        pdf = (ROOT / "assets" / "menubar-template.pdf").read_bytes()
        png = (ROOT / "assets" / "menubar-template@1x.png").read_bytes()
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertTrue(embedded(drawer.title(drawer.IDLE)).startswith(b"%PDF"))
        with tempfile.TemporaryDirectory() as assets:
            (Path(assets) / "menubar-template.pdf").write_bytes(pdf)
            (Path(assets) / "menubar-template@1x.png").write_bytes(png)
            (Path(assets) / "menubar-template@2x.png").write_bytes(b"\x89PNG2x")
            self.assertEqual(embedded(drawer.title(drawer.WORKING, assets)), pdf)
            (Path(assets) / "menubar-template.pdf").unlink()
            self.assertEqual(embedded(drawer.title(drawer.WORKING, assets)), png)
            (Path(assets) / "menubar-template@1x.png").unlink()
            bare = drawer.title(drawer.WORKING, assets)
            self.assertNotIn("templateImage=", bare)
            self.assertEqual(bare, f"● | color={drawer.GREEN}")

    def test_same_fixture_renders_byte_identical(self):
        once = render_cli("working", "stale_heartbeat", "idle_last_merge")
        again = render_cli("working", "stale_heartbeat", "idle_last_merge")
        self.assertEqual(once, again)
        self.assertIn("updated 0s ago", once)
        self.assertIn("idle · last merge KO-237 · 12m ago", once)


class Handler(http.server.BaseHTTPRequestHandler):
    body = b""
    runs_body = b'{"rows": [], "limit": null}'
    paths = []

    def do_GET(self):
        Handler.paths.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.runs_body if self.path.startswith("/runs")
                         else self.body)

    def log_message(self, *args):
        pass


class LiveTests(unittest.TestCase):
    def test_unreachable_daemon_is_red_and_the_rest_still_render(self):
        Handler.body = json.dumps(fixture("working")).encode()
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        dead = closed_loopback_port()
        with tempfile.TemporaryDirectory() as home:
            cfg = Path(home) / "drawer.toml"
            cfg.write_text(
                'linear = "https://linear.app/example"\n'
                f'[[daemon]]\nname = "alive"\nurl = "http://127.0.0.1:'
                f'{server.server_port}"\n'
                f'[[daemon]]\nname = "gone"\nurl = "http://127.0.0.1:{dead}"\n')
            out = io.StringIO()
            with redirect_stdout(out):
                drawer.main(["--config", str(cfg)])
        lines = out.getvalue().splitlines()
        self.assertIn(f"color={drawer.RED}", lines[0])
        needs = lines.index(next(x for x in lines if x.startswith("NEEDS YOU")))
        self.assertEqual(lines[needs + 1], f"gone · unreachable | color={drawer.RED}")
        text = "\n".join(lines)
        self.assertIn("alive · writer", text)
        self.assertIn("KO-232 · reviewing", text)
        self.assertIn("gone · ?", text)
        self.assertIn("href=https://linear.app/example", text)
        self.assertIn("2 targets · 1 host", text)

    def test_idle_daemon_is_asked_for_runs_and_a_working_one_is_not(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}"
        Handler.body = json.dumps(fixture("idle")).encode()
        Handler.runs_body = json.dumps(
            fixture("idle_last_merge")["runs"]).encode()
        Handler.paths = []
        got = drawer.poll({"name": "idle", "url": url})
        self.assertEqual(Handler.paths, ["/status", "/runs"])
        self.assertEqual(drawer.target_block(got)[1],
                         "idle · last merge KO-237 · 12m ago")
        Handler.body = json.dumps(fixture("working")).encode()
        Handler.paths = []
        got = drawer.poll({"name": "working", "url": url})
        self.assertEqual(Handler.paths, ["/status"])
        self.assertIsNone(got["runs"])

    def test_fetch_marks_a_closed_port_unreachable(self):
        got = drawer.fetch(f"http://127.0.0.1:{closed_loopback_port()}")
        self.assertTrue(got["unreachable"])


if __name__ == "__main__":
    unittest.main()
