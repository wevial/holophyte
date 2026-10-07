TRIM_BRIEF = """\
Trim the change on this branch. This procedure mirrors the trim skill:
fewer lines, fewer concepts, same behavior, same proof. KISS and DRY, but
never merge two things that only look alike. This is a size and legibility
pass; bugs and big restructurings belong to review.

Scope is this run's own diff, `git diff {base}...HEAD`. Read the
repository's AGENTS.md or CLAUDE.md first; its conventions override this
procedure. Read every file in the diff in full, not just its hunks. Change
no file outside the diff, except an import line where a new duplicate gives
way to an existing utility; never refactor that utility to fit.

The check command is the ticket's verify commands and the project's
baseline, run from the worktree root:
{commands}

If the diff holds under about 50 non-test lines and nothing obvious, reply
"nothing worth trimming" and commit nothing.

Run the passes below in order. After each pass, run the check command; if
it fails, discard that pass's edits and go on to the next pass. Commit each
pass that cut something as one commit whose subject is exactly
`trim: <pass>`, with no tool attribution or co-author lines. Skip a pass
with nothing to cut; make no empty commits. Never amend, rebase, branch or
push, and never rewrite a commit that is not yours.

1. delete. Remove what nothing needs. Imagine removing it: if complexity
   vanishes, delete it; if it reappears at every caller, keep it. Unused
   exports, parameters, fields, imports and options (search the whole
   repository, string lookups, config and registries included); branches
   for impossible states and checks a validated upstream already
   guarantees (keep checks at trust boundaries); flags, shims and aliases
   nothing toggles; wrappers that only forward and catches that only
   rethrow; an interface with one implementation, a factory building one
   thing, an options object with one option; commented-out code.
2. merge. Two functions differing by a value or a branch become one with
   a parameter; the same multi-line preamble at three or more sites becomes
   one helper. Before keeping any helper, look for an existing one in the
   repository's shared modules and the standard library. The same literal
   with the same meaning in two places gets one name. A type that mirrors
   another is derived from it. Near-duplicate test fixtures become one
   factory in the existing test-helper location.
3. flatten. Early returns instead of nesting; collapse boolean ceremony;
   inline one-use variables and helpers unless the name is the only
   explanation of a non-obvious expression; rename only when it lets a
   comment go. No clever compression: if the short form needs a comment,
   the long form was shorter. A change that pushes a function past the
   repository's complexity limit is undone.
4. comments. Delete comments that restate the code, narrate the change
   (tickets, "moved from", anything past-tense about the diff), banners,
   ownerless TODOs, and docstrings on internal functions whose name and
   signature say it all. Keep, in one sentence where possible: why a
   surprising choice was made, invariants the compiler cannot check,
   workaround links, public API docs, legal and security notes. Don't strip
   a file below its neighbours' comment density.
5. tests. One clear test per behavior. Delete a test only with a recorded
   proof that a kept test covers the same behavior: name the kept test that
   hits the same branch with an equivalent assertion, or run a mutation
   check (on a clean tree break the covered line, run the kept tests,
   confirm one fails, restore the file, confirm `git diff` is empty). A
   tautological test goes with no replacement; its proof is the inverse
   mutation check, the test still passing with the behavior broken. Merge
   same-branch, different-data tests into one table. Cut assertions on
   internal calls when a kept test asserts the result, tests of the
   language or a library, "doesn't throw" with nothing else asserted, and
   setup no assertion reads. Keep one test per handled error path, per
   branch of a switched-on enum, and per checked boundary, and keep the
   test that reproduces a reported defect.
   Proportionality: when added test lines pass three times the added
   non-test lines, or about 40 for a change under 10 code lines, every
   remaining test must answer what contract it protects, what credible
   regression fails it, and why a kept test doesn't already catch that; a
   test with no good answer goes. Several tests of one contract at several
   call sites keep one representative site plus any site with its own risk.
   A cross product keeps only the cases that each kill a different
   mutation. A one-line helper is tested through its caller, not alone.
   Such a cut may leave a mutation alive. That is an accepted trade-off,
   never silent, and never on a trust boundary, an auth check, a data-loss
   path or a money path.

Hard rules. Behavior is frozen: outputs, errors, log lines, ordering, side
effects; when unsure, raise it instead of applying it. Coverage is frozen,
except for named trade-offs. Public surface is frozen: exported, CLI, HTTP
and config names used outside the repository or held by a surface or
allow-list test. Never weaken to shorten: no escape-hatch types or casts,
no removed validation, auth checks or error handling. Match the file's
local style; no formatting churn. Raise rather than apply: signature changes
used outside the diff, moving code between modules, a mutation proof that
touches more than one path, a "why" comment you are unsure is dead. Never
touch generated, vendored or migration files.

Commit bodies carry the evidence. In a `trim: tests` commit, write one line
per deleted test, `Proof: <test> -- <the kept test, or the mutation check
you ran>`, and one line per accepted trade-off, `Trade-off: <the surviving
mutation> -- guarded by <the kept test>`.

End your reply with the net line delta, then one line per raised item
saying why it was not applied.
"""


PASSES = ("delete", "merge", "flatten", "comments", "tests")


def pass_name(subject):
    name = subject.removeprefix("trim: ")
    return name if name in PASSES and subject == f"trim: {name}" else None
