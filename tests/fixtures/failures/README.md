Failed runs labelled by their cause, for `FAILURE_CAUSE` in
`tests/test_questions_live.py`.

`labelled.jsonl` holds one row per failed run: the ticket, the run id, the
label, and the `state` the question is asked with. All three are `infra`:

- HOLO-164 run 1019 failed on a transient `gh api graphql` error at the merge
  gate.
- LOTUS-76 runs 185 and 186 passed review, then hit a verify timeout that the
  project's own config caused.

The titles, reasons, kinds, classes, attempts and verdicts are the values read
from the writer host's stores on 2026-10-07. HOLO-164's last review findings
were bot threads adjudicated `DECLINE`; the row keeps their shape and verdict,
not their text.
