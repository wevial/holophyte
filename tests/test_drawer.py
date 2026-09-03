"""SwiftBar drawer v0: `contrib/swiftbar/holophyte.10s.py` over `/status`.

The fixtures under `tests/fixtures/drawer/` are `/status` answers with a
fixed `now`. The script is exercised both through `--render` as a subprocess
(the exact bytes an operator sees) and through its imported pure functions;
the live path is exercised against a loopback daemon serving one fixture
beside a closed loopback port.

Run: python3 -m unittest discover -s tests -p 'test_drawer*' -v
"""
from __future__ import annotations

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
        detail = next(x for x in lines if x.startswith("--round"))
        self.assertIn("12m of 30m", detail)
        self.assertIn("heartbeat 4s", detail)
        self.assertEqual(
            len([x for x in lines if x.startswith("--supervisor live · 12s")]), 2)
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

    def test_same_fixture_renders_byte_identical(self):
        once = render_cli("working", "stale_heartbeat")
        again = render_cli("working", "stale_heartbeat")
        self.assertEqual(once, again)
        self.assertIn("updated 0s ago", once)


class Handler(http.server.BaseHTTPRequestHandler):
    body = b""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.body)

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
        self.assertIn("2 hosts", text)

    def test_fetch_marks_a_closed_port_unreachable(self):
        got = drawer.fetch(f"http://127.0.0.1:{closed_loopback_port()}")
        self.assertTrue(got["unreachable"])


if __name__ == "__main__":
    unittest.main()
