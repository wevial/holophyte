"""A scout route's `usd_per_mtok` price and the cost each helper and lead
turn records: the CLI's reported cost, else its tokens at the route's price.

Run: python3 -m unittest discover -s tests -p 'test_scout_cost.py' -v
"""
import json
import unittest

from holophyte.config.checks import check_config
from holophyte.config.project import Project
from holophyte.scout.scout_config import scout_config
from holophyte.scout.telemetry import turn_cost
from holophyte.scout.views import scout_detail
from store.scouts import create_scout, move_scout
from tests.host_fixture import git
from tests.scout_stubs import fenced
from tests.test_scout_config import ScoutFixture as ConfigFixture
from tests.test_scout_process import ScoutFixture as ProcessFixture

LUNA = ('[scout.helpers.luna]\nharness = "codex"\nmodel = "gpt-6-luna"\n'
        'effort = "low"\n')
HAIKU = ('[scout.helpers.haiku]\nharness = "claude"\nmodel = "haiku"\n'
         'effort = "low"\n')
SOL = ('[scout]\nlead = "sol"\n[scout.leads.sol]\nharness = "codex"\n'
       'model = "gpt-6.1-sol"\neffort = "high"\nusd_per_mtok = 2.0\n')
QUESTION = "Where is the retry loop?"
PLAN = fenced({"tasks": [{"question": QUESTION, "paths": ["notes.txt"],
                          "mode": "read"}]})
REPORT = fenced({"summary": "the retry loop never backs off", "findings": [],
                 "actions": []})


def priced(table, price):
    return table + f"usd_per_mtok = {price}\n"


class RoutePriceTests(ConfigFixture):
    def test_a_priced_helper_reads_its_price_and_keeps_its_argv(self):
        config = scout_config(self.project(priced(LUNA, 0.5) + HAIKU))
        self.assertEqual(config.prices["luna"], 0.5)
        self.assertIsNone(config.prices["haiku"])
        unpriced = scout_config(self.project(LUNA + HAIKU))
        self.assertEqual(config.helpers["luna"].turn("GOAL"),
                         unpriced.helpers["luna"].turn("GOAL"))

    def test_check_config_refuses_a_price_that_is_no_number_of_at_least_0(self):
        opus = ('[scout.leads.opus]\nharness = "claude"\nmodel = "opus"\n'
                'effort = "high"\n')
        for price in ("-1", '"cheap"', "true"):
            with self.subTest(price=price):
                with self.assertRaises(SystemExit) as refused:
                    check_config(self.project(priced(opus, price)))
                self.assertIn("[scout.leads.opus]", str(refused.exception))
                self.assertIn("usd_per_mtok", str(refused.exception))


class TurnCostTests(unittest.TestCase):
    def test_tokens_at_the_price_when_the_cli_reports_no_cost(self):
        self.assertEqual(turn_cost(0.5, 41000, None), 0.0205)

    def test_a_reported_cost_wins_whatever_the_price(self):
        self.assertEqual(turn_cost(0.5, 41000, 0.12), 0.12)
        self.assertEqual(turn_cost(None, 41000, 0.12), 0.12)

    def test_no_cost_without_a_price_or_without_tokens(self):
        self.assertIsNone(turn_cost(None, 41000, None))
        self.assertIsNone(turn_cost(0.5, None, None))


class ScoutTurnCostTests(ProcessFixture):
    def research_scout(self, config, helper_route=None, name="alpha"):
        path = self.project(name, config)
        conn = self.conn(path)
        (project,) = conn.execute("SELECT id FROM projects").fetchone()
        scout_id = create_scout(conn, project, "why does the retry spin?",
                                "research", 30, helper_route=helper_route)
        move_scout(conn, scout_id, "planning",
                   base_sha=git(path, "rev-parse", "main"))
        code, printed = self.cli("--scout", str(scout_id), str(path))
        self.assertEqual(code, 0, printed)
        return path, conn, scout_id

    def task(self, conn, scout_id):
        (row,) = conn.execute("SELECT tokens, costUsd FROM scoutTasks"
                              " WHERE scoutId = ?", (scout_id,)).fetchall()
        return row

    def plan_usage(self, conn, scout_id):
        (payload,) = conn.execute(
            "SELECT payload FROM runEvents WHERE scoutId = ?"
            " AND kind = 'agent_turn' ORDER BY seq LIMIT 1",
            (scout_id,)).fetchone()
        payload = json.loads(payload)
        self.assertEqual(payload["role"], "scout-lead")
        return payload["usage"]

    def test_a_priced_codex_helper_records_and_serves_its_tokens_cost(self):
        self.stubs.answer(plan=PLAN, report=REPORT, helper_tokens="41,000")
        path, conn, scout_id = self.research_scout(priced(LUNA, 0.5))
        self.assertEqual(self.task(conn, scout_id), (41000, 0.0205))
        code, body = scout_detail(Project.locate(path, adopt=False),
                                  str(scout_id))
        self.assertEqual(code, 200, body)
        self.assertEqual([task["cost_usd"] for task in body["tasks"]],
                         [0.0205])

    def test_an_unpriced_codex_helper_records_its_tokens_and_no_cost(self):
        self.stubs.answer(plan=PLAN, report=REPORT, helper_tokens="41,000")
        _, conn, scout_id = self.research_scout(LUNA)
        self.assertEqual(self.task(conn, scout_id), (41000, None))

    def test_a_claude_helper_records_its_reported_cost_over_its_price(self):
        self.stubs.answer(plan=PLAN, report=REPORT, claude_helper=json.dumps(
            {"type": "result", "result": "At notes.txt:1.",
             "usage": {"input_tokens": 7, "output_tokens": 5},
             "total_cost_usd": 0.12}))
        _, conn, scout_id = self.research_scout(LUNA + priced(HAIKU, 9.0),
                                                helper_route="haiku")
        self.assertEqual(self.task(conn, scout_id), (12, 0.12))

    def test_a_lead_plan_turn_records_its_priced_or_reported_cost(self):
        self.stubs.answer(codex_plan=fenced({"tasks": []}), report=REPORT,
                          plan_tokens="12,000")
        _, conn, scout_id = self.research_scout(SOL)
        self.assertEqual(self.plan_usage(conn, scout_id)["cost_usd"], 0.024)
        self.stubs.answer(claude_plan=json.dumps(
            {"type": "result", "result": fenced({"tasks": []}),
             "usage": {"input_tokens": 9, "output_tokens": 3},
             "total_cost_usd": 0.3}), report=REPORT)
        _, conn, scout_id = self.research_scout("", name="beta")
        self.assertEqual(self.plan_usage(conn, scout_id)["cost_usd"], 0.3)


if __name__ == "__main__":
    unittest.main()
