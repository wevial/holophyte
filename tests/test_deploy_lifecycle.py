"""The host units under a real systemd user manager (consolidation stage 4).

Linux only, and opt-in. The class installs renamed copies of the five host
units and of the loop template (`holophyte-lifecycle*`) into the running
user's manager, drives them, and removes them. Run it as a second Linux
user, or inside a `systemd-nspawn` container, never under the manager of the
user that runs the factory. It runs only when `HOLOPHYTE_LIFECYCLE_USER`
names the user running it, and it refuses (an error, not a skip) a manager
that has any other `holophyte*` unit file, or a home with a
`~/.holophyte/host.toml`. From that user's own checkout of the branch:

    sudo loginctl enable-linger holotest
    sudo machinectl shell holotest@    # a login with XDG_RUNTIME_DIR set
    HOLOPHYTE_LIFECYCLE_USER=holotest python3 -m unittest \\
        tests.test_deploy_lifecycle -v

What the copies change, and why: the unit names, so nothing collides with
a real unit; `ListenStream`, a free port; `WorkingDirectory`, a committed
copy of this factory (`host_fixture.factory_checkout()`) whose code check
runs every 2 s, so a commit there moves the daemon's `HEAD`; the interpreter,
this one; `HOLOPHYTE_HOME`, a throwaway home under this user's home (the
daemon's `PrivateTmp` hides `/tmp`) holding one registered project; the
sweep's command, `tests/lifecycle_sweep.py` (the same host sweep, which
counts its starts and can be held after writing `started`); and the loop's,
a `sleep`, since the unit graph is under test, not the loop. Every timing
is the shipped one, so the class takes about eight minutes.
"""
from __future__ import annotations

import getpass
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from holophyte.project import Project
from holophyte.status import HOME_LOCK, SWEEP_STATE
from holophyte.supervisor_lock import pid_alive, read_supervisor_lock
from tests.host_fixture import factory_checkout, git

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
USER = os.environ.get("HOLOPHYTE_LIFECYCLE_USER")
PREFIX = "holophyte-lifecycle"
# Shipped unit file name -> the name it is installed under here.
RENAMED = {
    "holophyte.target": f"{PREFIX}.target",
    "holophyte-serve.socket": f"{PREFIX}-serve.socket",
    "holophyte-serve.service": f"{PREFIX}-serve.service",
    "holophyte-sweep.timer": f"{PREFIX}-sweep.timer",
    "holophyte-sweep.service": f"{PREFIX}-sweep.service",
    "holophyte-loop@.service": f"{PREFIX}-loop@.service",
}
TARGET = RENAMED["holophyte.target"]
SOCKET = RENAMED["holophyte-serve.socket"]
SERVE = RENAMED["holophyte-serve.service"]
TIMER = RENAMED["holophyte-sweep.timer"]
SWEEP = RENAMED["holophyte-sweep.service"]
LOOP = f"{PREFIX}-loop@probe.service"
DEPENDENCY_KEYS = ("Wants", "Requires", "After", "PartOf")
# The shipped timeouts the waits below are measured against.
TIMEOUT_START_SEC = 120
TIMEOUT_STOP_SEC = 30
CODE_CHECK_SEC = 2


def systemctl(*args, check=True, timeout=60):
    done = subprocess.run(["systemctl", "--user", *args], capture_output=True,
                          text=True, timeout=timeout)
    if check and done.returncode != 0:
        raise AssertionError(f"systemctl --user {' '.join(args)} exited"
                             f" {done.returncode}: {done.stderr.strip()}")
    return done.stdout


def show(unit, prop):
    return systemctl("show", "-p", prop, "--value", unit).strip()


def wait_for(predicate, seconds, step=0.5):
    """Poll `predicate` until it is truthy or `seconds` pass; its last value."""
    deadline = time.monotonic() + seconds
    while True:
        value = predicate()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(step)


