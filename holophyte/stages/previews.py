"""Each note target's prompt as the next run would send it, stand-ins for run parts."""
import re

from holophyte.babysit.thread_answers import ask_text
from holophyte.config.agent_settings import review_mode
from holophyte.loop.implement import _commands_brief, implement_goal
from holophyte.loop.review_round import review_text
from holophyte.loop.trim_brief import trim_goal
from holophyte.pr.pullrequest import pr_text_goal
from holophyte.review.briefs import follow_up_place
from holophyte.review.freshness import critic_text
from holophyte.stages import notes

SLOT = "\0notes\0"
STANDS_FOR = {
    "merge conflicts": "the files a merge of main into a resumed branch left"
                       " in conflict; empty on a fresh branch",
    "requeue context": "the maintainer's context for a requeued ticket;"
                       " empty otherwise",
    "steering": "the maintainer's steering notes waiting for this turn;"
                " empty when none wait",
    "candidate commit": "the commit under review",
    "base ref": "the run's frozen base ref",
    "candidate ref": "the run's candidate ref",
    "base commit": "the commit the run's diff starts from",
    "ticket id": "the ticket's key",
    "title": "the ticket's title",
    "ticket": "the ticket's text as the run claimed it",
    "criteria": "the ticket's numbered acceptance criteria, with the rules"
                " for accounting for each",
    "verify commands": "the ticket's verify commands",
    "verify output": "whether the ticket's verify commands and the project's"
                     " baseline passed, and their output",
    "witness pre-check": "the gaps the factory's witness pre-check found",
    "stale approvals": "earlier approvals of this commit that no longer"
                       " stand; empty unless one was cited",
    "earlier rounds": "the earlier review rounds' findings and the fix"
                      " commits that answer them; empty in round 1",
    "test rules": "the Tests section of the worktree's AGENTS.md",
    "scope": "the changed files the ticket does not name",
    "trim commits": "the trim turn's commits in the reviewed range",
    "evidence": "the candidate's UI capture and the approved design",
    "orchestration": "the subagent brief for the orchestration the ticket or"
                     " project asks for",
    "evidence capture": "how to capture the ticket's evidence states",
    "design": "the approved design downloaded into the worktree",
    "tests": "the test and non-test line counts of the run's diff",
    "protected files": "the test files the criteria name or the run added",
    "merged since": "the runs merged since the ticket was filed, with their"
                    " changed files",
    "branch": "the run's branch",
    "pull request template": "the repository's pull request template",
    "convention file": "a convention file the repository holds",
    "conventions": "that convention file's text",
    "main ref": "the main ref the diff is taken against",
    "diff": "the run's diff against main",
    "pull request": "the pull request's URL",
    "asker": "the GitHub user who asked",
    "depends on": "the tickets this ticket depends on",
    "followed by": "the tickets that follow this ticket",
    "thread": "the review thread: where it sits, its author and its comments",
    "question": "the question asked",
}
STAND_IN = re.compile("⟨([^⟩]+)⟩")


def stand_in(name):
    return f"⟨{name}⟩"


def implement(project, slot):
    s = stand_in
    return implement_goal(
        s("merge conflicts"), s("requeue context"), s("steering"), slot,
        s("ticket"), _commands_brief(project, s("verify commands")),
        s("orchestration"), s("evidence capture"), s("design"), s("criteria"))


def review(project, slot):
    s = stand_in
    return review_text(
        project, s("candidate commit"), (s("base ref"), s("candidate ref")),
        s("ticket"), review_mode(project), follow_up_place([s("criteria")]),
        slot, verify=s("verify output"), criteria=s("criteria"),
        witness=s("witness pre-check"), stale=s("stale approvals"),
        earlier=s("earlier rounds"), tests=s("test rules"), scope=s("scope"),
        trim=s("trim commits"), evidence=s("evidence"))


def trim(project, slot):
    s = stand_in
    return trim_goal(s("base commit"),
                     _commands_brief(project, s("verify commands")).strip(),
                     s("tests"), slot, s("protected files"))


def critic(project, slot):
    s = stand_in
    return critic_text({"id": s("ticket id"), "title": s("title"),
                        "body": s("ticket")}, s("merged since"), slot)


def pr_writer(project, slot):
    s = stand_in
    return pr_text_goal(
        project, s("branch"), s("ticket id"), s("title"),
        s("pull request template"), [(s("convention file"), s("conventions"))],
        slot, s("ticket"), s("main ref"), s("diff"))


def babysit_reply(project, slot):
    s = stand_in
    return ask_text(s("pull request"), s("asker"), slot, s("ticket"),
                    s("depends on"), s("followed by"), s("thread"),
                    s("question"))


RENDERS = {"implement": implement, "review": review, "trim": trim,
           "critic": critic, "pr_writer": pr_writer,
           "babysit_reply": babysit_reply}


def preview(project, target):
    builtin, tail = RENDERS[target](project, SLOT).split(SLOT)
    block = notes.block(project, None, None, target)
    held = dict.fromkeys(found.group(1) for found
                         in STAND_IN.finditer(builtin + SLOT + tail))
    return {"target": target, "prompt": builtin + block + tail,
            "builtin": builtin, "notes": block, "tail": tail,
            "placeholders": [{"name": name, "stands_for": STANDS_FOR[name]}
                             for name in held]}
