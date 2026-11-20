"""Each note target's prompt as the next run would send it, stand-ins for run parts."""
import re

from holophyte.agents.harness import ORCHESTRATION_BRIEFS
from holophyte.babysit.maintainer_notes import (
    HINT_PREFIX,
    PREFIX,
    amended_text,
    note_text,
    requeue_text,
    unresolved_text,
)
from holophyte.babysit.thread_answers import ask_text
from holophyte.config.agent_settings import review_mode
from holophyte.config.config_tables import merge_config
from holophyte.config.pipeline_settings import pipeline_notes
from holophyte.config.review_settings import review_config
from holophyte.loop.claim import conflict_brief
from holophyte.loop.contradiction import brief_text
from holophyte.loop.escalation import escalation_text
from holophyte.loop.implement import (
    _commands_brief,
    implement_goal,
    orchestration_mode,
)
from holophyte.loop.review_round import review_text, verify_text
from holophyte.loop.trim_brief import TESTS_BRIEF, protected_brief, trim_goal
from holophyte.pr.mockup import design_text
from holophyte.pr.pr_media import capture_text
from holophyte.pr.pullrequest import pr_text_goal
from holophyte.review.briefs import (
    criteria_text,
    earlier_text,
    follow_up_place,
    named_tests_text,
    scope_text,
    test_rules_text,
    trim_commits_text,
    witness_text,
)
from holophyte.review.freshness import critic_text
from holophyte.review.reproduce import Reproduction
from holophyte.stages import notes
from holophyte.tickets import witness_once

