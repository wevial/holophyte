#!/usr/bin/env python3
"""hollow2: a minimal software factory.

Loop:
  1. Read TODO.md in the target repo, claim the first unchecked task.
  2. Spawn an implementer agent (goal-based) on a branch.
  3. Spawn a read-only reviewer agent on the committed result.
  4. If findings: implementer fixes, one narrow re-review. Max 2 review rounds.
  5. On approval: merge to main, check off the task, repeat.
"""
import re
import subprocess
import sys
from pathlib import Path

TARGET = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/srv/dev/hollow2test")
MAX_ROUNDS = 2
DEFAULT_BUDGET_MIN = 20  # per-task wall-clock cap unless the line says "(N min)"

TASK_RE = re.compile(r"^[-*] \[ \] (.+)$", re.M)
BUDGET_RE = re.compile(r"\((\d+)\s*min\)\s*$")


def parse_task(line):
    """Split 'do thing (acceptance: x) (15 min)' -> text, budget minutes."""
    text = line.strip()
    budget = DEFAULT_BUDGET_MIN
    m = BUDGET_RE.search(text)
    if m:
        budget = int(m.group(1))
        text = BUDGET_RE.sub("", text).strip()
    return text, budget


def sh(cmd, cwd=None):
    r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"`{cmd}` failed:\n{r.stdout}\n{r.stderr}")
    return r.stdout.strip()


def claude(goal, cwd, readonly=False):
    """Run a Claude Code turn with a single goal. Reviewer gets no write perms."""
    cmd = ["claude", "-p", goal, "--dangerously-skip-permissions"]
    if readonly:
        cmd = ["claude", "-p", goal,
               "--disallowedTools", "Edit,Write,NotebookEdit,Bash"]
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=1800)
    return (r.stdout + "\n" + r.stderr).strip()


def claim_task():
    text = (TARGET / "TODO.md").read_text()
    m = TASK_RE.search(text)
    if not m:
        return None, None
    return parse_task(m.group(1))


def complete_task(task_text):
    path = TARGET / "TODO.md"
    path.write_text(path.read_text().replace(f"[ ] {task_text}", f"[x] {task_text}", 1))


def run_task(task, budget_min):
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower())[:30].strip("-")
    branch = f"task/{slug}"
    sh(f"git checkout -b {branch} main", TARGET)

    def timed(goal):
        """Run one agent turn with a hard wall-clock cap; None on timeout."""
        import signal

        def handler(signum, frame):
            raise TimeoutError()

        old = signal.signal(signal.SIGALRM, handler)
        signal.alarm(budget_min * 60)
        try:
            return claude(goal, TARGET)
        except TimeoutError:
            print(f"[hollow2] task exceeded {budget_min} min budget")
            return None
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)

    # 1. implement
    out = timed(f"Implement this task in this repo: {task}\n"
                "Acceptance criteria are part of the task line; the task is done "
                "only when they hold. Commit your work with a clear message. "
                "Stay strictly on-scope; do not expand the task.")
    if out is None or sh("git rev-parse HEAD", TARGET) == sh("git rev-parse main", TARGET):
        print(f"[hollow2] implementer made no commits for: {task}")
        sh(f"git checkout main && git branch -D {branch}", TARGET)
        return False

    sha = sh("git rev-parse HEAD", TARGET)

    # 2. review loop (max 2 rounds)
    for rnd in range(1, MAX_ROUNDS + 1):
        verdict = claude(
            f"You are a READ-ONLY code reviewer. Review commit {sha} (diff vs main) "
            f"in this repo against the task: {task}\n"
            "Do not modify anything. End your reply with exactly one line:\n"
            "VERDICT: APPROVE  or  VERDICT: REQUEST_CHANGES\n"
            "If REQUEST_CHANGES, list only concrete blockers.", TARGET, readonly=True)

        if "VERDICT: APPROVE" in verdict:
            break

        if rnd == MAX_ROUNDS:
            print(f"[hollow2] task failed {MAX_ROUNDS} review rounds; "
                  f"leaving branch {branch} at {sha} for a human. Task: {task}")
            sh("git checkout main", TARGET)
            return False

        # 3. implementer addresses findings (same branch, new commit)
        out = timed(f"A reviewer left findings on your work for task: {task}\n\n"
                    f"{verdict}\n\n"
                    "Fix only the concrete blockers listed (ADDRESS/FOLLOW_UP/DECLINE "
                    "the rest in your commit message). Commit the fixes.")
        if out is None or sh("git rev-parse HEAD", TARGET) == sha:
            print(f"[hollow2] fix round timed out or made no progress; "
                  f"leaving branch {branch} at {sha} for a human.")
            sh("git checkout main", TARGET)
            return False
        sha = sh("git rev-parse HEAD", TARGET)

    # 4. merge + check off
    sh("git checkout main", TARGET)
    sh(f"git merge --no-ff {branch} -m 'Merge {branch}: {task}'", TARGET)
    sh(f"git branch -d {branch}", TARGET)
    complete_task(task)
    print(f"[hollow2] merged: {task}")
    return True


def main():
    while True:
        task, budget_min = claim_task()
        if not task:
            print("[hollow2] TODO.md has no open tasks. done.")
            return
        print(f"[hollow2] claimed: {task} (budget {budget_min} min)")
        if not run_task(task, budget_min):
            return  # stop on first failure; human decides next


if __name__ == "__main__":
    main()
