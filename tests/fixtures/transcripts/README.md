These excerpts were captured from a writer-host Codex rollout and a Devin CLI
conversation export on 2026-09-22. Messages, commands, output, identifiers and
paths have been replaced with harmless example values; unrelated metadata and
steps were removed. No credentials or original task content are retained.

Codex uses `response_item` records, including `custom_tool_call` and a result
whose content blocks contain a structured shell result. Devin uses agent steps
with `tool_calls` (`function_name`, `arguments`, `tool_call_id`) and
`observation.results` (`source_call_id`, `content`); exit status is in the
observation text. The `future_type` records are deliberately added unknowns.
