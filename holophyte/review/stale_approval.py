import json

import store
from holophyte.board.projection import ledger
from holophyte.loop.gates import RunFailure
from holophyte.redact import safe_print as print


def stale_rereview(conn, run_id, provider, task_id, sha, rnd, stale):
    cited = "\n".join(finding["message"] for finding in stale)
    summary = (f"round {rnd}: only stale approvals left open at {sha[:12]};"
               " reviewing the same head again")
    print(f"[holo2] {summary}")
    if conn is not None and run_id is not None:
        store.record_event(conn, run_id, "stale_approval_rereview", summary,
                           level="detail", payload=json.dumps(
                               {"sha": sha, "round": rnd,
                                "findings": [f["message"] for f in stale]}))
    ledger(conn, run_id, task_id, "round",
           f"Round {rnd}: the approvals it cited went stale; no fix round,"
           f" the review runs again at {sha}\n{cited}", provider)


def stale_again(branch, sha, stale):
    if stale:
        first = stale[0]["message"].splitlines()[0]
        raise RunFailure(
            f"the review again at {sha[:12]} still cites a stale approval:"
            f" {first}; branch {branch} preserved at {sha[:12]}")
