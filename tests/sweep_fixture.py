"""The sweep tests' shared fixture: a store with one project, runs placed in
time by hand, and the stubs both the sweep tests and the supervise tests use.

Not a test module: discovery never imports it by its own name, so the base
class here carries no test methods and is safe to import from both sides.
"""
from __future__ import annotations

import io
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
import holophyte.cli  # noqa: E402 - after the sys.path insert above
import holophyte.sweep_report  # noqa: E402 - after the sys.path insert above
import holophyte.target  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from tests.phase_fixture import advance_phase, seed_observed_phase  # noqa: E402

MINUTE = 60 * 1000
T0 = 1_700_000_000_000  # an epoch-millisecond wall clock the tests do sums on


class Tripwire:
    """Allow startup imports, but reject every attempted module API access."""

    def __init__(self, what):
        self.what, self.__spec__ = what, None

    def __getattr__(self, name):
        raise AssertionError(f"{self.what}.{name} was reached")


def no_network():
    """Fail any attempt to open a socket, at the one call urllib makes."""
    def refuse(*args, **kwargs):
        raise AssertionError("a network connection was attempted")

    return patch.multiple(socket, socket=refuse, create_connection=refuse)


class SweepTestCase(unittest.TestCase):
    """A store with one project, and runs the test places in time by hand."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.target = self.root / "repo"
        self.target.mkdir()
        # Where `Target.locate(self.target)` will look: the target's directory
        # under a HOLOPHYTE_HOME of this test's own, never the operator's real
        # one.
        home = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(self.root / "home")})
        home.start()
        self.addCleanup(home.stop)
        self.db = holophyte.target.state_dir(self.target) / "store.db"
        self.db.parent.mkdir(parents=True)
        # The `Target` every sweep here is handed. The acting sweep writes
        # FINDINGS.md into whichever target it names, so it is this test's
        # repository and never the one this suite is running in.
        self.tgt = holophyte.target.Target.locate(self.target)
        self.conn = store.open(str(self.db), migrate="owner")
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.projects = 1
        self.project = store.tickets.ensure_project(self.conn, "team-1", self.target)
        self.tickets = 0
        self.ticket_of = {}

    def another_project(self):
        """A second project, for the tests that want two runs live on two
        targets rather than two tickets of one."""
        self.projects += 1
        repo = self.root / f"repo-{self.projects}"
        repo.mkdir()
        return store.tickets.ensure_project(self.conn, f"team-{self.projects}", repo)

    def a_run(self, budget_min=25, claimed_at=T0, phase="working",
              project=None, ticket=None, active_work=False):
        """Claim one ticket at claimed_at, optionally doing uninterrupted work.

        An existing ticket can be reclaimed; otherwise create an in_flight ticket.
        active_work opens a persisted interval for budget-boundary fixtures."""
        project = self.project if project is None else project
        if ticket is None:
            self.tickets += 1
            n = self.tickets
            ticket = store.tickets.mirror_ticket(
                self.conn, project, linear_issue_id=f"issue-{n}",
                linear_identifier=f"KO-{n}", title=f"ticket {n}",
                acceptance_criteria=[f"Given ticket {n}, then it is worked"],
                verification_commands=["echo ok"],
                time_box_ms=budget_min and budget_min * MINUTE)
            store.tickets.transition(self.conn, ticket, "in_flight")
        run_id = store.claim(self.conn, project, ticket, now=claimed_at)
        self.ticket_of[run_id] = ticket
        if phase != "claimed":
            if phase in {"blocked_on_operator", "squashing"}:
                seed_observed_phase(self.conn, run_id, phase, now=claimed_at)
            else:
                advance_phase(self.conn, run_id, phase, now=claimed_at)
        if active_work:
            self.conn.execute('UPDATE runs SET workStartedAt = ? WHERE id = ?',
                              (claimed_at, run_id))
            self.conn.commit()
        return run_id

    def heartbeat_at(self, run_id, at):
        """Move the run's last heartbeat, the way a stage boundary would."""
        store.set_phase(self.conn, run_id, store.run_phase(self.conn, run_id),
                        now=at)

    def configure(self, toml):
        """Give the target a config file and a `Target` that reads it, the
        way `cli()`'s target does -- a `Target` parses its config once."""
        (self.db.parent / "config.toml").write_text(toml)
        self.tgt = holophyte.target.Target.locate(self.target)

    def run_sweep(self, at, *flags):
        """The mode end to end, with the provider and the network as tripwires.

        The mode reads the wall clock, which is the one thing about it a test
        cannot arrange, so `time` is what `at` replaces -- the seam the sweep
        itself takes as a parameter.
        """
        holophyte.cli.eager_import()  # Resolve the build before hiding git on PATH.
        out = io.StringIO()
        # No `docker` either: the review-container check asks the host's
        # daemon, and these tests are about the store.
        no_docker = self.root / "no-docker-bin"
        no_docker.mkdir(exist_ok=True)
        with patch.dict(sys.modules,
                        {"linear_provider": Tripwire("linear_provider")}), \
                patch.dict(os.environ, {"PATH": str(no_docker)}):
            with no_network(), patch.object(sys, "stdout", out), \
                    patch.object(holophyte.sweep_report, "time", lambda: at / 1000):
                holophyte.cli.cli(["--sweep", *flags, str(self.target)])
        return out.getvalue().splitlines()

    def strikes(self, run_id):
        """The strike row the sweep keeps, read straight out of the table."""
        row = self.conn.execute(
            "SELECT strikes, lastSeen FROM sweepStrikes WHERE runId = ?",
            (run_id,)).fetchone()
        return row


class StubProvider:
    """The board, recording what it is told rather than telling Linear.

    Only the escalating sweep needs one: a swept failure below the threshold
    pushes no status and comments on nothing, which is why the other acting
    tests can leave the provider as the tripwire it is in the mode tests.

    `ready` is what `ready_issues()` answers the supervisor's empty-mirror
    fall-through (KO-411): a list of issues, or an exception to raise; the
    ask is counted in `ready_asked`. `team` is the board's identifier, the
    key the fall-through finds the mirror's project row by (KO-420) --
    "team-1" is the one `setUp` ensures.
    """

    def __init__(self, ready=(), team="team-1"):
        self.states = []
        self.comments = []
        self.ready = ready
        self.ready_asked = 0
        self.team = team

    def ready_issues(self):
        self.ready_asked += 1
        if isinstance(self.ready, BaseException):
            raise self.ready
        return list(self.ready)

    # The board lease label (KO-351): what the loop labelled and unlabelled,
    # per issue, so the stub answers the claim's and the close-out's calls.
    def label_issue(self, issue_id, name):
        self.__dict__.setdefault("labels", {}).setdefault(issue_id, [])
        if name not in self.labels[issue_id]:
            self.labels[issue_id].append(name)

    def unlabel_issue(self, issue_id, name):
        self.__dict__.setdefault("labels", {}).setdefault(issue_id, [])
        self.labels[issue_id] = [n for n in self.labels[issue_id] if n != name]

    def issue_labels(self, issue_id):
        return list(self.__dict__.setdefault("labels", {}).get(issue_id, []))

    def set_state(self, issue_id, state):
        self.states.append((issue_id, state))

    def comment(self, issue_id, body):
        self.comments.append((issue_id, body))
