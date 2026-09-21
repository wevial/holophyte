These files pin actual daemon responses from the seeded store in
`tests/serve_contract_fixture.py` and are also parsed by the console tests.
`normalize_contract` preserves nulls and all keys, replaces host/path labels
with writer and /repo, IDs and PIDs with 1, and absolute timestamps with NOW;
the builder freezes clocks, so durations remain meaningful and unchanged.
The status fixture includes an unestimated run with no recorded host;
`run-detail-unestimated.json` pins the same run with both fields null.
