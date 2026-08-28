#!/usr/bin/env python3
"""holo2: a minimal software factory.

Loop:
  1. Claim the first ready ticket from Linear (project in linear_provider).
  2. Spawn an implementer agent (goal-based) on a branch.
  3. Spawn a read-only reviewer agent on the committed result.
  4. If findings: implementer fixes, one narrow re-review. Max 2 review rounds.
  5. On approval: merge to main, check off the task, repeat.
"""
import re
import subprocess
import sys
from pathlib import Path

import review_runner

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


def run_verify(cmd, cwd=None):
    """Mechanical acceptance check. Returns (ok, output). Runs via shell on
    purpose: the command is author-supplied on the ticket, not agent output."""
    if not cmd:
        return True, "(no verify command)"
    r = subprocess.run(cmd, shell=True, cwd=str(cwd or TARGET),
                       capture_output=True, text=True, timeout=300)
    return r.returncode == 0, (r.stdout + r.stderr).strip()[-2000:]


def sh(args, cwd=None):
    """Run an argv list — no shell, so task text can't break quoting."""
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"`{args}` failed:\n{r.stdout}\n{r.stderr}")
    return r.stdout.strip()


# Role -> harness/model pins. Each gate uses a distinct, live-probed route:
# Claude Code / Opus High implements; the local container boundary runs Codex /
# GPT-5.6 Sol Medium against a detached, zero-remote, read-only candidate.
IMPL_MODEL = "opus"
IMPL_EFFORT = "high"
REVIEW_PROFILE = "codex-sol-medium"


def agent(role, goal, cwd, *, base_sha=None, candidate_sha=None):
    """Run one agent turn for a role. Returns combined output text."""
    if role == "implement":
        cmd = ["claude", "-p", goal, "--model", IMPL_MODEL,
               "--effort", IMPL_EFFORT]
    elif role == "review":
        if not base_sha or not candidate_sha:
            raise ValueError("review requires exact base_sha and candidate_sha")
        return review_runner.run_review(
            repo=Path(cwd),
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            prompt=goal,
            profile=REVIEW_PROFILE,
            timeout=1800,
        )
    else:
        raise ValueError(role)
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=1800)
    return (r.stdout + "\n" + r.stderr).strip()


def ledger(task_id, entry):
    """Persist a findings record: Linear comment (primary) + FINDINGS.md."""
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        import linear_provider
        linear_provider.comment(task_id, f"**{ts}**\n\n{entry}")
    except Exception as e:
        print(f"[holo2] Linear comment failed ({e}); FINDINGS.md only")
    p = TARGET / "FINDINGS.md"
    with p.open("a") as f:
        f.write(f"\n## {ts} — {task_id}\n{entry}\n")


WORKTREES = TARGET.parent / f"{TARGET.name}.worktrees"


