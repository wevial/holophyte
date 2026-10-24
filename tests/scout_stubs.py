"""Stub `claude` and `codex` lead CLIs for scout tests.

Each stub logs its argv, working directory and the scouts' recorded lead
sessions at launch, then answers by the goal's opening line: `ready` to a
probe, and the test's own text to a plan, synthesis, report, repair,
helper or answer turn.
"""
import json
import os
import stat
import sys
from pathlib import Path

STUB = r'''#!{python}
import json, os, re, sqlite3, subprocess, sys, time
from pathlib import Path

here = Path(__file__).resolve().parent
answers = json.loads((here / "answers.json").read_text())
goal, binary = sys.argv[-1], Path(sys.argv[0]).name
turn = next((name for opening, name in (
    ("Scout plan turn", "plan"), ("Scout report turn", "report"),
    ("Scout report repair turn", "repair"),
    ("Scout synthesis turn", "synthesis"), ("Scout helper turn", "helper"),
    ("Scout answer turn", "answer"))
    if goal.startswith(opening)), "probe")
sessions = None
if answers.get("store"):
    with sqlite3.connect(answers["store"]) as db:
        sessions = [row[0] for row in db.execute(
            "SELECT leadSession FROM scouts ORDER BY id")]
call = {{"argv": sys.argv, "cwd": os.getcwd(), "turn": turn,
        "sessions": sessions}}
if turn == "helper":
    call["question"] = re.search(r"^Question: (.*)$", goal, re.M).group(1)
    answers = dict(answers, **answers.get("helpers", {{}}).get(
        call["question"], {{}}))
answers = dict(answers, **answers.get("binaries", {{}}).get(binary, {{}}))
with open(here / "calls.jsonl", "a") as log:
    log.write(json.dumps(call) + "\n")
print(answers.get(binary + "_" + turn, answers.get(turn, "")), flush=True)
if answers.get(turn + "_pid_file"):
    subprocess.Popen(["sh", "-c", "echo $$ > " + answers[turn + "_pid_file"]
                      + "; exec sleep 600"])
done = {{"cwd": os.getcwd(), "question": call.get("question"), "git": []}}
if answers.get(turn + "_shared"):
    mine = Path(answers[turn + "_shared"]) / str(os.getpid())
    mine.touch()
    time.sleep(answers.get(turn + "_sleep", 0) / 2)
    done["present"] = len(os.listdir(mine.parent))
    time.sleep(answers.get(turn + "_sleep", 0) / 2)
    mine.unlink()
else:
    time.sleep(answers.get(turn + "_sleep", 0))
for args in answers.get(turn + "_git", []):
    ran = subprocess.run(["git", *args], capture_output=True, text=True)
    done["git"].append([ran.returncode, ran.stdout.strip()])
for command in answers.get(turn + "_sh", []):
    ran = subprocess.run(command, shell=True, capture_output=True, text=True)
    done.setdefault("sh", []).append([ran.returncode, ran.stdout.strip()])
if answers.get(turn + "_tokens"):
    print("tokens used\n" + answers[turn + "_tokens"], flush=True)
if turn == "helper":
    with open(here / "helpers.jsonl", "a") as log:
        log.write(json.dumps(done) + "\n")
'''


class LeadStubs:
    """`answers` maps `probe`, `plan`, `synthesis`, `report`, `repair`,
    `helper` and `answer` to the printed text, or `BINARY_TURN` to one binary's text;
    `TURN_sleep` to seconds slept after it; `TURN_pid_file` to a file a child of that
    turn writes its pid to; `TURN_shared` to a directory the turn holds a
    file in while it sleeps, logging how many files it saw there;
    `TURN_git` to git argument lists it runs, logging each exit status and
    output; `TURN_sh` to shell commands it runs, logged the same way;
    `TURN_tokens` to a count printed under a `tokens used` line;
    `helpers` maps a helper's question to answers overriding these for it;
    `binaries` maps `claude` or `codex` to answers overriding these for it;
    and `store` to a store whose scouts' lead sessions each call logs."""

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

    def logged(self, name):
        log = self.directory / f"{name}.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def calls(self):
        return self.logged("calls")

    def helpers(self):
        """What each helper turn that ran to its end logged."""
        return self.logged("helpers")

    def turns(self, turn):
        return [call for call in self.calls() if call["turn"] == turn]


def fenced(document):
    return f"Here it is.\n\n```json\n{json.dumps(document)}\n```\n"

