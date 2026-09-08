# Reviewing

The boundary the local reviewer runs inside. Back to the
[README](index.md).

## Local reviewer boundary

The factory never gives a reviewer the implementation worktree directly.
`review_runner.py` stages the frozen base and candidate commits into a fresh,
detached, zero-remote Git repository and verifies its identity before and after
the review. Docker mounts that repository at `/workspace` read-only. The
container also has a read-only root filesystem, no Linux capabilities, no
privilege escalation, bounded processes/memory/CPU, and no Docker socket or
host home.

Codex runs with `danger-full-access` **inside** this container because Ubuntu's
AppArmor policy blocks its nested Bubblewrap sandbox when the previous
maintainer's agent runs it as a service. The outer container is the enforcement boundary: an actual write
probe under `/workspace` must fail before the model is called. Only a
disposable copy of `~/.codex/auth.json` and the installed Codex release binaries
are mounted; the copy and all reviewer state are removed afterward. Outbound
network remains enabled because Codex uses remote inference, but no GitHub,
SSH, Linear, Docker, or unrelated host credentials are exposed.

Two directories matter inside the container. `/workspace` is the read-only
staged checkout: the identity check, the write probe and the `PREFLIGHT_OK`
line all run there, and nothing ever writes to it. Once preflight passes the
script copies the whole tree (including `.git`, so `refs/review/base` and
`refs/review/candidate` resolve) to `/home/reviewer/candidate` and runs Codex
from that writable copy, so a package install or a `go build` beside the
sources can succeed and the ticket's criteria can actually be witnessed. The
copy is discarded with the reviewer home at the end of the round; the merge
takes the host worktree's SHA, never the container's files.

How many review rounds a run gets is decided per run, before its first
review, from the size of the candidate's diff and the `[loop]` review keys
(`review_rounds`, `review_rounds_per_lines`, `review_rounds_max`; see
[config.md](config.md)). The cap is printed and recorded in the run's ledger,
and the terminal adjudication follows the last round it allows.

The first review builds `holophyte-reviewer:ubuntu24.04-v4` automatically from
the digest-pinned Ubuntu image; it carries git, python3, ripgrep, a pinned
Bun (checksum-verified, on `PATH` under `/opt/bun/bin`) so console `bun`
criteria can be witnessed inside the container, and a pinned Go 1.26.6
(checksum-verified, under `/usr/local/go`, `GOTOOLCHAIN=local` so no other
toolchain is ever downloaded, caches under the writable `/home/reviewer`) so a
Go target's `go test` criteria can be witnessed too. `/tmp` is a `noexec`
tmpfs so the reviewer cannot run what a candidate drops there; because `go
test` executes its test binaries from the temp directory, the image sets
`TMPDIR` and `GOTMPDIR` to `/home/reviewer/tmp`, which the container script
creates on the writable reviewer home before preflight. A run fails closed if
preflight identity or write rejection fails, the Codex tool host cannot
execute a local command, the container times out, or the staged repository
fingerprint changes.

## PR rounds

Under `[merge] mode = "pr"` (see [Config](config.md)) the reviewer's approval
is not the last word: the candidate is pushed and opened as a pull request,
and the repository's own review bots and people leave threads on it. The
loop answers those the way an operator would by hand -- read, judge, fix,
reply, wait, repeat -- and every pass is a review round of the run, so
FINDINGS shows the GitHub rounds beside the Codex ones.

One pass:

1. **Read.** One GraphQL query returns the PR's unresolved review threads,
   the head commit's check rollup, and whether the PR is merged or closed;
   the threads are paged, and every page is read before anything is
   decided, so a PR is never "quiet" because its open thread was past the
   first page. Each thread is read whole: the opening comment and every
   follow-up, paged to the last one, since a reply can withdraw a finding
   or turn it into a question the opener alone does not show. A PR
   someone merged by hand lands the run as merged with that sha; one
   closed without merging fails the run, branch preserved.
   A head that is not the candidate this run pushed -- someone else pushed
   to the branch -- parks the run naming both shas: the checks and threads
   are about their commit, and the shepherd judges and merges only its own.
