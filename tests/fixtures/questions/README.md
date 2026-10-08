Output of the `claude` and `codex` question runners.

- `claude_answer.json` and `codex_answer.jsonl` are real captures, unchanged:
  stdout of this repository's `claude_argv` and `codex_argv`, run by the
  operator on the writer host on 2026-10-07 with both CLIs signed in, asking
  `MENTION_INTENT` about the comment "Why does this read the config twice?".
  Claude ran `haiku` at effort `high` and answered `question` at 0.93 through
  `structured_output`; Codex ran `gpt-6-luna` at effort `low`, printed an
  `agent_message` of `question` at 0.99 and a `turn.completed` usage. The CLI
  versions were not recorded with this capture.
- `claude_signed_out.json` and `codex_signed_out.jsonl` are the captured
  stdout of the same runners on a host where neither CLI was signed in
  (`claude` 2.1.286, `codex-cli` 0.160.1, 2026-10-07): Claude's
  `--output-format json` error document, and Codex's `--json` events with its
  `{"type":"error"}` warnings ending in `turn.failed`.
