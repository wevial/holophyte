"""The whole suite run inside a real isolated implementer launch."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


class ContainerSuiteTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("HOLOPHYTE_TEST_DOCKER") == "1",
        "set HOLOPHYTE_TEST_DOCKER=1 for container integration",
    )
    def test_whole_suite_passes_inside_an_isolated_launch(self):
        from holophyte.config.project import Project
        from holophyte.isolation import isolation
        from holophyte.isolation.isolation_git import git

        if not shutil.which("docker"):
            self.skipTest("Docker absent")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        clone = root / "checkout"
        git(root, "clone", "-q", str(ROOT), str(clone))
        with patch.dict(os.environ, {"HOLOPHYTE_HOME": str(root / "home")}):
            project = Project.locate(clone, adopt=False)
            project.config_path.parent.mkdir(parents=True)
            project.config_path.write_text(
                "[agents]\n"
                'implementer_isolation = { backend = "container",'
                ' memory = "4g", writable = true }\n'
            )
            code, output = isolation.launch(
                isolation.route_for(project), clone,
                isolation.environment(project),
                ["python3", "tests/run_modules.py", "--jobs", "4"],
                timeout=1500, project=project,
            )
        self.assertEqual(code, 0, output)
        summary = output.strip().splitlines()[-1]
        self.assertRegex(summary, r"^\d+ modules, \d+ tests, 0 failed, ", output)