2. **Verdict.** The adjudicator route (`[agents] adjudicator`, or the
   default container) is given the ticket, the candidate as the same frozen
   `refs/review/base` and `refs/review/candidate` pair a review round gets,
   and the threads numbered with their whole conversation, and answers one
   line per thread: `THREAD n:
   ADDRESS` (a concrete defect), `DECLINE` (a style preference, a
   duplicate, a request beyond the ticket) or `HUMAN` (a genuine question,
   a rejection of the approach, anything it would not answer on the
   operator's behalf). A thread with no verdict line is `HUMAN`. The pass
   is recorded as a `reviewRounds` row -- route `github:LOGIN`, the
   threads' authors; `github:ci` for a pass that found none -- before
   anything is posted, so an interrupted pass has its row.
3. **Fix.** The addressed threads go to one fix round on the branch (the
   implementer, under the ticket's budget). The fix must be one clean
   commit -- the tree, `HEAD` and the branch all on it -- before the
   ticket's verify commands run over it, so what verified is what is
   pushed; then the branch is pushed. A fix round that commits nothing,
   leaves edits uncommitted, or one the verify commands fail on, fails
   the run with the branch preserved, nothing pushed, nothing posted.
4. **Reply.** Each addressed thread gets a reply opening `---- Comment by
   MODEL ----` (the adjudicator's route, never a constant), then what
   changed and the sha it changed in, and is resolved. Each declined thread
   gets a reply with the reason and is left open for its author to close.
   A `HUMAN` thread gets no reply at all. Every reply and every resolve is
   a `runEvents` row.
5. **Park or go on.** A `HUMAN` verdict ends the pass with the run parked
   and the thread quoted in the ticket's question; a decline parks the run
   with the declined threads listed. Otherwise the next pass reads the PR
   again -- new threads, the checks the fix restarted. A pass with no
   thread waits for pending checks (`pr.CHECK_POLL_S` between reads, at
   most `pr.CHECK_WAIT_S`); red checks park the run, green ones are "ready
   to merge": the PR is merged through its merge API, pinned to the
   candidate's sha so a push that races the pass is refused rather than
   landed, under `[merge] approve = "auto"` or after the operator's
   `--approve`, and parks for the human under `approve = "human"`.
6. **Review the fix.** A fix round moved the candidate past the sha the
   reviewer approved, and the fix is the implementer's work nobody
   independent has judged. So before the merge API is called on a moved
   candidate, the reviewer route reviews it at its fixed sha -- verify
   first, the same read-only brief and frozen `refs/review/*` pair as a
   review round, one more `reviewRounds` row -- and only an `APPROVE`
   that accounts for every acceptance criterion merges; `REQUEST_CHANGES`,
   or an approval that leaves a criterion not met or unwitnessed, parks
   the run with the findings in the ticket's question, with no further
   fix round. The merge API is never called on a sha this process has not
   verified: a resumed run's candidate goes through the merge gate (the
   ticket's verify commands, then the drift check) first, and a failure
   stops the run there as under `mode = "local"`. The operator's
   `--approve` was of the sha it released: a candidate moved since parks
   for the human again under `approve = "human"`. The sha the last
   approval covered survives the park as `runs.approvedSha`, so a
   `--shepherd` re-entry merges the candidate only at that sha and
   reviews it again at any other -- a fix the reviewer rejected has none
   on record, and is reviewed again before anything merges it.

After `[merge] pr_rounds` passes the run parks naming the cap, whatever
the PR looks like.

Every park is the `awaiting_merge_approval` park of `approve = "human"`:
the ticket asks `PR open: URL` with why and the open threads listed, the
run keeps `runs.prUrl`, `runs.candidateSha` and `runs.approvedSha`, branch
and worktree stay, the lease is released. `--approve KO-n` answers "merge": the resumed run
shepherds once more and merges when green and quiet. `--shepherd KO-n`
answers "look again": the same resume, parking again rather than merging
under `approve = "human"`. It is refused on a run parked with no pull
request (parked under `mode = "local"`): there are no threads to look at,
and the local gate merges on release, so that answer is `--approve`'s only.
Merging is GitHub's; local `main` is never moved by the factory.
