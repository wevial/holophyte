#!/usr/bin/env python3
"""holo2: a minimal software factory.

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

TARGET = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/srv/dev/holo2test")
MAX_ROUNDS = 2
DEFAULT_BUDGET_MIN = 20  # per-task wall-clock cap unless the line says "(N min)"

TASK_RE = re.compile(r"^[-*] \[ \] (.+)$", re.M)
BUDGET_RE = re.compile(r"\((\d+)\s*min\)\s*$")
VERIFY_RE = re.compile(r"\(verify:\s*(.+?)\)\s*$")


def parse_task(line):
    """Split 'do thing (verify: cmd) (15 min)' -> text, verify cmd, budget."""
    text = line.strip()
    budget, verify = DEFAULT_BUDGET_MIN, None
    m = BUDGET_RE.search(text)
    if m:
        budget = int(m.group(1))
        text = BUDGET_RE.sub("", text).strip()
    m = VERIFY_RE.search(text)
    if m:
        verify = m.group(1).strip()
        text = VERIFY_RE.sub("", text).strip()
    return text, verify, budget


def run_verify(cmd):
    """Mechanical acceptance check. Returns (ok, output). Runs via shell on
    purpose: the command is author-supplied in TODO.md, not agent output."""
    if not cmd:
        return True, "(no verify command)"
    r = subprocess.run(cmd, shell=True, cwd=TARGET, capture_output=True,
                       text=True, timeout=300)
    return r.returncode == 0, (r.stdout + r.stderr).strip()[-2000:]


def sh(args, cwd=None):
    """Run an argv list — no shell, so task text can't break quoting."""
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"`{args}` failed:\n{r.stdout}\n{r.stderr}")
    return r.stdout.strip()


# Role -> harness/model pins. One role, one harness: variables stay pinned
# per gate (implementer=opencode/ox-alpha, reviewer=claude/opus med,
# checker=claude/terra med for the future supervisor loop).
IMPL_MODEL = "openrouter/stealth/ox-alpha"   # opencode
REVIEW_MODEL = "opus"                        # claude, effort medium
CHECK_MODEL = "openrouter/openai/gpt-5.6-terra"  # opencode, variant medium


def agent(role, goal, cwd):
    """Run one agent turn for a role. Returns combined output text."""
    if role == "implement":
        # opencode pins its project root at startup and ignores subprocess cwd;
        # --dir is required to target the repo.
        cmd = ["opencode", "run", "--dir", str(cwd), "-m", IMPL_MODEL, goal]
    elif role == "review":
        cmd = ["claude", "-p", goal, "--model", REVIEW_MODEL,
               "--effort", "medium",
               "--disallowedTools", "Edit,Write,NotebookEdit,Bash"]
    elif role == "check":
        cmd = ["opencode", "run", "-m", CHECK_MODEL, "--variant", "medium", goal]
    else:
        raise ValueError(role)
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=1800)
    return (r.stdout + "\n" + r.stderr).strip()


def claim_task():
    text = (TARGET / "TODO.md").read_text()
    m = TASK_RE.search(text)
    if not m:
        return None, None, None
    return parse_task(m.group(1))


def complete_task(task_text):
    path = TARGET / "TODO.md"
    path.write_text(path.read_text().replace(f"[ ] {task_text}", f"[x] {task_text}", 1))


def ledger(task, entry):
    """Append a timestamped record to FINDINGS.md in the target repo."""
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    p = TARGET / "FINDINGS.md"
    with p.open("a") as f:
        f.write(f"\n## {ts} — {task[:100]}\n{entry}\n")


