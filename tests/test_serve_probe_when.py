from tests.serve_fixture import ServeTestCase

PROFILE = ('[agents.implementer_profiles.sonnet]\nharness = "claude"\n'
           'model = "sonnet"\n')


class ProbeWhenTests(ServeTestCase):
    def probe_when(self, config):
        self.seed()
        self.start(config)
        code, _, status = self.request("GET", "/status")
        self.assertEqual(code, 200)
        return {seat: {key: entry[key] for key in ("probe_when", "inherits")
                       if key in entry}
                for seat, entry in status["route_probes"].items()}

    def test_an_unset_trimmer_inherits_and_a_profile_waits_for_first_use(self):
        seats = self.probe_when(PROFILE)
        self.assertEqual(seats["trimmer"],
                         {"probe_when": "inherits", "inherits": "implementer"})
        self.assertEqual(seats["implementer_profiles.sonnet"],
                         {"probe_when": "first_use"})
        self.assertEqual(seats["implementer"], {"probe_when": "start"})

    def test_a_trimmer_with_its_own_route_is_probed_at_start(self):
        seats = self.probe_when('[agents]\ntrimmer = "trim-agent"\n' + PROFILE)
        self.assertEqual(seats["trimmer"], {"probe_when": "start"})
