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
AppArmor policy blocks its nested Bubblewrap sandbox in the Hermes service
context. The outer container is the enforcement boundary: an actual write
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
