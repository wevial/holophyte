"""`holo follow` streams one line per run event, ledger entry and stall as
the store gains it, and `holo status --watch` redraws the status page. Each
command runs as a real subprocess against a real store; the ssh case uses
the fake `ssh` of `tests/test_holo_ssh.py`, which runs the remote command
under `sh -c` with the host's home.

Run: python3 -m unittest discover -s tests -p 'test_holo_follow.py' -v
"""
import json
import os
import pty
import queue
import re
import select
import signal
import subprocess
import sys
import threading
import time

import store
import store.tickets
from tests.test_holo_ssh import ROOT, SshTests

WAIT = 15
POLL = "0.2"
SEVERAL_POLLS = 1.2
HOUR_MS = 3_600_000
CLEAR = "\x1b[H\x1b[2J"
SEPARATOR = re.compile(r"--- \d\d:\d\d:\d\d \S+ ---")


def now_ms():
    return int(time.time() * 1000)


class Stream:
    """A `holo` subprocess whose stdout and stderr lines are read with a
    deadline, so a missing line fails the test rather than hanging it."""

    def __init__(self, argv, env, cwd):
        self.child = subprocess.Popen(
            argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True)
        self.out, self.err = queue.Queue(), []
        threading.Thread(target=self.pump, args=(self.child.stdout, self.out.put),
                         daemon=True).start()
        self.err_done = threading.Thread(
            target=self.pump, args=(self.child.stderr, self.err.append),
            daemon=True)
        self.err_done.start()

    @staticmethod
    def pump(stream, put):
        for text in stream:
            put(text.rstrip("\n"))

    def line(self, timeout=WAIT):
        try:
            return self.out.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(f"no line within {timeout} s; stderr:"
                                 f" {''.join(self.err)!r}") from None

    def quiet(self, seconds):
        lines, deadline = [], time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            try:
                lines.append(self.out.get(timeout=left))
            except queue.Empty:
                break
        return lines

    def started(self):
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            if any("following" in text for text in self.err):
                return
            if self.child.poll() is not None:
                break
            time.sleep(0.05)
        raise AssertionError(f"follow never started; stderr: {self.err!r}")

    def stop(self):
        if self.child.poll() is None:
            os.killpg(self.child.pid, signal.SIGKILL)
        self.child.wait()
        for stream in (self.child.stdout, self.child.stderr):
            stream.close()


