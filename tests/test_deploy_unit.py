"""`deploy/`: the host units -- `holophyte.target`, the serve socket and
service, the sweep timer and oneshot -- and the project templates beside
them (`holophyte-serve@`, `holophyte-supervise@`, `holophyte-loop@`).

The units are static configuration, so the checks are structural: each
parses as INI and names the keys the consolidation design tables, with the
values the code depends on read from the code (the sweep interval, the
sweep's timeout, the daemon's drain, the worst Linear read), and each host
unit's command line runs the host form it claims to. `systemd-analyze
--user verify` runs on Linux only; the lifecycle under a real user manager
is `tests/test_deploy_lifecycle.py`.

Run: python3 -m unittest discover -s tests -p 'test_deploy*' -v
"""
from __future__ import annotations

import configparser
import inspect
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import linear_provider
from holophyte.host import SWEEP_SEC
from holophyte.reexec import SWEEP_UNIT
from holophyte.serve_host import SWEEP_TIMEOUT_SEC
from holophyte.serve_watch import DRAIN_SEC

ROOT = Path(__file__).resolve().parent.parent
UNIT = ROOT / "deploy" / "holophyte-serve@.service"
SUPERVISE_UNIT = ROOT / "deploy" / "holophyte-supervise@.service"
LOOP_UNIT = ROOT / "deploy" / "holophyte-loop@.service"
HOSTS = ROOT / "docs" / "operating" / "hosts.md"
OPERATING = ROOT / "docs" / "operating.md"
DEPLOY_README = ROOT / "deploy" / "README.md"

DEPLOY = ROOT / "deploy"
TARGET = "holophyte.target"
SOCKET = "holophyte-serve.socket"
SERVE = "holophyte-serve.service"
TIMER = "holophyte-sweep.timer"
SWEEP = "holophyte-sweep.service"
HOST_UNITS = (TARGET, SOCKET, SERVE, TIMER, SWEEP)
# `linear_provider._urlopen()`'s per-attempt timeout; the source is checked
# for it below, so a change there fails here rather than going unnoticed.
LINEAR_ATTEMPT_SEC = 30

ENV_KEYS = ("HOLOPHYTE_TARGET", "HOLOPHYTE_SERVE_ADDRESS",
            "HOLOPHYTE_SERVE_PORT")


def parse_unit(path=UNIT):
    # systemd units are INI-like; keys may repeat and are case-sensitive.
    parser = configparser.RawConfigParser(strict=False)
    parser.optionxform = str
    parser.read_string(path.read_text())
    return parser


class UnitFileTests(unittest.TestCase):
    def test_parses_with_the_three_systemd_sections(self):
        unit = parse_unit()
        self.assertEqual(sorted(unit.sections()),
                         ["Install", "Service", "Unit"])

    def test_serves_the_substituted_address_and_port(self):
        service = parse_unit()["Service"]
        exec_start = service["ExecStart"]
        self.assertIn("factory.py", exec_start)
        self.assertIn("--serve", exec_start)
        self.assertIn(
            "${HOLOPHYTE_SERVE_ADDRESS}:${HOLOPHYTE_SERVE_PORT}", exec_start)
        self.assertIn("${HOLOPHYTE_TARGET}", exec_start)
        self.assertIn("%i", service["EnvironmentFile"])
        self.assertEqual(service["Restart"], "on-failure")
        self.assertIn("WorkingDirectory", service)

    def test_every_key_it_substitutes_is_documented(self):
        substituted = set(re.findall(r"\$\{(\w+)\}", UNIT.read_text()))
        self.assertEqual(substituted, set(ENV_KEYS))
        section = OPERATING.read_text().split("## Serving standing", 1)
        self.assertEqual(len(section), 2, "no `## Serving standing` section")
        for key in ENV_KEYS:
            self.assertIn(f"`{key}`", section[1])
        self.assertIn("7710", section[1])
        self.assertIn("holophyte-serve@.service", DEPLOY_README.read_text())

    def test_supervise_unit_restarts_the_supervisor_on_failure(self):
        unit = parse_unit(SUPERVISE_UNIT)
        self.assertEqual(sorted(unit.sections()),
                         ["Install", "Service", "Unit"])
        service = unit["Service"]
        exec_start = service["ExecStart"]
        self.assertIn("factory.py ${HOLOPHYTE_TARGET}", exec_start)
        self.assertTrue(exec_start.endswith("--supervise"), exec_start)
        self.assertIn("%i", service["EnvironmentFile"])
        self.assertEqual(service["Restart"], "on-failure")
        self.assertIn("WorkingDirectory", service)

    def test_loop_unit_runs_one_pass_and_does_not_restart(self):
        unit = parse_unit(LOOP_UNIT)
        self.assertEqual(sorted(unit.sections()),
                         ["Install", "Service", "Unit"])
        service = unit["Service"]
        exec_start = service["ExecStart"]
        self.assertIn("factory.py ${HOLOPHYTE_TARGET}", exec_start)
        self.assertNotIn("--", exec_start.split("factory.py", 1)[1])
        self.assertIn("%i", service["EnvironmentFile"])
        self.assertEqual(service["Restart"], "no")
        self.assertEqual(service["Type"], "exec")
        self.assertIn("WorkingDirectory", service)

    def test_the_operating_pages_name_the_supervise_and_loop_units(self):
        readme = DEPLOY_README.read_text()
        self.assertIn("systemctl --user enable --now holophyte.target", readme)
        self.assertIn(
            "systemctl --user enable --now holophyte-supervise@", readme)
        self.assertIn("systemctl --user start holophyte-loop@", readme)
        hosts = HOSTS.read_text()
        self.assertIn("holophyte-supervise@", hosts)
        self.assertIn("holophyte-loop@", hosts)


