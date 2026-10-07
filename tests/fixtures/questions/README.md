Output of the `claude` (2.1.286) and `codex` (codex-cli 0.160.1) question
runners, captured on 2026-10-07 on a host where neither CLI was signed in.

- `claude_signed_out.json` and `codex_signed_out.jsonl` are the captured
  stdout, unchanged: Claude's `--output-format json` error document and
  Codex's `--json` events ending in `turn.failed`.
- `claude_answer.json` and `codex_answer.jsonl` are not captures. They keep
  the captured documents' other fields and Codex's captured warning events
  (`{"type":"error"}` and an `item.completed` error item), with the answer
  and usage fields (`structured_output`, `usage`, `total_cost_usd`; an
  `agent_message` item and `turn.completed`) written from the operator seat's
  signed-in recordings of the same day, as the ticket reports them.

Replace both answer files with fresh signed-in captures from the writer host.
