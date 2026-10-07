Output of the `claude` (2.1.286) and `codex` (codex-cli 0.160.1) question
runners, captured on 2026-10-07 on a host where neither CLI was signed in.

- `claude_signed_out.json` and `codex_signed_out.jsonl` are the captured
  stdout, unchanged: Claude's `--output-format json` error document and
  Codex's `--json` events ending in `turn.failed`.
- `claude_answer.json` is the captured document with the answer fields
  (`structured_output`, `result`, `usage`, `total_cost_usd`, `modelUsage`)
  filled in from a signed-in operator-seat run of the same day.
- `codex_answer.jsonl` keeps the captured `thread.started`, `turn.started`
  and warning events (`{"type":"error"}` and an `item.completed` error item),
  then ends in an `agent_message` item and `turn.completed` in the shape of a
  signed-in run.

Replace the answer files with fresh signed-in captures when the CLIs change.
