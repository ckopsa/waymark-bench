"""The factory kit's conformance cases, run against the rig's two forges.

The cases are bench/kit/forge-conformance.json, vendored from the kit
(bench/kit/SOURCE). Each one is given to the class of its forge as the
forge would answer it, and the rig's judgment must be the one the kit
expects.
"""

import json
import os
import unittest

from bench import forge


with open(os.path.join(forge.KIT, "forge-conformance.json"), encoding="utf-8") as _handle:
    CASES = json.load(_handle)["cases"]


def client_of(case):
    """Gives the client of the case's forge, which answers the case's checks
    from memory: check runs for GitHub, commit statuses for Bitbucket."""
    checks = case["checks"]
    if case["forge"] == "github":
        client = forge.GitHub("o", "r")
        runs = [dict(check, name=check["check_name"]) for check in checks]
        client.check_runs = lambda sha: runs
        client.request = lambda method, url, *args, **kwargs: {"statuses": []}
        return client
    client = forge.Bitbucket("w", "s")
    statuses = [dict(check, key=check["check_name"]) for check in checks]
    client.all_values = lambda path, *args, **kwargs: statuses
    return client


class TestConformance(unittest.TestCase):

    def answer(self, case):
        asks = case["asks"]
        if asks == "behind":
            return forge.behind(case["behind_by"])
        if asks == "state":
            return forge.check_state(case["forge"], case["check"])
        if asks == "verdict":
            states = client_of(case).check_states("abc")
            return forge.head_verdict(states, case["required"], case.get("advisory") or ())
        self.fail("the rig does not know what %r asks" % asks)

    def test_every_case_of_the_kit_passes(self):
        self.assertTrue(CASES)
        for case in CASES:
            with self.subTest(case["name"]):
                self.assertEqual(self.answer(case), case["expect"])

    def test_the_cases_ask_both_forges(self):
        self.assertEqual({case["forge"] for case in CASES if "forge" in case},
                         set(forge.BUILD_STATES["forges"]))

    def test_a_stopped_bitbucket_build_is_not_red_at_the_merge(self):
        client = forge.Bitbucket("w", "s")
        client.request = lambda method, url, *args, **kwargs: {
            "state": "OPEN", "source": {"commit": {"hash": "abc"}}}
        client.all_values = lambda path, *args, **kwargs: [{"key": "build", "state": "STOPPED"}]
        self.assertEqual(client.merge_when_green(7, "abc", ["build"]),
                         {"state": "interrupted", "interrupted": ["build"]})


if __name__ == "__main__":
    unittest.main()