def host_unit(name):
    return parse_unit(DEPLOY / name)


def words(value):
    """A dependency list such as `Wants=a b` as its unit names."""
    return value.split()


def seconds(value):
    """A systemd time span written as bare seconds (`120`, `120s`)."""
    match = re.fullmatch(r"(\d+)s?", value.strip())
    if match is None:
        raise AssertionError(f"{value!r} is not a span in seconds")
    return int(match.group(1))


def worst_linear_read():
    """One Linear read at its longest: every attempt timing out, with the
    retry waits between them (`linear_provider._urlopen()`)."""
    waits = linear_provider.READ_RETRY_WAITS
    return LINEAR_ATTEMPT_SEC * (len(waits) + 1) + sum(waits)


class HostUnitTests(unittest.TestCase):
    """The five host units as the design's unit table sets them out."""

    def test_the_target_wants_the_socket_and_the_timer_and_alone_installs(self):
        target = host_unit(TARGET)
        self.assertEqual(sorted(words(target["Unit"]["Wants"])),
                         sorted([SOCKET, TIMER]))
        self.assertNotIn("Requires", target["Unit"])
        self.assertEqual(target["Install"]["WantedBy"], "default.target")
        installed = [name for name in HOST_UNITS
                     if host_unit(name).has_section("Install")]
        self.assertEqual(installed, [TARGET])

    def test_the_socket_holds_the_port_for_the_target(self):
        socket_unit = host_unit(SOCKET)
        self.assertEqual(socket_unit["Socket"]["ListenStream"],
                         "127.0.0.1:7710")
        self.assertEqual(socket_unit["Socket"]["Backlog"], "128")
        self.assertEqual(socket_unit["Unit"]["PartOf"], TARGET)

    def test_the_daemon_rides_the_socket_with_no_start_limit(self):
        serve = host_unit(SERVE)
        unit, service = serve["Unit"], serve["Service"]
        self.assertEqual(words(unit["Requires"]), [SOCKET])
        self.assertEqual(sorted(words(unit["After"])),
                         sorted([SOCKET, "network-online.target"]))
        self.assertEqual(unit["PartOf"], TARGET)
        self.assertEqual(unit["StartLimitIntervalSec"], "0")
        self.assertEqual(service["Restart"], "on-failure")
        self.assertEqual(seconds(service["RestartSec"]), 5)
        # The drain ends before the service manager's SIGKILL.
        self.assertGreater(seconds(service["TimeoutStopSec"]), DRAIN_SEC)

    def test_the_timer_fires_the_sweep_every_sweep_sec(self):
        timer = host_unit(TIMER)
        self.assertEqual(timer["Unit"]["PartOf"], TARGET)
        self.assertEqual(seconds(timer["Timer"]["OnActiveSec"]), 5)
        # The daemon judges the sweep stale after two `sweep_sec`
        # intervals, so the timer's interval is that default.
        self.assertEqual(seconds(timer["Timer"]["OnUnitActiveSec"]),
                         SWEEP_SEC)
        self.assertEqual(seconds(timer["Timer"]["AccuracySec"]), 1)
        # No `Unit=`: the timer starts the service of its own name, the
        # one `POST /actions/run-sweep` starts too.
        self.assertNotIn("Unit", timer["Timer"])
        self.assertEqual(Path(TIMER).stem, Path(SWEEP).stem)
        self.assertEqual(SWEEP, SWEEP_UNIT)

    def test_the_sweep_is_a_oneshot_bounded_above_the_worst_read(self):
        self.assertIn("timeout=%d" % LINEAR_ATTEMPT_SEC,
                      inspect.getsource(linear_provider._urlopen))
        sweep = host_unit(SWEEP)
        unit, service = sweep["Unit"], sweep["Service"]
        self.assertEqual(service["Type"], "oneshot")
        start = seconds(service["TimeoutStartSec"])
        self.assertGreater(start, worst_linear_read())
        # The daemon reads a run with no end older than this as killed.
        self.assertEqual(start, SWEEP_TIMEOUT_SEC)
        self.assertGreater(seconds(service["TimeoutStopSec"]), 0)
        self.assertEqual(words(unit["After"]), ["network-online.target"])
        # A target stop leaves a run in flight; nothing restarts one.
        self.assertNotIn("PartOf", unit)
        self.assertNotIn("Restart", service)

    def test_the_loop_template_waits_on_no_host_unit(self):
        # `After=holophyte-sweep.service` would deadlock: the loop's start
        # job waiting on the oneshot while the sweep's `systemctl start`
        # waits on the loop. And a target restart must not reach a loop.
        unit = parse_unit(LOOP_UNIT)["Unit"]
        named = " ".join(unit.get(key, "")
                         for key in ("After", "Requires", "PartOf", "BindsTo"))
        for name in HOST_UNITS:
            self.assertNotIn(name, named)


