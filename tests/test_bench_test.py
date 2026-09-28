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

    def make_enrolled(self, test={"workflow": "tests.yml", "input": "only"}):
        """Makes a bench with no repositories and enrolls demo with this test block."""
        data_dir = os.path.join(self.root, "data")
        os.makedirs(data_dir, exist_ok=True)
        self.bench = Bench(config_module.from_dict({"data_dir": data_dir, "repos": {}}))
        return self.enroll(test)

    def enroll(self, test):
        args = {"clone_url": self.clone_url,
                "land": {"stages": [], "pull_request": PULL_REQUEST}}
        if test is not None:
            args["test"] = test
        return self.ok("enroll", **args)

    def serve(self, head, status, conclusion=None, jobs=()):
        """Answers run 31 of workflow tests on the scratch ref; it shows once dispatched."""
        run = {"id": 31, "run_number": 4, "status": status, "conclusion": conclusion,
               "head_sha": head, "head_branch": "bench-test/work",
               "created_at": "2999-01-01T00:00:00Z", "updated_at": "2999-01-01T00:03:00Z",
               "html_url": "https://github.com/o/r/actions/runs/31", "name": "tests"}
        self.run = run
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

    def test_dispatch_answers_pending_with_the_run_at_once(self):
        head = self.make_test()
        self.serve(head, "completed", "success")
        answer = self.ok("test", branch="work", select="waymark.core-test")
        self.assertEqual(self.dispatches(),
                         [{"ref": "bench-test/work", "inputs": {"only": "waymark.core-test"}}])
        self.assertEqual(answer["run_id"], 31)
        self.assertEqual(answer["run_url"], "https://github.com/o/r/actions/runs/31")
        self.assertEqual(answer["conclusion"], "pending")
        self.assertEqual(self.slept, [])
        result = self.ok("test_result", run_id=31)
        self.assertEqual(result["conclusion"], "success")
        self.assertEqual(result["duration_s"], 180)
        self.assertNotIn("failures", result)
        self.assertTrue(result["scratch_deleted"])
        self.assertEqual(self.scratch(), "")

    def test_a_red_run_answers_the_failing_tests_with_their_lines(self):
        head = self.make_test()
        self.serve(head, "completed", "failure", RED_JOBS)
        self.ok("test", branch="work", select="waymark.core-test")
        answer = self.ok("test_result", run_id=31)
        self.assertEqual(answer["conclusion"], "failure")
        self.assertEqual(answer["failures"], [{"test": "factory-test", "job": "unit",
                                               "lines": ["FAIL in (factory-test)", "expected: 1"]}])

    def test_a_run_still_going_answers_pending_and_a_second_call_answers_the_result(self):
        head = self.make_test()
        self.serve(head, "in_progress")
        self.ok("test", branch="work", select="waymark.core-test")
        self.calls = []
        answer = self.ok("test_result", run_id=31)
        self.assertEqual(answer["conclusion"], "pending")
        self.assertEqual(answer["run_id"], 31)
        self.assertEqual(self.slept, [5] * 8)
        self.slept = []
        self.ok("test_result", run_id=31, wait_seconds=45)
        self.assertEqual(self.slept, [5] * 9)
        self.assertNotEqual(self.scratch(), "")
        self.serve(head, "completed", "success")
        again = self.ok("test_result", run_id=31)
        self.assertEqual(again["conclusion"], "success")
        self.assertEqual(self.dispatches(), [])
        self.assertEqual(self.scratch(), "")

    def test_a_second_test_refuses_while_the_first_run_is_going(self):
        head = self.make_test()
        self.serve(head, "in_progress")
        self.ok("test", branch="work", select="waymark.core-test")
        pushed = self.scratch()
        self.calls = []
        answer = self.refused("test", branch="work", select="waymark.api-test")
        self.assertEqual(answer["refused"], "test_running")
        self.assertEqual(answer["run_id"], 31)
        self.assertEqual(answer["run_url"], "https://github.com/o/r/actions/runs/31")
        self.assertEqual(self.dispatches(), [])
        self.assertEqual(self.scratch(), pushed)
        self.run["status"], self.run["conclusion"] = "completed", "success"
        self.ok("test", branch="work", select="waymark.api-test")
        self.assertEqual(self.dispatches(),
                         [{"ref": "bench-test/work", "inputs": {"only": "waymark.api-test"}}])

    def test_a_going_run_whose_scratch_ref_is_gone_does_not_block(self):
        head = self.make_test()
        self.serve(head, "in_progress")
        self.ok("test", branch="work", select="waymark.core-test")
        util.git(["push", "origin", ":refs/heads/bench-test/work"], cwd=self.bench.bare_dir("demo"))
        self.calls = []
        self.ok("test", branch="work", select="waymark.api-test")
        self.assertEqual(len(self.dispatches()), 1)

    def test_a_run_not_seen_at_dispatch_is_found_by_test_result(self):
        head = self.make_test()
        self.serve(head, "completed", "success")
        self.after_dispatch = {}
        answer = self.ok("test", branch="work", select="waymark.core-test")
        self.assertIsNone(answer["run_id"])
        self.assertEqual(answer["conclusion"], "pending")
        self.assertLessEqual(sum(self.slept), 15)
        self.answers[WORKFLOW + "/runs"] = ("GET", {"workflow_runs": [self.run]})
        again = self.ok("test_result", branch="work", head=answer["head"],
                        dispatched_at=answer["dispatched_at"])
        self.assertEqual(again["run_id"], 31)
        self.assertEqual(again["conclusion"], "success")
        self.assertEqual(len(self.dispatches()), 1)

    def test_a_repository_with_no_test_block_refuses(self):
        self.make_test(test=None)
        self.calls = []
        answer = self.refused("test", branch="work", select="waymark.core-test")
        self.assertEqual(answer["refused"], "no_test_workflow")
        self.assertEqual(self.calls, [])

    def test_an_enrolled_test_block_dispatches_its_workflow(self):
        enrolled = self.make_enrolled()
        self.assertEqual(enrolled["test"], {"workflow": "tests.yml", "input": "only"})
        path = self.prepared()
        head = util.git(["rev-parse", "HEAD"], cwd=path).strip()
        self.serve(head, "completed", "success")
        answer = self.ok("test", branch="work", select="waymark.core-test")
        self.assertEqual(self.dispatches(),
                         [{"ref": "bench-test/work", "inputs": {"only": "waymark.core-test"}}])
        self.assertEqual(answer["conclusion"], "pending")
        self.assertEqual(answer["run_id"], 31)
        with open(os.path.join(self.bench.config.data_dir, "repos.json"), encoding="utf-8") as handle:
            entry = json.load(handle)["repos"]["demo"]
        self.assertEqual(entry["test"], {"workflow": "tests.yml", "input": "only"})

    def test_an_enroll_without_a_test_block_refuses_the_test_tool(self):
        enrolled = self.make_enrolled(test=None)
        self.assertIsNone(enrolled["test"])
        self.prepared()
        self.calls = []
        answer = self.refused("test", branch="work", select="waymark.core-test")
        self.assertEqual(answer["refused"], "no_test_workflow")
        self.assertEqual(self.calls, [])

    def test_a_re_enroll_without_a_test_block_clears_it(self):
        self.make_enrolled()
        again = self.enroll(None)
        self.assertIsNone(again["test"])
        self.assertIsNone(self.bench.config.repo("demo").test)
        self.prepared()
        answer = self.refused("test", branch="work", select="waymark.core-test")
        self.assertEqual(answer["refused"], "no_test_workflow")

    def test_an_enroll_with_a_bad_test_block_refuses_input(self):
        data_dir = os.path.join(self.root, "data")
        os.makedirs(data_dir, exist_ok=True)
        self.bench = Bench(config_module.from_dict({"data_dir": data_dir, "repos": {}}))
        answer = self.refused("enroll", clone_url=self.clone_url, test={"workflow": "tests.yml"})
        self.assertEqual(answer["refused"], "input")
        self.assertEqual(answer["field"], "test")
        self.assertNotIn("demo", self.bench.config.repos)
