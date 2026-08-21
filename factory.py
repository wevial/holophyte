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

TASK_RE = re.compile(r"^[-*] \[ \] (.+)$", re.M)


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
    return (m.group(1), m.start(1)) if m else (None, None)


def complete_task(task_text):
    path = TARGET / "TODO.md"
    path.write_text(path.read_text().replace(f"[ ] {task_text}", f"[x] {task_text}", 1))


def run_task(task):
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower())[:30].strip("-")
    branch = f"task/{slug}"
    sh(f"git checkout -b {branch} main", TARGET)

    # 1. implement
    claude(f"Implement this task in this repo: {task}\n"
           "Commit your work with a clear message. Stay strictly on-scope.", TARGET)

    if sh("git rev-parse --verify HEAD", TARGET) == sh("git rev-parse main", TARGET):
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
        claude(f"A reviewer left findings on your work for task: {task}\n\n"
               f"{verdict}\n\n"
               "Fix only the concrete blockers listed (ADDRESS/FOLLOW_UP/DECLINE "
               "the rest in your commit message). Commit the fixes.", TARGET)
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
        task, _ = claim_task()
        if not task:
            print("[hollow2] TODO.md has no open tasks. done.")
            return
        print(f"[hollow2] claimed: {task}")
        if not run_task(task):
            return  # stop on first failure; human decides next


if __name__ == "__main__":
    main()
