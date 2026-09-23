"""Heartbeats as a busy runner, or a dead beat, would deliver them (KO-674).

`heartbeat_while()`'s timer thread beats through `holophyte.runs._heartbeat`,
so patching it reaches every beat taken inside a slow step, while the block's
exit beat and the phase writes keep their own path.
"""
import shlex
import sys
import time
from unittest.mock import patch

import holophyte.runs

# Real milliseconds a busy runner adds before each beat.
LOADED_MS = 400
# Milliseconds between a `heartbeat_sampler()`'s samples.
SAMPLE_MS = 200


def heartbeat_sampler(db, samples, seconds):
    """A shell command that, for `seconds`, appends the run's phase and
    `lastHeartbeat` from store `db` to `samples` every `SAMPLE_MS`."""
    script = (
        "import sqlite3, time\n"
        f"deadline = time.monotonic() + {seconds}\n"
        f"conn = sqlite3.connect({str(db)!r})\n"
        "while time.monotonic() < deadline:\n"
        f"    time.sleep({SAMPLE_MS / 1000})\n"
        "    row = conn.execute('SELECT phase, lastHeartbeat FROM runs')"
        ".fetchone()\n"
        f"    open({str(samples)!r}, 'a').write('%s %s\\n' % row)\n")
    return f"{sys.executable} -c {shlex.quote(script)}"


def patch_beats(test, delay_ms=0, silent=False):
    """For the rest of `test`, each timer beat first sleeps `delay_ms` of real
    time; a `silent` beat then writes nothing yet reports the run live: the
    heartbeat stopped under a worker that is still going."""
    beat = holophyte.runs._heartbeat

    def delayed(conn, run_id, swept):
        time.sleep(delay_ms / 1000)
        return True if silent else beat(conn, run_id, swept)

    test.enterContext(patch.object(holophyte.runs, "_heartbeat", delayed))
