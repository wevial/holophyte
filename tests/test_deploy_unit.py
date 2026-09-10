"""`deploy/holophyte-serve@.service`: the standing `--serve` unit template,
with the `--supervise` and one-pass loop templates beside it.

The unit is static configuration, so the checks are structural: it parses
as INI with the three systemd sections, runs `--serve` on the substituted
address and port, reads its environment file per instance, restarts on
failure, and every environment key it substitutes is one that the operating
notes document.

Run: python3 -m unittest discover -s tests -p 'test_deploy*' -v
"""
from __future__ import annotations

import configparser
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNIT = ROOT / "deploy" / "holophyte-serve@.service"
SUPERVISE_UNIT = ROOT / "deploy" / "holophyte-supervise@.service"
LOOP_UNIT = ROOT / "deploy" / "holophyte-loop@.service"
HOSTS = ROOT / "docs" / "operating" / "hosts.md"
OPERATING = ROOT / "docs" / "operating.md"
DEPLOY_README = ROOT / "deploy" / "README.md"

ENV_KEYS = ("HOLOPHYTE_TARGET", "HOLOPHYTE_SERVE_ADDRESS",
            "HOLOPHYTE_SERVE_PORT")


def parse_unit(path=UNIT):
    # systemd units are INI-like; keys may repeat and are case-sensitive.
    parser = configparser.RawConfigParser(strict=False)
    parser.optionxform = str
    parser.read_string(path.read_text())
    return parser


class UnitFileTests(unittest.TestCase):
    def test_parses_with_the_three_systemd_sections(self):
        unit = parse_unit()
        self.assertEqual(sorted(unit.sections()),
                         ["Install", "Service", "Unit"])

    def test_serves_the_substituted_address_and_port(self):
        service = parse_unit()["Service"]
        exec_start = service["ExecStart"]
        self.assertIn("factory.py", exec_start)
        self.assertIn("--serve", exec_start)
        self.assertIn(
            "${HOLOPHYTE_SERVE_ADDRESS}:${HOLOPHYTE_SERVE_PORT}", exec_start)
        self.assertIn("${HOLOPHYTE_TARGET}", exec_start)
        self.assertIn("%i", service["EnvironmentFile"])
        self.assertEqual(service["Restart"], "on-failure")
        self.assertIn("WorkingDirectory", service)

    def test_every_key_it_substitutes_is_documented(self):
        substituted = set(re.findall(r"\$\{(\w+)\}", UNIT.read_text()))
        self.assertEqual(substituted, set(ENV_KEYS))
        section = OPERATING.read_text().split("## Serving standing", 1)
        self.assertEqual(len(section), 2, "no `## Serving standing` section")
        for key in ENV_KEYS:
            self.assertIn(f"`{key}`", section[1])
        self.assertIn("7710", section[1])
        self.assertIn("holophyte-serve@.service", DEPLOY_README.read_text())

    def test_supervise_unit_restarts_the_supervisor_on_failure(self):
        unit = parse_unit(SUPERVISE_UNIT)
        self.assertEqual(sorted(unit.sections()),
                         ["Install", "Service", "Unit"])
        service = unit["Service"]
        exec_start = service["ExecStart"]
        self.assertIn("factory.py ${HOLOPHYTE_TARGET}", exec_start)
        self.assertTrue(exec_start.endswith("--supervise"), exec_start)
        self.assertIn("%i", service["EnvironmentFile"])
        self.assertEqual(service["Restart"], "on-failure")
        self.assertIn("WorkingDirectory", service)

    def test_loop_unit_runs_one_pass_and_does_not_restart(self):
        unit = parse_unit(LOOP_UNIT)
        self.assertEqual(sorted(unit.sections()),
                         ["Install", "Service", "Unit"])
        service = unit["Service"]
        exec_start = service["ExecStart"]
        self.assertIn("factory.py ${HOLOPHYTE_TARGET}", exec_start)
        self.assertNotIn("--", exec_start.split("factory.py", 1)[1])
        self.assertIn("%i", service["EnvironmentFile"])
        self.assertEqual(service["Restart"], "no")
        self.assertEqual(service["Type"], "exec")
        self.assertIn("WorkingDirectory", service)

    def test_the_operating_pages_name_the_supervise_and_loop_units(self):
        readme = DEPLOY_README.read_text()
        self.assertIn(
            "systemctl --user enable --now holophyte-supervise@", readme)
        self.assertIn("systemctl --user start holophyte-loop@", readme)
        hosts = HOSTS.read_text()
        self.assertIn("holophyte-supervise@", hosts)
        self.assertIn("holophyte-loop@", hosts)


if __name__ == "__main__":
    unittest.main()