SLOT = "\0notes\0"
STANDS_FOR = {
    "conflicted files": "the files a merge of main into the resumed branch"
                        " left in conflict, comma-separated",
    "previous run": "the failed run the ticket was requeued from",
    "requeuer": "who requeued it: Operator or The factory",
    "intervention event": "the id of the event recording the requeue",
    "requeue note": "the requeue's note",
    "last round": "the number of the previous run's last review round",
    "unresolved findings": "that round's findings, one line each",
    "findings cut": "a line saying the findings were cut to length; empty"
                    " when they were not",
    "escalated run": "the run that failed on an implementer profile before"
                     " the factory escalated the ticket",
    "profile": "that run's implementer profile",
    "failure": "why that run failed",
    "branch head": "the commit at the branch's head, abbreviated",
    "hint id": "the id of a maintainer's hint waiting for this turn",
    "hint author": "who wrote that hint",
    "hint": "that hint's text",
    "amended criteria": "the acceptance criteria as the maintainer's"
                        " amendments leave them, one line each",
    "steer note id": "the id of a maintainer's steering note waiting for"
                     " this turn",
    "steer author": "who wrote that note",
    "steer note": "that note's text, less the criteria edits it makes",
    "reproduction commit": "the commit a reproduce turn made with a failing"
                           " test",
    "failing command": "the verify command that test makes fail",
    "witnessed-once tests": "each criterion witnessed once and its"
                            " uncommitted test file, one line each",
    "candidate commit": "the commit under review",
    "base ref": "the run's frozen base ref",
    "candidate ref": "the run's candidate ref",
    "base commit": "the commit the run's diff starts from",
    "ticket id": "the ticket's key",
    "title": "the ticket's title",
    "ticket": "the ticket's text as the run claimed it",
    "criteria": "the ticket's acceptance criteria, one line each as C1, C2"
                " and so on",
    "criterion id": "the last criterion's id, such as C3",
    "named tests": "the tests defined in the test files the criteria name",
    "verify commands": "the ticket's verify commands",
    "baseline commands": "how many baseline commands ran",
    "verify output": "the output of the ticket's verify commands and the"
                     " project's baseline",
    "witness gaps": "the gaps the factory's witness pre-check found",
    "unnamed criteria": "the ids of the criteria that name no test file",
    "pre-check point": "when the pre-check ran, such as before this review",
    "merge-base baseline": "the named checks the factory ran at the merge"
                           " base; empty when it ran none",
    "stale approvals": "earlier approvals of this commit that no longer"
                       " stand; empty unless one was cited",
    "earlier findings": "the findings of each earlier review round",
    "first candidate": "the commit round 1 reviewed",
    "fix commit messages": "the messages of the commits made since then, as"
                           " JSON",
    "adjudicator rulings": "the adjudicator's rulings on findings it"
                           " settled; empty when none were",
    "test rules": "the Tests section of the worktree's AGENTS.md, quoted",
    "scope": "the changed files the ticket does not name, as a JSON list",
    "trim commits": "the trim turn's commits in the reviewed range",
    "trade-offs": "the Trade-off lines the trim commits record, quoted",
    "evidence": "the candidate's UI capture and the approved design",
    "evidence states": "the ticket's evidence states, one line each",
    "design": "the approved design's images and boards in the worktree",
    "test lines": "the test lines the run's diff adds",
    "code lines": "the non-test lines the run's diff adds",
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
MARKED = re.compile("\x01([^\x01]+)\x01")


def stand_in(name):
    return f"\x01{name}\x01"


def implement(project, slot):
    s = stand_in
    return implement_goal(
        conflict_brief(s("branch"), [s("conflicted files")]),
        requeue_text(s("previous run"), s("requeuer"),
                     f" (intervention event {s('intervention event')})",
                     s("requeue note"),
                     unresolved_text(s("last round"), s("unresolved findings"),
                                     s("findings cut")))
        + escalation_text(s("escalated run"), s("profile"), s("failure"),
                          s("branch"), s("branch head"))
        + note_text(HINT_PREFIX, s("hint id"), s("hint author"), s("hint"))
        + "\n\n"
        + Reproduction(s("reproduction commit"), s("failing command")).opening(),
        (amended_text(s("amended criteria")) + "\n\n"
         + note_text(PREFIX, s("steer note id"), s("steer author"),
                     s("steer note"))).lstrip() + "\n\n", slot,
        s("ticket"), _commands_brief(project, s("verify commands")),
        ORCHESTRATION_BRIEFS[orchestration_mode(project, "")[0]],
        capture_text(merge_config(project), s("ticket id"),
                     s("evidence states"), False),
        design_text(s("design")),
        brief_text(s("criteria"))
        + witness_once.implementer_text(s("witnessed-once tests")))


def review(project, slot):
    s = stand_in
    return review_text(
        project, s("candidate commit"), (s("base ref"), s("candidate ref")),
        s("ticket"), review_mode(project), follow_up_place([s("criteria")]),
        slot, verify=verify_text(s("baseline commands"), True,
                                 s("verify output")),
        criteria=criteria_text(s("criteria"), s("criterion id"),
                               "REQUEST_CHANGES", named_tests_text(
                                   s("named tests")))
        + witness_once.reviewer_text(s("witnessed-once tests")),
        witness=witness_text(s("witness gaps"), s("unnamed criteria"),
                             s("pre-check point"))
        + s("merge-base baseline"),
        stale=s("stale approvals"),
        earlier=earlier_text(s("earlier findings"), s("first candidate"),
                             s("fix commit messages"),
                             follow_up_place([s("criteria")]))
        + s("adjudicator rulings"),
        tests=test_rules_text(s("test rules")), scope=scope_text(s("scope")),
        trim=trim_commits_text(s("trim commits"), s("trade-offs")),
        evidence=s("evidence"), seated=bool(review_config(project).council))


def trim(project, slot):
    s = stand_in
    return trim_goal(s("base commit"),
                     _commands_brief(project, s("verify commands")).strip(),
                     TESTS_BRIEF.format(tests=s("test lines"),
                                        code=s("code lines")), slot,
                     protected_brief(s("protected files")))


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


def preview(project, target, scrub):
    rendered = RENDERS[target](project, SLOT)
    held = dict.fromkeys(MARKED.findall(rendered))
    builtin, tail = (shown(part, scrub) for part in rendered.split(SLOT))
    block = scrub(notes.framed(pipeline_notes(project).get(target, "")))
    return {"target": target, "prompt": builtin + block + tail,
            "builtin": builtin, "notes": block, "tail": tail,
            "placeholders": [{"name": name,
                              "stands_for": scrub(STANDS_FOR[name])}
                             for name in held]}


def shown(text, scrub):
    return "".join(scrub(piece) if at % 2 == 0 else f"⟨{piece}⟩"
                   for at, piece in enumerate(MARKED.split(text)))