class HostUnitCommandTests(unittest.TestCase):
    """Each host service's `ExecStart` is the host form: run from this
    checkout against a home with no registry, each refuses as the host form
    refuses -- never as argparse does (exit 2) or as a project form would."""

    def run_unit(self, name, arguments):
        service = host_unit(name)["Service"]
        argv = shlex.split(service["ExecStart"])
        self.assertEqual(argv, ["/usr/bin/python3", "factory.py", *arguments])
        self.assertIn("WorkingDirectory", service)
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("LISTEN_")}
        env["HOLOPHYTE_HOME"] = home
        done = subprocess.run([sys.executable, *argv[1:]], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=60)
        return done, Path(home)

    def test_the_sweep_unit_runs_one_host_sweep(self):
        done, home = self.run_unit(SWEEP, ["--supervise", "--once"])
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("project add PATH", done.stdout)
        state = json.loads((home / "sweep.json").read_text())
        self.assertIsInstance(state["started"], int)
        self.assertIsInstance(state["ended"], int)
        self.assertEqual(state["exit"], 1)
        # One run and out: the home lock is released.
        self.assertFalse((home / "supervisor.lock").exists())

    def test_the_serve_unit_waits_for_the_handed_socket(self):
        done, _ = self.run_unit(SERVE, ["--serve"])
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("unless the service manager hands it a socket",
                      done.stderr + done.stdout)


@unittest.skipUnless(sys.platform.startswith("linux")
                     and shutil.which("systemd-analyze"),
                     "systemd-analyze --user verify needs Linux with systemd;"
                     " the operator runs it on the writer host")
class SystemdVerifyTests(unittest.TestCase):
    def test_the_host_units_verify_clean(self):
        # A copy whose WorkingDirectory is this checkout, since the shipped
        # one names a layout this host may not have.
        scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, scratch, True)
        for name in HOST_UNITS:
            text = (DEPLOY / name).read_text()
            text = re.sub(r"^WorkingDirectory=.*$", f"WorkingDirectory={ROOT}",
                          text, flags=re.MULTILINE)
            (scratch / name).write_text(text)
        done = subprocess.run(
            ["systemd-analyze", "--user", "verify",
             *(str(scratch / name) for name in HOST_UNITS)],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(done.stderr.strip(), "", done.stderr)


if __name__ == "__main__":
    unittest.main()
