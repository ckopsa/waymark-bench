"""The tests of the test tool: one test selection of a branch, run on the
repository's own CI. The forge is the fake of test_landing; the clock and
the sleep are fakes too, so a wait costs no time."""

import json
import os

from bench import config as config_module, tools
from bench.tools import Bench

from . import util
from .test_landing import LandingCase


WORKFLOW = "/actions/workflows/tests.yml"
PULL_REQUEST = {"provider": "github", "owner": "o", "repo": "r"}
RED_JOBS = [
    {"id": 41, "name": "unit", "status": "completed", "conclusion": "failure",
     "steps": [{"name": "Set up job", "conclusion": "success"},
               {"name": "Run tests", "conclusion": "failure"}]},
    {"id": 42, "name": "lint", "status": "completed", "conclusion": "success", "steps": []}]


class TestTheTestTool(LandingCase):

    def setUp(self):
        LandingCase.setUp(self)
        self.now = [0.0]
        self.slept = []
        self.after_dispatch = {}
        for name, value in (("_clock", lambda: self.now[0]), ("_sleep", self.sleep)):
            self.addCleanup(setattr, tools, name, getattr(tools, name))
            setattr(tools, name, value)

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now[0] += seconds

    def fake_http(self, method, url, headers, body=None):
        answer = LandingCase.fake_http(self, method, url, headers, body)
        if method == "POST" and "/dispatches" in url:
            self.answers.update(self.after_dispatch)
        return answer

    def make_test(self, test={"workflow": "tests.yml", "input": "only"}):
        """Makes the bench with this test block and prepares branch work. Gives its head."""
        data_dir = os.path.join(self.root, "data")
        os.makedirs(data_dir, exist_ok=True)
        self.bench = Bench(config_module.from_dict({"data_dir": data_dir, "repos": {"demo": {
            "clone_url": self.clone_url, "default_branch": "main",
            "land": {"stages": [], "pull_request": PULL_REQUEST}, "test": test}}}))
        path = self.prepared()
        return util.git(["rev-parse", "HEAD"], cwd=path).strip()

    def serve(self, head, status, conclusion=None, jobs=()):
        """Answers run 31 of workflow tests on the scratch ref; it shows once dispatched."""
        run = {"id": 31, "run_number": 4, "status": status, "conclusion": conclusion,
               "head_sha": head, "head_branch": "bench-test/work",
               "html_url": "https://github.com/o/r/actions/runs/31", "name": "tests"}
        self.answers = {
            WORKFLOW + "/dispatches": ("POST", {}),
            WORKFLOW + "/runs": ("GET", {"workflow_runs": []}),
            "/actions/runs/31/jobs": ("GET", {"jobs": list(jobs)}),
            "/actions/jobs/41/logs": ("GET", "Ran 12 tests\nFAIL in (factory-test)\nexpected: 1\n"),
            "/actions/runs/31": ("GET", run),
        }
        self.after_dispatch = {WORKFLOW + "/runs": ("GET", {"workflow_runs": [run]})}
        self.calls = []

    def scratch(self):
        bare = self.bench.bare_dir("demo")
        return util.git(["ls-remote", "origin", "refs/heads/bench-test/work"], cwd=bare).strip()

    def dispatches(self):
        return [body if isinstance(body, dict) else json.loads(body)
                for method, path, body in self.calls if method == "POST"]

    def test_dispatch_sends_the_select_input_on_the_scratch_ref(self):
        head = self.make_test()
        self.serve(head, "completed", "success")
        answer = self.ok("test", branch="work", select="waymark.core-test")
        self.assertEqual(self.dispatches(),
                         [{"ref": "bench-test/work", "inputs": {"only": "waymark.core-test"}}])
        self.assertEqual(answer["run_id"], 31)
        self.assertEqual(answer["conclusion"], "success")
        self.assertEqual(answer["failed"], [])
        self.assertTrue(answer["scratch_deleted"])
        self.assertEqual(self.scratch(), "")

    def test_a_finished_red_run_answers_the_failed_step_with_its_log_tail(self):
        head = self.make_test()
        self.serve(head, "completed", "failure", RED_JOBS)
        answer = self.ok("test", branch="work", select="waymark.core-test")
        self.assertEqual(answer["conclusion"], "failure")
        self.assertEqual([(f["job"], f["step"]) for f in answer["failed"]], [("unit", "Run tests")])
        self.assertIn("FAIL in (factory-test)", answer["failed"][0]["log_tail"])

    def test_a_timeout_answers_running_and_run_id_asks_again(self):
        head = self.make_test()
        self.serve(head, "in_progress")
        answer = self.ok("test", branch="work", select="waymark.core-test", wait=60)
        self.assertEqual(answer["conclusion"], "running")
        self.assertEqual(answer["run_id"], 31)
        self.assertEqual(self.slept, [20, 20, 20])
        self.assertNotEqual(self.scratch(), "")
        self.serve(head, "completed", "success")
        again = self.ok("test", run_id=31)
        self.assertEqual(again["conclusion"], "success")
        self.assertEqual(self.dispatches(), [])
        self.assertEqual(self.scratch(), "")

    def test_a_repository_with_no_test_block_refuses(self):
        self.make_test(test=None)
        self.calls = []
        answer = self.refused("test", branch="work", select="waymark.core-test")
        self.assertEqual(answer["refused"], "no_test_workflow")
        self.assertEqual(self.calls, [])
