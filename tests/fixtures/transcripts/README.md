These excerpts were captured from a writer-host Codex rollout and a Devin CLI
conversation export on 2026-09-22. Messages, commands, output, identifiers and
paths have been replaced with harmless example values; unrelated metadata and
steps were removed. No credentials or original task content are retained.

Codex uses `response_item` records, including `custom_tool_call` and a result
whose content blocks contain a structured shell result. Devin uses agent steps
with `tool_calls` (`function_name`, `arguments`, `tool_call_id`) and
`observation.results` (`source_call_id`, `content`); exit status is in the
observation text. The `future_type` records are deliberately added unknowns.

`claude.jsonl` copies the record shapes of operator-seat Claude Code 2.1 session
files on 2026-10-16 (keys, nesting and field order kept). Every text, command,
output, path, identifier and timestamp is invented: the session works in
`/workspace/app`, ids are placeholder UUIDs and `msg_`/`toolu_`/`req_` strings,
and the thinking signature is a dummy. In file order: a `queue-operation`
record, a user prompt, a `thinking` block, an assistant text, a Bash `tool_use`
whose `tool_result` has `is_error` true, an `Edit` of `src/example.py` with its
result, a sidechain assistant record, and a `system` `api_error` record with
`retryAttempt` 1 and `maxRetries` 10.
