"""`MergeModeFixture`'s fake `gh api`, answered inside the test process.

It answers from the same files, in the same order of precedence, as the
`api` branch of the shell `gh` that `fake_route()` writes, so a test reads
the same `recorded()` lines and `api_calls()` bodies through either."""
from __future__ import annotations

import fnmatch
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Answer:
    """What `holophyte.pr.github._gh_run` hands back from `subprocess.run`."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def out(text):
    return Answer(stdout=f"{text}\n")


def refused(text, code=1):
    return Answer(returncode=code, stderr=f"{text}\n")


@dataclass
class FakeGitHub:
    """The `fake_route()` answers: its state directories and its switches."""

    calls: Path
    api_dir: Path
    answers: Path
    queue: Path
    pages: Path
    open_answer: Path
    pr_body: Path
    job_log: Path
    label_log: Path
    target: Path
    git: str
    branch: str
    head: str
    merge_sha: str
    enqueued_at: str
    close_exit: int = 0
    refuse_labels: bool = False
    refuse_reactions: bool = False
    refuse_rerun: bool = False
    merge_queue: bool = False
    plan_gated_rules: bool = False

    def __call__(self, target, argv, body, path):
        args = " ".join(argv[1:])
        with self.calls.open("a") as log:
            log.write(f"gh {args}\n")
        return (self.branch_reads(args) or self.routed(args, body or "")
                or self.posted(args, body or ""))

    def branch_reads(self, args):
        if "check-runs" in args:
            return out('{"check_runs":[]}')
        if self.plan_gated_rules and "branches/main" in args:
            return refused("gh: Upgrade to GitHub Pro or make this repository"
                           " public to enable this feature. (HTTP 403)")
        if "rules/branches/" in args:
            return out('[{"type":"merge_queue","parameters":{}}]'
                       if self.merge_queue else "[]")
        if "/branches/" in args:
            return out("{}")
        return None

    def routed(self, args, body):
        if "--method PATCH repos/example/repo/pulls/" in args:
            return (refused("HTTP 422 refused", self.close_exit)
                    if self.close_exit else out('{"state":"closed"}'))
        if fnmatch.fnmatchcase(args, "*/issues/*/labels*"):
            with self.label_log.open("a") as log:
                log.write(f"{body}\n")
            return refused("label refused") if self.refuse_labels else out("[]")
        if fnmatch.fnmatchcase(args, "* DELETE */issues/comments/*"):
            return Answer()
        if "rerun-failed-jobs" in args:
            return refused("HTTP 403") if self.refuse_rerun else Answer()
        if fnmatch.fnmatchcase(args, "*actions/jobs/*/logs*"):
            if not self.job_log.exists():
                return refused(f"cat: {self.job_log}: No such file or directory")
            return Answer(stdout=self.job_log.read_text())
        if "actions/runs?event=merge_group" in args:
            page = self.queue / f"page-{args.rsplit('&page=', 1)[-1]}"
            return Answer(stdout=page.read_text()) if page.exists() else out("{}")
        if "GET repos/example/repo/pulls/" in args:
            return out(json.dumps(dict(
                title="feat(x): do y (KO-1)", node_id="PR_1",
                body=self.pr_body.read_text() if self.pr_body.exists() else "")))
        return None

    def posted(self, args, body):
        n = len(list(self.api_dir.iterdir())) + 1
        (self.api_dir / f"{n}.json").write_text(body)
        if re.search("/issues/.*/comments", args):
            return out(f'{{"id":{n}}}')
        if "resolveReviewThread" in body:
            return out('{"data":{"resolveReviewThread":{}}}')
        if "addPullRequestReviewThreadReply" in body:
            return out('{"data":{"addPullRequestReviewThreadReply":{}}}')
        if "addReaction" in body:
            return (refused("reaction refused") if self.refuse_reactions
                    else out('{"data":{"addReaction":{}}}'))
        if "markPullRequestReadyForReview" in body:
            return out('{"data":{"markPullRequestReadyForReview":'
                       '{"pullRequest":{"isDraft":false}}}}')
        if "enqueuePullRequest" in body:
            return out('{"data":{"enqueuePullRequest":{"mergeQueueEntry":'
                       f'{{"enqueuedAt":"{self.enqueued_at}"}}}}}}}}')
        if "isInMergeQueue" in body:
            return self.served(self.queue)
        if "mergedBy" in body:
            return out('{"data":{"repository":{"pullRequest":'
                       '{"state":"OPEN","merged":false}}}}')
        if "PullRequestReviewThread" in body:
            return self.next_page()
        if "headRefName" in body:
            return Answer(stdout=self.open_answer.read_text())
        if "reviewThreads" in body:
            return self.served(self.answers)
        return out(f'{{"sha":"{self.merge_sha}","merged":true}}')

    def served(self, directory):
        """The first answer in `directory`, `head` swapped for the branch's
        tip once a line as `sed` swaps it; the last answer stays."""
        files = sorted(directory.glob("*.json"))
        if not files:
            return Answer()
        tip = subprocess.run(
            [self.git, "-C", str(self.target), "rev-parse", self.branch],
            capture_output=True, text=True).stdout.strip()
        text = "".join(line.replace(self.head, tip, 1)
                       for line in files[0].read_text().splitlines(True))
        if len(files) > 1:
            files[0].unlink()
        return Answer(stdout=text)

    def next_page(self):
        files = sorted(self.pages.glob("*.json"))
        if not files:
            return Answer()
        text = files[0].read_text()
        files[0].unlink()
        return Answer(stdout=text)
