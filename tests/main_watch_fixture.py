"""A `gh` on PATH answering main's GitHub reads per project, and one host
sweep pass: the factory's real `github.rest()` runs it as a subprocess.

Each project's answers are a JSON file named for its checkout directory,
mapping an API path less its query to the answer and an optional delay;
every call is logged as `DIRECTORY PATH`."""
import io
import json
import os
import stat
import sys
from unittest.mock import patch

import holophyte.host.sweep_host as sweep_host
from holophyte.config.project import Project
from holophyte.host.registry import Host
from tests.host_fixture import git

GH = """#!PYTHON
import json, os, sys, time
from pathlib import Path
home = Path(os.environ["FAKE_GH_DIR"])
name, path = Path.cwd().name, sys.argv[-1].split("?")[0]
with open(home / "calls.log", "a") as log:
    log.write(f"{name} {path}\\n")
answers = home / f"{name}.json"
found = json.loads(answers.read_text()).get(path) if answers.exists() else None
if found is None:
    sys.exit(print("gh: Not Found (HTTP 404)", file=sys.stderr) or 1)
time.sleep(found.get("sleep", 0))
print(json.dumps(found["answer"]))
"""


def job(name, conclusion, job_id, run_id, status="completed"):
    """A GitHub Actions check run as the check-runs read lists it."""
    return {"name": name, "status": status, "conclusion": conclusion,
            "id": job_id, "app": {"slug": "github-actions"},
            "html_url": f"https://github.com/o/r/actions/runs/{run_id}"
                        f"/job/{job_id}"}


class FakeMainGitHub:
    def __init__(self, case, directory):
        self.dir = directory
        self.dir.mkdir()
        script = self.dir / "gh"
        script.write_text(GH.replace("PYTHON", sys.executable))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        environ = patch.dict(os.environ, {
            "FAKE_GH_DIR": str(self.dir),
            "PATH": f"{self.dir}{os.pathsep}{os.environ['PATH']}"})
        environ.start()
        case.addCleanup(environ.stop)

    def origin(self, path):
        git(path, "remote", "add", "origin",
            f"https://github.com/o/{path.name}.git")

    def main(self, path, sha, runs, required=("unit",), head_sleep=0):
        repo = f"repos/o/{path.name}"
        answers = {
            f"{repo}/commits/main": {"answer": {"sha": sha},
                                     "sleep": head_sleep},
            f"{repo}/commits/{sha}/check-runs": {"answer": {
                "total_count": len(runs), "check_runs": runs}},
            f"{repo}/rules/branches/main": {"answer": [{
                "type": "required_status_checks", "parameters": {
                    "required_status_checks": [{"context": context}
                                               for context in required]}}]},
            f"{repo}/branches/main": {"answer": {"name": "main"}},
        }
        (self.dir / f"{path.name}.json").write_text(json.dumps(answers))

    def head_reads(self, path):
        log = self.dir / "calls.log"
        lines = log.read_text().splitlines() if log.exists() else []
        return [line for line in lines
                if line == f"{path.name} repos/o/{path.name}/commits/main"]


def automations(path, mode):
    config = Project.locate(path, adopt=False).config_path
    with config.open("a") as handle:
        handle.write(f'\n[automations.main_watch]\nmode = "{mode}"\n')


def sweep_pass(at):
    """One `--supervise --once` pass at `at` with no board; its output."""
    out = io.StringIO()
    with patch.object(sweep_host, "_provider", lambda _target: None), \
            patch("holophyte.host.supervisor.linear_budget_low",
                  return_value=False), \
            patch("holophyte.host.supervisor.start_loop_for",
                  lambda *_a, **_k: None):
        sweep_host.supervise_host(Host.locate(), once=True, out=out,
                                  clock=lambda: at)
    return out.getvalue()
