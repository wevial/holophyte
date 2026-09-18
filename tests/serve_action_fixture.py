"""Unit-action regression shared with the pinned serve-actions suite."""
import subprocess
from unittest.mock import patch

import store
import store.read


class UnitActionCases:
    def test_restart_supervisor_runs_systemctl_against_the_named_instance(self):
        self.seed()
        self.start(self.token_config('actions = true\nname = "writer-a"\n'),
                   host="0.0.0.0")
        with patch.object(subprocess, "run") as run:
            code, _, body = self.request("POST", "/actions/restart-supervisor")
            self.assertEqual(code, 401)
            self.assertEqual(body, {})
            run.assert_not_called()

            run.side_effect = lambda argv, **kw: self.completed(argv)
            code, _, body = self.request("POST", "/actions/restart-supervisor",
                                         self.BEARER)
        self.assertEqual(code, 200)
        self.assertEqual(body["action"], "restart-supervisor")
        self.assertIs(body["ok"], True)
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["systemctl", "--user", "restart",
                                "holophyte-supervise@writer-a"])
        self.assertEqual(run.call_args.kwargs["timeout"], 20)
        self.assertEqual(body["recorded"], self.run)
        # The row lands before the unit is touched: a human
        # `restart_supervisor` intervention on the store's newest run, its
        # ledger copy naming the unit and the route.
        conn = store.read.open_readonly(self.db)
        try:
            rows = conn.execute(
                'SELECT runId, source, "trigger", "action" FROM interventions'
                " WHERE action != 'migrate'"
            ).fetchall()
            entries = store.read.ledger(conn, self.run)
        finally:
            conn.close()
        self.assertEqual(rows, [(self.run, "human", "manual",
                                 "restart_supervisor")])
        self.assertEqual([(e.kind, e.source) for e in entries],
                         [("intervention", "operator")])
        self.assertIn("holophyte-supervise@writer-a", entries[0].text)
        self.assertIn("restart-supervisor", entries[0].text)
