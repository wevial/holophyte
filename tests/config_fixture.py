"""The shared `ConfigTestCase` fixture the per-target config tests build on.

Both `test_factory_config.py` and `test_config_tables.py` derive from it; it
carries no test methods, so importing it into either does not re-discover
anything.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.cli
import holophyte.project


class FakeChild:
    """What the stubbed `Popen` hands back: a pid and nothing else."""
    pid = 4242


class ConfigTestCase(unittest.TestCase):
    """Build a `Project` at a throwaway repository, optionally with a config.

    `Project.locate()` is the only thing that derives a target's paths, so the
    tests go through it rather than assembling a `Project` by hand: a test
    that set the config path itself would pass even if the file were never
    wired into the path `cli()` derives at all.
    """

    def locate(self, config=None):
        """The `Project` for a fresh repository under a fresh home, as
        `self.project`."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.set_home(self.home)
        self.target = self.root / "repo"
        self.target.mkdir()
        self.project = holophyte.project.Project.locate(self.target)
        if config is not None:
            self.write_config(config)
        self.stub_supervisor_spawn()
        return self.project

    def stub_supervisor_spawn(self):
        """Replace the `Popen` seam the loop path starts a supervisor through.

        Every test that reaches the loop path goes through here: the real
        one would leave a detached `--supervise` running against the
        throwaway home after the test. `self.popen` records the calls, so a
        test about the spawn reads what would have been started.
        """
        patcher = patch.object(holophyte.cli, "SPAWN", return_value=FakeChild())
        self.popen = patcher.start()
        self.addCleanup(patcher.stop)

    def set_home(self, home):
        """Point HOLOPHYTE_HOME at a throwaway directory for this test.

        Every test in this file goes through here: state now lives under a
        home directory, and a test that let the real `~/.holophyte` stand
        would read and write the operator's own stores.
        """
        patcher = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_config(self, config):
        """Put `config` where `self.project` will look for it.

        Before anything has read it: a `Project` parses its config once, so a
        file written after the first read would be a file nobody reads.
        """
        self.project.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.project.config_path.write_text(config)