def run_task(task, verify_cmd, budget_min):
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower())[:30].strip("-")
    branch = f"task/{slug}"
    sh(["git", "checkout", "-b", branch, "main"], TARGET)

    def timed(goal):
        """Run one agent turn with a hard wall-clock cap; None on timeout."""
        import signal

        def handler(signum, frame):
            raise TimeoutError()

        old = signal.signal(signal.SIGALRM, handler)
        signal.alarm(budget_min * 60)
        try:
            return agent("implement", goal, TARGET)
        except TimeoutError:
            print(f"[holo2] task exceeded {budget_min} min budget")
            return None
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)

    # 1. implement
    out = timed(f"Implement this task in this repo: {task}\n"
                "Acceptance criteria are part of the task line; the task is done "
                "only when they hold. Commit your work with a clear message. "
                "Stay strictly on-scope; do not expand the task.")
    if out is None or sh(["git", "rev-parse", "HEAD"], TARGET) == sh(["git", "rev-parse", "main"], TARGET):
        print(f"[holo2] implementer made no commits for: {task}")
        sh(["git", "checkout", "main"], TARGET); sh(["git", "branch", "-D", branch], TARGET)
        return False

    sha = sh(["git", "rev-parse", "HEAD"], TARGET)

    # 2. review loop (max 2 rounds). Verify gate runs before each review:
    # a failing mechanical check skips the reviewer and goes straight to fixes.
    for rnd in range(1, MAX_ROUNDS + 1):
        ok, out = run_verify(verify_cmd)
        if not ok:
            print(f"[holo2] verify FAILED before round {rnd}:\n{out}")
            if rnd == MAX_ROUNDS:
                print(f"[holo2] task failed verify at max rounds; "
                      f"leaving branch {branch} at {sha} for a human.")
                sh(["git", "checkout", "main"], TARGET)
                return False
        else:
            print(f"[holo2] verify ok before round {rnd}")

        verdict = agent("review",
            f"You are a READ-ONLY code reviewer. Review commit {sha} (diff vs main) "
            f"in this repo against the task: {task}\n"
            + (f"A mechanical verification command was run and "
               f"{'PASSED' if ok else 'FAILED with output below'}:\n{out}\n" if verify_cmd else "")
            + "Do not modify anything. End your reply with exactly one line:\n"
            "VERDICT: APPROVE  or  VERDICT: REQUEST_CHANGES\n"
            "If REQUEST_CHANGES, list only concrete blockers.", TARGET)

        if ok and "VERDICT: APPROVE" in verdict:
            break

        if rnd == MAX_ROUNDS:
            print(f"[holo2] task failed {MAX_ROUNDS} review rounds; "
                  f"leaving branch {branch} at {sha} for a human. Task: {task}")
            ledger(task, f"FAILED after {MAX_ROUNDS} review rounds; branch {branch} "
                         f"preserved at {sha}\n\nLast reviewer verdict:\n{verdict}")
            sh(["git", "checkout", "main"], TARGET)
            return False

        # 3. implementer addresses findings (same branch, new commit)
        out = timed(f"A reviewer left findings on your work for task: {task}\n\n"
                    f"{verdict}\n\n"
                    "For EACH finding, adjudicate it first: ADDRESS (concrete "
                    "blocker — fix now), FOLLOW_UP (valid but out of scope — name "
                    "it in the commit message), or DECLINE (invalid/out-of-scope — "
                    "state the rationale in the commit message). Then fix only the "
                    "ADDRESS items and commit.")
        ledger(task, f"Round {rnd}: REQUEST_CHANGES -> fix round\n"
                     f"Reviewer findings:\n{verdict}\n\n"
                     f"Implementer response:\n{out}")
        if out is None or sh(["git", "rev-parse", "HEAD"], TARGET) == sha:
            print(f"[holo2] fix round timed out or made no progress; "
                  f"leaving branch {branch} at {sha} for a human.")
            sh(["git", "checkout", "main"], TARGET)
            return False
        sha = sh(["git", "rev-parse", "HEAD"], TARGET)

    # 4. pre-merge verify (catches fix-round regressions), then merge
    ok, out = run_verify(verify_cmd)
    if not ok:
        print(f"[holo2] verify FAILED before merge; leaving branch {branch} "
              f"at {sha} for a human:\n{out}")
        sh(["git", "checkout", "main"], TARGET)
        return False
    print("[holo2] verify ok before merge")

    sh(["git", "checkout", "main"], TARGET)
    sh(["git", "merge", "--no-ff", branch, "-m", f"Merge {branch}: {task}"], TARGET)
    sh(["git", "branch", "-d", branch], TARGET)
    complete_task(task)
    ledger(task, f"MERGED to main (branch {branch} deleted). "
                 f"Verify: {'passed' if ok else 'n/a'}. Rounds used: {rnd}.")
    sh(["git", "add", "FINDINGS.md"], TARGET)
    sh(["git", "add", "TODO.md"], TARGET)
    sh(["git", "commit", "-m", f"Complete task: {task}"], TARGET)
    print(f"[holo2] merged: {task}")
    return True


def main():
    while True:
        task, verify_cmd, budget_min = claim_task()
        if not task:
            print("[holo2] TODO.md has no open tasks. done.")
            return
        print(f"[holo2] claimed: {task} (budget {budget_min} min, "
              f"verify: {verify_cmd or 'none'})")
        if not run_task(task, verify_cmd, budget_min):
            return  # stop on first failure; human decides next


if __name__ == "__main__":
    main()
