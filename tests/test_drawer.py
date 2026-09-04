"""SwiftBar drawer v0: `contrib/swiftbar/holophyte.10s.py` over `/status`
and `/attention`.

The fixtures under `tests/fixtures/drawer/` are `/status` answers with a
fixed `now`, some wrapped with the `/runs` and `/attention` answers. The
script is exercised both through `--render` as a subprocess (the exact
bytes an operator sees) and through its imported pure functions;
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
import os
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


def render_cli(*names, env=None):
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--render",
         *(str(FIXTURES / f"{n}.json") for n in names)],
        capture_output=True, text=True, check=True, cwd=ROOT, env=env)
    return out.stdout


def embedded(line, parameter="templateImage="):
    return base64.b64decode(line.split(parameter)[1].split()[0])


def closed_loopback_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RenderTests(unittest.TestCase):
    def test_working_and_idle_targets_render_green_with_no_needs_you(self):
        lines = render_cli("working", "idle").splitlines()
        self.assertNotIn("●", lines[0])
        self.assertNotIn("templateImage=", lines[0])
        self.assertEqual(embedded(lines[0], "image="),
                         (ROOT / "assets" / "menubar-ok.pdf").read_bytes())
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
        self.assertEqual(embedded(lines[0], "image="),
                         (ROOT / "assets" / "menubar-warn.pdf").read_bytes())
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
        # Rows from a daemon older than `ended_ms`: the ticket alone.
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

    def test_title_picks_the_variant_by_level_whatever_the_appearance(self):
        assets = ROOT / "assets"
        for level, name in ((drawer.WORKING, "ok"), (drawer.ATTENTION, "warn"),
                            (drawer.CRITICAL, "bad")):
            line = drawer.title(level)
            self.assertNotIn("●", line)
            self.assertNotIn("templateImage=", line)
            self.assertNotIn("color=", line)
            self.assertEqual(embedded(line, "image="),
                             (assets / f"menubar-{name}.pdf").read_bytes(), level)
        idle = drawer.title(drawer.IDLE)
        self.assertNotIn("●", idle)
        self.assertNotIn(" image=", idle)
        self.assertTrue(embedded(idle).startswith(b"%PDF"))
        # The bar's tint is not the system appearance, so the variable
        # SwiftBar sets must not change the glyph: Dark, Light and unset
        # all embed the same bytes.
        warn = (assets / "menubar-warn.pdf").read_bytes()
        for appearance in ("Dark", "Light", None):
            env = {k: v for k, v in os.environ.items() if k != "OS_APPEARANCE"}
            if appearance is not None:
                env["OS_APPEARANCE"] = appearance
            first = render_cli("stale_heartbeat", env=env).splitlines()[0]
            self.assertEqual(embedded(first, "image="), warn, appearance)

    def test_missing_variant_falls_back_to_template_and_dot_text(self):
        pdf = (ROOT / "assets" / "menubar-template.pdf").read_bytes()
        with tempfile.TemporaryDirectory() as assets:
            (Path(assets) / "menubar-template.pdf").write_bytes(pdf)
            line = drawer.title(drawer.CRITICAL, assets)
            self.assertTrue(line.startswith("●"))
            self.assertIn(f"color={drawer.RED}", line)
            self.assertNotIn(" image=", line)
            self.assertEqual(embedded(line), pdf)
            self.assertEqual(drawer.glyph(drawer.CRITICAL, assets),
                             (None, None))

    def test_title_embeds_pdf_then_1x_png_then_nothing(self):
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

    def test_daemon_attention_items_render_one_row_each_in_its_order(self):
        lines = render_cli("attention_all_kinds").splitlines()
        self.assertEqual(embedded(lines[0], "image="),
                         (ROOT / "assets" / "menubar-warn.pdf").read_bytes())
        needs = lines.index(next(x for x in lines if x.startswith("NEEDS YOU")))
        rows = lines[needs + 1:lines.index("---", needs)]
        amber = f" | color={drawer.AMBER}"
        self.assertEqual(rows, [
            "attention_all_kinds · KO-240 · blocked: The ticket names two "
            "verify commands that disagree about th…" + amber,
            "attention_all_kinds · KO-232 · heartbeat 7m" + amber,
            "attention_all_kinds · KO-229 · failed 2h ago: verify failed: "
            "2 tests errored in test_…" + amber,
            "attention_all_kinds · supervisor stale · 20m" + amber,
        ])
        # The question is cut at 60 characters, the reason at 40; a short
        # one is kept whole and a multi-line one is flattened.
        e = drawer.fixture_entry(FIXTURES / "attention_all_kinds.json")
        e["attention"]["items"][0]["question"] = "Which\nfixture?"
        e["attention"]["items"][2]["reason"] = "x" * 40
        rows, level = drawer.attention_rows(e)
        self.assertEqual(rows[0][0],
                         "attention_all_kinds · KO-240 · blocked: Which fixture?")
        self.assertTrue(rows[2][0].endswith("failed 2h ago: " + "x" * 40))
        self.assertEqual(level, drawer.ATTENTION)
        # The daemon's own level rules the glyph: `none` with no items is
        # idle, and an unreachable daemon outranks anything it could say.
        e["attention"] = {"level": "none", "items": [], "now": 1756900000000}
        self.assertEqual(drawer.attention_rows(e), ([], drawer.IDLE))
        gone = {"name": "gone", "url": "gone",
                "status": {"unreachable": True, "error": "timed out"},
                "attention": None}
        rows, level = drawer.attention([e, gone])
        self.assertEqual((rows, level),
                         ([("gone · unreachable", drawer.RED)], drawer.CRITICAL))

    def test_without_an_attention_answer_the_local_rule_still_applies(self):
        # No `attention` key in the fixture: the stale-heartbeat row is
        # computed from `/status` as before, and a 404 body (no `items`)
        # is the same case.
        e = drawer.fixture_entry(FIXTURES / "stale_heartbeat.json")
        self.assertIsNone(e["attention"])
        expected = ([("stale_heartbeat · KO-232 · heartbeat 7m", drawer.AMBER)],
                    drawer.ATTENTION)
        self.assertEqual(drawer.attention_rows(e), expected)
        e["attention"] = {"error": "not found", "path": "/attention",
                          "http_status": 404}
        self.assertEqual(drawer.attention_rows(e), expected)
        lines = render_cli("stale_heartbeat").splitlines()
        needs = lines.index(next(x for x in lines if x.startswith("NEEDS YOU")))
        self.assertEqual(lines[needs + 1], "stale_heartbeat · KO-232 · "
                         f"heartbeat 7m | color={drawer.AMBER}")
        self.assertEqual(lines[needs + 2], "---")


    def test_an_attention_failure_after_a_good_status_is_a_red_row(self):
        # Only a 404 earns the local fallback. A timeout or a 5xx on
        # `/attention` after a 200 on `/status` must stay visible: the
        # blocked and failed items it hid are the ones this side cannot
        # compute, so the target may not look green.
        e = drawer.fixture_entry(FIXTURES / "idle.json")
        self.assertEqual(drawer.attention_rows(e), ([], drawer.IDLE))
        for answer, why in (
                ({"unreachable": True, "error": "timed out"}, "timed out"),
                ({"error": "store busy", "http_status": 500}, "HTTP 500"),
                ({"unreachable": True, "error": "HTTP 502", "http_status": 502},
                 "HTTP 502")):
            e["attention"] = answer
            rows, level = drawer.attention_rows(e)
            self.assertEqual(rows, [(f"idle · /attention failed: {why}", drawer.RED)],
                             answer)
            self.assertEqual(level, drawer.CRITICAL, answer)
        # The local rule's rows still follow the error row.
        e = drawer.fixture_entry(FIXTURES / "stale_heartbeat.json")
        e["attention"] = {"unreachable": True, "error": "timed out"}
        rows, level = drawer.attention_rows(e)
        self.assertEqual([t for t, _ in rows],
                         ["stale_heartbeat · /attention failed: timed out",
                          "stale_heartbeat · KO-232 · heartbeat 7m"])
        self.assertEqual(level, drawer.CRITICAL)


class Handler(http.server.BaseHTTPRequestHandler):
    """A stub daemon: `/status` and `/runs` answer 200 with the bodies set
    on the class; `/attention` answers 404 like a daemon older than the
    path unless `attention_body` is set."""
    body = b""
    runs_body = b'{"rows": [], "limit": null}'
    attention_body = None
    attention_code = 200
    paths = []

    def do_GET(self):
        Handler.paths.append(self.path)
        code, body = 200, self.body
        if self.path.startswith("/runs"):
            body = self.runs_body
        elif self.path.startswith("/attention"):
            if self.attention_body is None:
                code, body = 404, b'{"error": "not found", "path": "/attention"}'
            else:
                code, body = self.attention_code, self.attention_body
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

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
        self.assertEqual(embedded(lines[0], "image="),
                         (ROOT / "assets" / "menubar-bad.pdf").read_bytes())
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
        self.assertEqual(Handler.paths, ["/status", "/attention", "/runs"])
        self.assertEqual(drawer.target_block(got)[1],
                         "idle · last merge KO-237 · 12m ago")
        Handler.body = json.dumps(fixture("working")).encode()
        Handler.paths = []
        got = drawer.poll({"name": "working", "url": url})
        self.assertEqual(Handler.paths, ["/status", "/attention"])
        self.assertIsNone(got["runs"])

    def test_older_daemon_answering_404_on_attention_gets_the_local_rule(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        Handler.body = json.dumps(fixture("stale_heartbeat")).encode()
        Handler.attention_body = None
        Handler.paths = []
        with tempfile.TemporaryDirectory() as home:
            cfg = Path(home) / "drawer.toml"
            cfg.write_text(f'[[daemon]]\nname = "old"\nurl = "http://127.0.0.1:'
                           f'{server.server_port}"\n')
            out = io.StringIO()
            with redirect_stdout(out):
                drawer.main(["--config", str(cfg)])
        self.assertIn("/attention", Handler.paths)
        lines = out.getvalue().splitlines()
        self.assertEqual(embedded(lines[0], "image="),
                         (ROOT / "assets" / "menubar-warn.pdf").read_bytes())
        needs = lines.index(next(x for x in lines if x.startswith("NEEDS YOU")))
        self.assertEqual(lines[needs + 1:needs + 3],
                         [f"old · KO-232 · heartbeat 7m | color={drawer.AMBER}",
                          "---"])
        text = out.getvalue()
        self.assertNotIn("not found", text)
        self.assertNotIn("404", text)
        self.assertNotIn("unreachable", text)
        # The same stub answering `/attention` is taken at its word.
        Handler.attention_body = json.dumps(
            fixture("attention_all_kinds")["attention"]).encode()
        self.addCleanup(setattr, Handler, "attention_body", None)
        got = drawer.poll({"name": "new", "url": f"http://127.0.0.1:{server.server_port}"})
        rows, _ = drawer.attention_rows(got)
        self.assertEqual([t for t, _ in rows][:2],
                         ["new · KO-240 · blocked: The ticket names two verify "
                          "commands that disagree about th…",
                          "new · KO-232 · heartbeat 7m"])

    def test_daemon_answering_500_on_attention_shows_the_failure_live(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        Handler.body = json.dumps(fixture("idle")).encode()
        Handler.attention_body = b'{"error": "store busy"}'
        Handler.attention_code = 500
        self.addCleanup(setattr, Handler, "attention_body", None)
        self.addCleanup(setattr, Handler, "attention_code", 200)
        with tempfile.TemporaryDirectory() as home:
            cfg = Path(home) / "drawer.toml"
            cfg.write_text(f'[[daemon]]\nname = "sick"\nurl = "http://127.0.0.1:'
                           f'{server.server_port}"\n')
            out = io.StringIO()
            with redirect_stdout(out):
                drawer.main(["--config", str(cfg)])
        lines = out.getvalue().splitlines()
        self.assertEqual(embedded(lines[0], "image="),
                         (ROOT / "assets" / "menubar-bad.pdf").read_bytes())
        needs = lines.index(next(x for x in lines if x.startswith("NEEDS YOU")))
        self.assertEqual(lines[needs + 1:needs + 3],
                         [f"sick · /attention failed: HTTP 500 | color={drawer.RED}",
                          "---"])
        # The status block itself is unaffected: `/status` did answer.
        self.assertIn("sick · writer", out.getvalue())

    def test_fetch_marks_a_closed_port_unreachable(self):
        got = drawer.fetch(f"http://127.0.0.1:{closed_loopback_port()}")
        self.assertTrue(got["unreachable"])


if __name__ == "__main__":
    unittest.main()