def render(text, overrides, extra=()):
    """A shipped unit with its unit names renamed, the keys in `overrides`
    replaced (a value of None drops the line) and `extra` lines added after
    `[Service]`. Every override must meet its key, so a unit that loses one
    fails here instead of running with the shipped value."""
    lines, met = [], set()
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in overrides:
            met.add(key)
            if overrides[key] is not None:
                lines.append(f"{key}={overrides[key]}")
            continue
        if sep and key in DEPENDENCY_KEYS:
            line = f"{key}=" + " ".join(RENAMED.get(name, name)
                                        for name in value.split())
        lines.append(line)
        if line == "[Service]":
            lines.extend(extra)
    missing = set(overrides) - met
    if missing:
        raise AssertionError(f"the unit has no {sorted(missing)} to replace")
    return "\n".join(lines) + "\n"


def settled(unit):
    """True once `unit` has reached a state systemd leaves it in: not
    `activating` and not `deactivating`, whose stop may still be waiting
    out `TimeoutStopSec` before its SIGKILL."""
    return show(unit, "ActiveState") in ("inactive", "failed", "active")


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def refuse_a_live_manager():
    """RuntimeError unless this user's manager and home are disposable."""
    if USER != getpass.getuser():
        raise RuntimeError(
            f"HOLOPHYTE_LIFECYCLE_USER={USER!r} but this is"
            f" {getpass.getuser()!r}: name the user whose manager may be used")
    if (Path.home() / ".holophyte" / "host.toml").exists():
        raise RuntimeError(f"{Path.home()}/.holophyte/host.toml exists: this"
                           " user runs a factory; use a second user")
    listed = systemctl("list-unit-files", "holophyte*", "--no-legend")
    others = [line.split()[0] for line in listed.splitlines()
              if line.strip() and not line.startswith(PREFIX)]
    if others:
        raise RuntimeError(f"this manager has Holophyte units {others}; use a"
                           " second user or a container")


@unittest.skipUnless(sys.platform.startswith("linux") and USER,
                     "needs Linux, a systemd user manager and"
                     " HOLOPHYTE_LIFECYCLE_USER naming a disposable user; the"
                     " operator runs it before the rehearsal")
class HostUnitLifecycleTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        refuse_a_live_manager()
        cache = Path.home() / ".cache"
        cache.mkdir(exist_ok=True)
        cls.scratch = Path(tempfile.mkdtemp(prefix=PREFIX, dir=cache))
        cls.units = Path.home() / ".config" / "systemd" / "user"
        cls.units.mkdir(parents=True, exist_ok=True)
        cls.home = cls.scratch / "home"
        cls.checkout = factory_checkout(unittest.TestCase(),
                                        cls.scratch / "factory", CODE_CHECK_SEC)
        cls.port = free_port()
        cls.register()
        cls.install()

    @classmethod
    def register(cls):
        """One project, `probe`, in the throwaway home's registry."""
        repo = cls.scratch / "probe"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        env = {**os.environ, "HOLOPHYTE_HOME": str(cls.home)}
        with patch.dict(os.environ, {"HOLOPHYTE_HOME": str(cls.home)}):
            target = Project.locate(repo, adopt=False)
        target.holo_dir.mkdir(parents=True, exist_ok=True)
        target.config_path.write_text(
            '[board]\nteam = "team-probe"\nproject_id = "p-probe"\n'
            '[serve]\nname = "probe"\n')
        subprocess.run([sys.executable, "factory.py", "project", "add",
                        str(repo)], cwd=cls.checkout, env=env, check=True,
                       capture_output=True)
        cls.host_toml = cls.home / "host.toml"

    @classmethod
    def install(cls):
        python = sys.executable
        home = f"Environment=HOLOPHYTE_HOME={cls.home}"
        work = str(cls.checkout)
        plans = {
            "holophyte.target": ({}, ()),
            "holophyte-serve.socket": (
                {"ListenStream": f"127.0.0.1:{cls.port}"}, ()),
            "holophyte-serve.service": (
                {"WorkingDirectory": work,
                 "ExecStart": f"{python} factory.py --serve"}, (home,)),
            "holophyte-sweep.timer": ({}, ()),
            "holophyte-sweep.service": (
                {"WorkingDirectory": work,
                 "ExecStart": f"{python} {ROOT / 'tests' / 'lifecycle_sweep.py'}"},
                (home, "Environment=LINEAR_API_KEY=",
                 f"Environment=HOLOPHYTE_LIFECYCLE_DIR={cls.scratch}")),
            "holophyte-loop@.service": (
                {"WorkingDirectory": work, "EnvironmentFile": None,
                 "ExecStart": "/bin/sleep 3600"}, ()),
        }
        for shipped, (overrides, extra) in plans.items():
            text = render((DEPLOY / shipped).read_text(), overrides, extra)
            (cls.units / RENAMED[shipped]).write_text(text)
        systemctl("daemon-reload")

    @classmethod
    def tearDownClass(cls):
        (cls.scratch / "hold").unlink(missing_ok=True)
        for unit in (TARGET, SWEEP, LOOP, SERVE, SOCKET, TIMER):
            systemctl("stop", unit, check=False, timeout=90)
        for name in RENAMED.values():
            (cls.units / name).unlink(missing_ok=True)
        systemctl("daemon-reload", check=False)
        systemctl("reset-failed", f"{PREFIX}*", check=False)
        shutil.rmtree(cls.scratch, ignore_errors=True)

    # -- helpers ---------------------------------------------------------

    def fresh(self, hold=False):
        """Everything stopped and forgotten, then the target started: the
        timer's first fire is `OnActiveSec` from now."""
        (self.scratch / "hold").unlink(missing_ok=True)
        systemctl("stop", TARGET, timeout=90)
        systemctl("stop", SWEEP, timeout=90)
        systemctl("reset-failed", f"{PREFIX}*", check=False)
        (self.scratch / "starts").unlink(missing_ok=True)
        if hold:
            (self.scratch / "hold").touch()
        systemctl("start", TARGET)
        self.addCleanup(lambda: (self.scratch / "hold").unlink(missing_ok=True))

    def starts(self):
        path = self.scratch / "starts"
        if not path.exists():
            return []
        return [int(line.split()[0]) for line in path.read_text().splitlines()
                if line.strip()]

    def sweep_state(self):
        path = self.home / SWEEP_STATE
        return json.loads(path.read_text()) if path.exists() else {}

    def get(self, path="/status", timeout=30):
        """The status code of GET `path`, or the error's text."""
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}{path}",
                    timeout=timeout) as answer:
                return answer.status
        except urllib.error.HTTPError as bad:
            return bad.code
        except OSError as bad:
            return f"{type(bad).__name__}: {bad}"

    # -- the design's lifecycle list ---------------------------------------

    def test_the_timer_fires_twice_in_130_seconds(self):
        self.fresh()
        fired = wait_for(lambda: len(self.starts()) >= 2, 130)
        self.assertTrue(fired, f"starts in 130 s: {self.starts()}")
        ended = wait_for(lambda: self.sweep_state().get("ended"), 30)
        self.assertIsInstance(ended, int)

    def test_a_run_held_past_the_interval_is_not_joined(self):
        self.fresh(hold=True)
        self.assertTrue(wait_for(lambda: len(self.starts()) == 1, 20))
        # Past the first fire's `OnUnitActiveSec` (60 s after it started).
        time.sleep(75)
        self.assertEqual(len(self.starts()), 1, self.starts())
        self.assertEqual(show(SWEEP, "ActiveState"), "activating")
        (self.scratch / "hold").unlink()
        self.assertTrue(wait_for(lambda: settled(SWEEP), 30),
                        show(SWEEP, "ActiveState"))

    def test_a_run_held_past_its_start_timeout_is_stopped_and_reclaimed(self):
        self.fresh(hold=True)
        self.assertTrue(wait_for(lambda: len(self.starts()) == 1, 20))
        killed = self.starts()[0]
        # No second fire may reclaim the lock before it is looked at.
        systemctl("stop", TIMER)
        # At TimeoutStartSec the unit goes to `deactivating` with the held
        # run still alive (it ignores SIGTERM); only TimeoutStopSec's SIGKILL
        # ends it. Wait for both the unit's end and the process's.
        done = wait_for(lambda: settled(SWEEP) and not pid_alive(killed),
                        TIMEOUT_START_SEC + TIMEOUT_STOP_SEC + 30, step=2)
        self.assertTrue(done, f"the held run {killed} outlived TimeoutStartSec"
                        f" plus TimeoutStopSec: {show(SWEEP, 'ActiveState')}")
        self.assertEqual(show(SWEEP, "ActiveState"), "failed")
        self.assertEqual(show(SWEEP, "Result"), "timeout")
        state = self.sweep_state()
        self.assertEqual(state.get("pid"), killed)
        self.assertIsNone(state.get("ended"), state)
        lock = self.home / HOME_LOCK
        self.assertEqual(read_supervisor_lock(lock)[0], killed)
        self.assertFalse(pid_alive(killed))
        (self.scratch / "hold").unlink()
        systemctl("start", SWEEP, check=False, timeout=TIMEOUT_START_SEC + 30)
        state = self.sweep_state()
        self.assertEqual(state.get("interrupted", {}).get("pid"), killed, state)
        self.assertGreaterEqual(state.get("ended") or 0, state["started"])
        self.assertFalse(lock.exists())

    def test_a_daemon_failing_five_times_leaves_the_port_held(self):
        good = self.host_toml.read_text()
        self.addCleanup(self.host_toml.write_text, good)
        self.fresh()
        systemctl("stop", TARGET)
        self.host_toml.write_text("[[project]\n")
        systemctl("reset-failed", f"{PREFIX}*", check=False)
        systemctl("start", TARGET)
        waiting = socket.create_connection(("127.0.0.1", self.port), 5)
        self.addCleanup(waiting.close)
        restarts = wait_for(
            lambda: int(show(SERVE, "NRestarts") or 0) >= 5, 60, step=1)
        self.assertTrue(restarts, show(SERVE, "NRestarts"))
        self.assertEqual(show(SOCKET, "ActiveState"), "active")
        again = socket.create_connection(("127.0.0.1", self.port), 5)
        again.close()
        self.host_toml.write_text(good)
        self.assertEqual(wait_for(lambda: self.get(timeout=5) == 200, 30),
                         True)

    def test_restart_and_stop_of_the_target_leave_a_loop_running(self):
        self.fresh()
        self.assertEqual(self.get(), 200)
        systemctl("start", LOOP)
        self.addCleanup(systemctl, "stop", LOOP, check=False)
        loop_pid = show(LOOP, "MainPID")
        before = {unit: show(unit, "InvocationID")
                  for unit in (SOCKET, SERVE, TIMER)}
        systemctl("restart", TARGET, timeout=90)
        for unit, invocation in before.items():
            with self.subTest(after="restart", unit=unit):
                self.assertEqual(show(unit, "ActiveState"), "active")
                self.assertNotEqual(show(unit, "InvocationID"), invocation)
        self.assertEqual(self.get(), 200)
        self.assertEqual(show(LOOP, "MainPID"), loop_pid)
        systemctl("stop", TARGET, timeout=90)
        for unit in (SOCKET, SERVE, TIMER):
            with self.subTest(after="stop", unit=unit):
                self.assertEqual(show(unit, "ActiveState"), "inactive")
        self.assertEqual(show(LOOP, "ActiveState"), "active")
        self.assertEqual(show(LOOP, "MainPID"), loop_pid)

    def test_a_request_during_a_head_move_is_answered_by_the_next_daemon(self):
        self.fresh()
        self.assertEqual(self.get(), 200)
        first = show(SERVE, "MainPID")
        git(self.checkout, "commit", "-q", "--allow-empty", "-m", "B")
        answers = []
        deadline = time.monotonic() + CODE_CHECK_SEC + 45
        while time.monotonic() < deadline:
            answers.append(self.get(timeout=30))
            now = show(SERVE, "MainPID")
            if now not in ("0", first) and answers[-1] == 200:
                break
            time.sleep(0.2)
        self.assertNotEqual(show(SERVE, "MainPID"), first)
        self.assertEqual([code for code in answers if code != 200], [])


if __name__ == "__main__":
    unittest.main()
