# Reviewing

The boundary the local reviewer runs inside. Back to the
[README](../README.md).

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

The first review builds `holophyte-reviewer:ubuntu24.04-v1` automatically from
the digest-pinned Ubuntu image. A run fails closed if preflight identity or
write rejection fails, the Codex tool host cannot execute a local command, the
container times out, or the staged repository fingerprint changes.