def run_task(task):
    """task: dict from a provider — {id, title, verify, budget_min}.

    Each task works in its own git worktree (TARGET stays on main, untouched),
    so a dirty/failed task can never block the repo or the next ticket.
    """
    task_id = task["id"]
    verify_cmd, budget_min = task.get("verify"), task["budget_min"]
    task = task["title"]
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower())[:30].strip("-")
    branch = f"task/{slug}"
    wt = WORKTREES / slug
    if wt.exists():
        # leftover from a previous failed run — reuse it as-is so preserved
        # work survives; the branch check below still gates on commits.
        sh(["git", "worktree", "prune"], TARGET)
        r = subprocess.run(["git", "worktree", "list", "--porcelain"],
                           cwd=TARGET, capture_output=True, text=True)
        registered = str(wt) in r.stdout
        if not registered:
            sh(["git", "worktree", "add", "--detach", str(wt), "main"], TARGET)
        sh(["git", "checkout", "-B", branch, "main"], cwd=wt)
    else:
        sh(["git", "worktree", "add", "--detach", str(wt), "main"], TARGET)
        sh(["git", "checkout", "-b", branch], cwd=wt)
    base_sha = sh(["git", "rev-parse", "HEAD"], cwd=wt)

    def timed(goal):
        """Run one agent turn with a hard wall-clock cap; None on timeout."""
        import signal

        def handler(signum, frame):
            raise TimeoutError()

        old = signal.signal(signal.SIGALRM, handler)
        signal.alarm(budget_min * 60)
        try:
            return agent("implement", goal, wt)
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
    if out is None or sh(["git", "rev-parse", "HEAD"], cwd=wt) == sh(["git", "rev-parse", "main"], TARGET):
        print(f"[holo2] implementer made no commits for: {task}")
        sh(["git", "worktree", "remove", "--force", str(wt)], TARGET)
        sh(["git", "branch", "-D", branch], TARGET)
        return False

    sha = sh(["git", "rev-parse", "HEAD"], cwd=wt)

    # 2. review loop (max 2 rounds). Verify gate runs before each review:
    # a failing mechanical check skips the reviewer and goes straight to fixes.
    for rnd in range(1, MAX_ROUNDS + 1):
        ok, out = run_verify(verify_cmd, wt)
        if not ok:
            print(f"[holo2] verify FAILED before round {rnd}:\n{out}")
            if rnd == MAX_ROUNDS:
                print(f"[holo2] task failed verify at max rounds; "
                      f"leaving branch {branch} (worktree {wt}) at {sha} for a human.")
                return False
        else:
            print(f"[holo2] verify ok before round {rnd}")

        verdict = agent("review",
            f"You are a READ-ONLY code reviewer. Review commit {sha} using "
            "refs/review/base as the frozen base and refs/review/candidate as "
            "the candidate "
            f"in this repo against the task: {task}\n"
            + (f"A mechanical verification command was run and "
               f"{'PASSED' if ok else 'FAILED with output below'}:\n{out}\n" if verify_cmd else "")
            + "Do not modify anything. End your reply with exactly one line:\n"
            "VERDICT: APPROVE  or  VERDICT: REQUEST_CHANGES\n"
            "If REQUEST_CHANGES, list only concrete blockers.", wt,
            base_sha=base_sha, candidate_sha=sha)

        if ok and review_runner.terminal_verdict(verdict) == "APPROVE":
            break

        if rnd == MAX_ROUNDS:
            print(f"[holo2] task failed {MAX_ROUNDS} review rounds; "
                  f"leaving branch {branch} at {sha} for a human. Task: {task}")
            ledger(task_id, f"FAILED after {MAX_ROUNDS} review rounds; branch {branch} "
                         f"preserved at {sha}\n\nLast reviewer verdict:\n{verdict}")
            return False

        # 3. implementer addresses findings (same branch, new commit)
        out = timed(f"A reviewer left findings on your work for task: {task}\n\n"
                    f"{verdict}\n\n"
                    "For EACH finding, adjudicate it first: ADDRESS (concrete "
                    "blocker — fix now), FOLLOW_UP (valid but out of scope — name "
                    "it in the commit message), or DECLINE (invalid/out-of-scope — "
                    "state the rationale in the commit message). Then fix only the "
                    "ADDRESS items and commit.")
        ledger(task_id, f"Round {rnd}: REQUEST_CHANGES -> fix round\n"
                     f"Reviewer findings:\n{verdict}\n\n"
                     f"Implementer response:\n{out}")
        if out is None or sh(["git", "rev-parse", "HEAD"], cwd=wt) == sha:
            print(f"[holo2] fix round timed out or made no progress; "
                  f"leaving branch {branch} at {sha} for a human.")
            return False
        sha = sh(["git", "rev-parse", "HEAD"], cwd=wt)

    # 4. pre-merge verify (catches fix-round regressions), then merge
    ok, out = run_verify(verify_cmd, wt)
    if not ok:
        print(f"[holo2] verify FAILED before merge; leaving branch {branch} "
              f"at {sha} for a human:\n{out}")
        return False
    print("[holo2] verify ok before merge")

    # Commit any pending FINDINGS.md changes BEFORE merging so the merge
    # never trips over a dirty index.
    r = subprocess.run(["git", "status", "--porcelain", "FINDINGS.md"],
                       cwd=TARGET, capture_output=True, text=True)
    if r.stdout.strip():
        sh(["git", "add", "FINDINGS.md"], TARGET)
        sh(["git", "commit", "-m", f"FINDINGS: {task_id} review records"], TARGET)

    mr = subprocess.run(["git", "merge", "--no-ff", branch, "-m",
                         f"Merge {branch}: {task}"], cwd=TARGET,
                        capture_output=True, text=True)
    if mr.returncode != 0 and "FINDINGS.md" in (mr.stdout + mr.stderr):
        # conflict limited to FINDINGS.md — prefer the branch side (fuller log)
        subprocess.run(["git", "checkout", "--theirs", "FINDINGS.md"],
                       cwd=TARGET, capture_output=True, text=True)
        sh(["git", "add", "FINDINGS.md"], TARGET)
        sh(["git", "commit", "--no-edit"], TARGET)
    else:
        assert mr.returncode == 0, (mr.stdout, mr.stderr)
    sh(["git", "worktree", "remove", str(wt)], TARGET)
    sh(["git", "branch", "-d", branch], TARGET)
    import linear_provider
    linear_provider.complete(task_id)
    ledger(task_id, f"MERGED to main (branch {branch} deleted). "
                 f"Verify: {'passed' if ok else 'n/a'}. Rounds used: {rnd}.")
    sh(["git", "add", "FINDINGS.md"], TARGET)
    sh(["git", "commit", "-m", f"Complete task {task_id}: {task}"], TARGET)
    print(f"[holo2] merged: {task}")
    return True


def main():
    import linear_provider
    while True:
        task = linear_provider.claim_next()
        if not task:
            print("[holo2] Linear has no ready tickets. done.")
            return
        if not run_task(task):
            return  # stop on first failure; ticket stays In Progress for a human


if __name__ == "__main__":
    main()