class FollowCase(SshTests):
    def environment(self, home):
        environment = {key: value for key, value in os.environ.items()
                       if key not in ("HOLO_PROJECT", "HOLO_TRANSPORT",
                                      "NO_COLOR")}
        environment.update(PATH=f"{self.bin}:{os.environ['PATH']}",
                           HOLOPHYTE_HOME=str(home), PYTHONPATH=str(ROOT),
                           GIT_CEILING_DIRECTORIES=str(self.root))
        return environment

    def stream(self, *args, home=None):
        stream = Stream([sys.executable, "-m", "holophyte.holo", *args],
                        self.environment(home or self.host), self.desk)
        self.addCleanup(stream.stop)
        return stream

    def follow(self, *args, home=None):
        stream = self.stream("follow", "-p", "alpha", "--every", POLL, *args,
                             home=home)
        stream.started()
        return stream

    def ticket(self, key="HOLO-1"):
        ticket = store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id=f"issue-{key}",
            linear_identifier=key, title=f"ticket {key}",
            acceptance_criteria=[f"Given {key}, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=25 * 60_000)
        store.tickets.transition(self.conn, ticket, "in_flight")
        return ticket

    def live(self, key="HOLO-1"):
        run = store.claim(self.conn, self.project_id, self.ticket(key))
        store.set_phase(self.conn, run, "working")
        return run


class FollowTests(FollowCase):
    def test_a_phase_change_a_round_and_a_merge_print_once_each_in_order(self):
        run = store.claim(self.conn, self.project_id, self.ticket())
        follow = self.follow()
        store.set_phase(self.conn, run, "working")
        store.record_ledger(self.conn, run, "round", "r1 approve: no findings")
        store.record_ledger(self.conn, run, "merge", "merged 92ef2b0 into main")

        lines = [follow.line() for _ in range(3)]

        summaries = ["claimed -> working", "round: r1 approve: no findings",
                     "merge: merged 92ef2b0 into main"]
        for text, summary in zip(lines, summaries):
            self.assertRegex(text, r"^\d\d:\d\d:\d\d  ")
            self.assertTrue(text.endswith(f"  HOLO-1  {summary}"), text)
        self.assertEqual(follow.quiet(SEVERAL_POLLS), [])

    def test_an_entry_written_before_follow_started_is_not_printed(self):
        run = self.live()
        store.record_ledger(self.conn, run, "note", "written before follow")
        follow = self.follow()
        store.record_ledger(self.conn, run, "note", "written after follow")

        self.assertTrue(follow.line().endswith("note: written after follow"))
        self.assertEqual(follow.quiet(SEVERAL_POLLS), [])

    def test_since_an_hour_prints_the_entry_written_before_follow_started(self):
        run = self.live()
        store.record_ledger(self.conn, run, "note", "written before follow")
        follow = self.follow("--since", "1h")
        store.record_ledger(self.conn, run, "note", "written after follow")

        lines = [follow.line()]
        while not lines[-1].endswith("written after follow"):
            lines.append(follow.line())
        notes = [text.split("  HOLO-1  ")[1] for text in lines
                 if "  HOLO-1  note: " in text]
        self.assertEqual(notes, ["note: written before follow",
                                 "note: written after follow"])

    def test_a_stale_heartbeat_prints_one_line_until_it_recovers(self):
        run = self.live()
        follow = self.follow()

        store.heartbeat(self.conn, run, now=now_ms() - HOUR_MS)
        first = follow.line()
        self.assertRegex(
            first, rf"  ✗  HOLO-1  run {run} silent: heartbeat .+ ago \(working\)$")
        self.assertEqual(follow.quiet(SEVERAL_POLLS), [])

        store.heartbeat(self.conn, run)
        self.assertEqual(follow.quiet(SEVERAL_POLLS), [])
        store.heartbeat(self.conn, run, now=now_ms() - HOUR_MS)
        self.assertRegex(follow.line(), rf"  ✗  HOLO-1  run {run} silent")

    def test_sigint_exits_zero_without_a_traceback(self):
        run = self.live()
        follow = self.follow()
        store.record_ledger(self.conn, run, "note", "the loop is polling")
        follow.line()

        follow.child.send_signal(signal.SIGINT)

        self.assertEqual(follow.child.wait(timeout=WAIT), 0)
        follow.err_done.join(timeout=WAIT)
        self.assertNotIn("Traceback", "\n".join(follow.err))

    def test_json_prints_each_entry_as_one_object_per_line(self):
        run = self.live()
        follow = self.follow("--json")
        store.record_ledger(self.conn, run, "round", "r2 changes requested")

        entry = json.loads(follow.line())

        self.assertEqual((entry["ticket"], entry["kind"], entry["summary"]),
                         ("HOLO-1", "round", "r2 changes requested"))

    def test_over_ssh_each_line_is_rendered_as_it_arrives(self):
        run = self.live()
        follow = self.follow(home=self.seat)
        self.assertIn("follow", self.calls()[0][-1])
        store.record_ledger(self.conn, run, "merge", "merged over ssh")

        self.assertTrue(follow.line().endswith("  ✓  HOLO-1  merge: merged over ssh"))
        self.assertIsNone(follow.child.poll())


class WatchTests(FollowCase):
    def park(self, key):
        ticket = self.ticket(key)
        run = store.claim(self.conn, self.project_id, ticket)
        for phase in ("working", "verifying", "reviewing", "merge_gate"):
            store.set_phase(self.conn, run, phase)
        store.park(self.conn, run, "awaiting_merge_approval")
        store.tickets.transition(self.conn, ticket, "blocked_on_operator")
        store.set_question(self.conn, ticket, "ready to merge, waiting on you")

    def frames(self, watch):
        self.assertRegex(watch.line(), SEPARATOR)
        frame = []
        while True:
            text = watch.line()
            if SEPARATOR.fullmatch(text):
                yield frame
                frame = []
            else:
                frame.append(text)

    def test_piped_frames_follow_a_timed_separator_and_show_a_new_park(self):
        frames = self.frames(self.stream("status", "--watch", POLL, "-p", "alpha"))
        before = next(frames)
        self.assertTrue(before[0].startswith("project "), before)
        self.assertNotIn("HOLO-7", "\n".join(before))

        self.park("HOLO-7")

        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            frame = next(frames)
            if any(text.startswith("Needs you") for text in frame):
                break
        else:
            self.fail("no frame named the parked ticket")
        needs = frame[frame.index("Needs you (1)") + 1]
        self.assertIn("HOLO-7", needs)
        self.assertIn("ready to merge, waiting on you", needs)

    def test_on_a_terminal_each_frame_follows_a_clear_screen(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        child = subprocess.Popen(
            [sys.executable, "-m", "holophyte.holo", "status", "--watch",
             POLL, "-p", "alpha"], env=self.environment(self.host),
            cwd=self.desk, stdin=subprocess.DEVNULL, stdout=slave,
            stderr=subprocess.DEVNULL)
        os.close(slave)
        output, deadline = "", time.monotonic() + WAIT
        while output.count(CLEAR) < 3 and time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.5)
            if ready:
                output += os.read(master, 65536).decode()
        child.send_signal(signal.SIGINT)
        self.assertEqual(child.wait(timeout=WAIT), 0)

        self.assertTrue(output.startswith(CLEAR), output[:80])
        frames = output.split(CLEAR)[1:3]
        self.assertEqual(len(frames), 2, output)
        for frame in frames:
            self.assertTrue(frame.startswith("project "), frame[:80])
