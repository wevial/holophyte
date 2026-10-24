"""Stub `claude` and `codex` lead CLIs for scout tests.

Each stub logs its argv, working directory and the scouts' recorded lead
sessions at launch, then answers by the goal's opening line: `ready` to a
probe, and the test's own text to a plan or report turn.
"""
import json
import os
import stat
import sys
from pathlib import Path

STUB = r'''#!{python}
import json, os, sqlite3, subprocess, sys, time
from pathlib import Path

here = Path(__file__).resolve().parent
answers = json.loads((here / "answers.json").read_text())
goal = sys.argv[-1]
turn = ("plan" if goal.startswith("Scout plan turn")
        else "report" if goal.startswith("Scout report turn") else "probe")
sessions = None
if answers.get("store"):
    with sqlite3.connect(answers["store"]) as db:
        sessions = [row[0] for row in db.execute(
            "SELECT leadSession FROM scouts ORDER BY id")]
with open(here / "calls.jsonl", "a") as log:
    log.write(json.dumps({{"argv": sys.argv, "cwd": os.getcwd(),
                          "turn": turn, "sessions": sessions}}) + "\n")
print(answers.get(turn, ""), flush=True)
if answers.get(turn + "_pid_file"):
    subprocess.Popen(["sh", "-c", "echo $$ > " + answers[turn + "_pid_file"]
                      + "; exec sleep 600"])
time.sleep(answers.get(turn + "_sleep", 0))
'''


class LeadStubs:
    """`answers` maps `probe`, `plan` and `report` to the printed text,
    `TURN_sleep` to seconds slept after it, `TURN_pid_file` to a file a
    child of that turn writes its pid to, and `store` to a store whose
    scouts' lead sessions each call logs."""

    def __init__(self, directory, **answers):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.answer(**answers)
        for name in ("claude", "codex"):
            script = self.directory / name
            script.write_text(STUB.format(python=sys.executable))
            script.chmod(script.stat().st_mode | stat.S_IXUSR)

    def answer(self, **answers):
        answers = {"probe": "ready", **answers}
        (self.directory / "answers.json").write_text(json.dumps(answers))

    def on_path(self):
        os.environ["PATH"] = f"{self.directory}{os.pathsep}{os.environ['PATH']}"

    def calls(self):
        log = self.directory / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def turns(self, turn):
        return [call for call in self.calls() if call["turn"] == turn]


def fenced(document):
    return f"Here it is.\n\n```json\n{json.dumps(document)}\n```\n"

