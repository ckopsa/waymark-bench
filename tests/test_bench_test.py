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
        self.assertFalse(answer["dirty_included"])
        self.assertEqual(self.slept, [])
        result = self.ok("test_result", run_id=31)
        self.assertEqual(result["conclusion"], "success")
        self.assertFalse(result["dirty_included"])
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

    def test_a_dirty_worktree_is_tested_on_a_scratch_commit_and_the_branch_stays(self):
        head = self.make_test()
        path = self.bench.worktree(self.bench.repo("demo"), "work")
        with open(os.path.join(path, "dirty.txt"), "w") as handle:
            handle.write("an edit not yet committed\n")
        self.serve(head, "completed", "success")
        answer = self.ok("test", branch="work", select="waymark.core-test")
        self.assertTrue(answer["dirty_included"])
        self.assertEqual(answer["paths"], ["dirty.txt"])
        self.assertNotEqual(answer["head"], head)
        self.assertEqual(self.scratch().split()[0], answer["head"])
        self.assertEqual(util.git(["show", answer["head"] + ":dirty.txt"], cwd=path),
                         "an edit not yet committed\n")
        self.assertEqual(util.git(["rev-parse", "HEAD"], cwd=path).strip(), head)
        self.assertIn("dirty.txt", util.git(["status", "--porcelain"], cwd=path))
        self.serve(answer["head"], "completed", "success")
        self.assertTrue(self.ok("test_result", run_id=31)["dirty_included"])

    def test_a_run_still_going_answers_pending_and_a_second_call_answers_the_result(self):
        head = self.make_test()
        self.serve(head, "in_progress")
        self.ok("test", branch="work", select="waymark.core-test")
        self.calls = []
        answer = self.ok("test_result", run_id=31)
        self.assertEqual(answer["conclusion"], "pending")
        self.assertEqual(answer["run_id"], 31)
        self.assertEqual(self.slept, [5] * 5)
        self.slept = []
        self.ok("test_result", run_id=31, wait_seconds=45)
        self.assertEqual(self.slept, [5] * 5 + [3])
        self.assertNotEqual(self.scratch(), "")
        self.serve(head, "completed", "success")
        again = self.ok("test_result", run_id=31)
        self.assertEqual(again["conclusion"], "success")
        self.assertEqual(self.dispatches(), [])
        self.assertEqual(self.scratch(), "")

    def test_a_pending_run_answers_pending_within_the_cap_of_the_setting(self):
        head = self.make_test()
        self.serve(head, "in_progress")
        self.ok("test", branch="work", select="waymark.core-test")
        os.environ["BENCH_TEST_WAIT"] = "60"
        self.addCleanup(os.environ.pop, "BENCH_TEST_WAIT", None)
        self.slept = []
        answer = self.ok("test_result", run_id=31)
        self.assertEqual(answer["conclusion"], "pending")
        self.assertEqual(sum(self.slept), 28)
        os.environ["BENCH_TEST_WAIT"] = "10"
        self.slept = []
        self.assertEqual(self.ok("test_result", run_id=31)["conclusion"], "pending")
        self.assertEqual(self.slept, [5, 5])

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

    def test_a_select_that_is_not_a_test_namespace_refuses_before_dispatch(self):
        self.make_test()
        for select in ("test-factory", "core-test", "waymark.core", "waymark.core-test/"):
            self.serve(None, "completed", "success")
            answer = self.refused("test", branch="work", select=select)
            self.assertEqual(answer["refused"], "input")
            self.assertEqual(answer["field"], "select")
            self.assertIn("factory10.merge-line-test", answer["reason"])
            self.assertEqual(self.calls, [])
        self.assertEqual(self.scratch(), "")

    def test_a_namespace_or_a_test_id_dispatches(self):
        head = self.make_test()
        for select in ("factory10.merge-line-test", "factory10.merge-line-test/merges-a-line?"):
            self.serve(head, "completed", "success")
            self.ok("test", branch="work", select=select)
            self.assertEqual(self.dispatches(),
                             [{"ref": "bench-test/work", "inputs": {"only": select}}])
            self.ok("test_result", run_id=31)

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


class TestTrainChecks(LandingCase):
    """train_checks dispatches the workflow it is given and names it."""

    setUp = TestTheTestTool.setUp
    sleep = TestTheTestTool.sleep
    fake_http = TestTheTestTool.fake_http
    make_test = TestTheTestTool.make_test

    def train(self, workflow, shows=True):
        """Pushes train/one and serves run 61 of this workflow on it once dispatched."""
        path = util.clone(self.root, self.clone_url, name="train")
        util.push_change(path, "train/one", "docs/t.txt", "train\n")
        head = util.git(["rev-parse", "HEAD"], cwd=path).strip()
        runs = "/actions/workflows/%s/runs" % workflow
        run = {"id": 61, "status": "queued", "head_sha": head, "head_branch": "train/one",
               "html_url": "https://github.com/o/r/actions/runs/61", "name": "checks"}
        self.answers = {"/actions/workflows/%s/dispatches" % workflow: ("POST", {}),
                        runs: ("GET", {"workflow_runs": []})}
        self.after_dispatch = {runs: ("GET", {"workflow_runs": [run]})} if shows else {}
        self.calls = []
        return head

    def dispatched(self):
        return [path for method, path, body in self.calls if method == "POST"]

    def test_a_given_workflow_is_dispatched_and_echoed(self):
        self.make_test()
        head = self.train("checks.yml")
        answer = self.ok("train_checks", branch="train/one", workflow="checks.yml")
        self.assertEqual(self.dispatched(), ["/actions/workflows/checks.yml/dispatches"])
        self.assertEqual(answer["workflow"], "checks.yml")
        self.assertEqual(answer["run_id"], 61)
        self.assertEqual(answer["head"], head)

    def test_without_a_workflow_the_default_is_dispatched_and_named(self):
        self.make_test()
        self.train("tests.yml")
        answer = self.ok("train_checks", branch="train/one")
        self.assertEqual(self.dispatched(), ["/actions/workflows/tests.yml/dispatches"])
        self.assertEqual(answer["workflow"], "tests.yml")
        self.assertEqual(answer["run_id"], 61)

    def test_the_workflow_is_named_when_no_run_showed(self):
        self.make_test()
        head = self.train("checks.yml", shows=False)
        answer = self.ok("train_checks", branch="train/one", workflow="checks.yml")
        self.assertIsNone(answer["run_id"])
        self.assertEqual(answer["workflow"], "checks.yml")
        self.assertEqual(answer["head"], head)


class TestTrain(LandingCase):
    """The merge train: build, land and delete a train/* branch."""

    def setUp(self):
        LandingCase.setUp(self)
        self.make({"stages": [], "pull_request": PULL_REQUEST})
        # pull requests 1 and 2 change the same line: 2 conflicts once 1 is in
        for number, word in ((1, "delta"), (2, "echo")):
            path = util.clone(self.root, self.clone_url, name="pr%d" % number)
            util.push_change(path, "pr%d" % number, "docs/a.txt", "alpha\n%s\ncharlie\n" % word)
            sha = util.git(["rev-parse", "HEAD"], cwd=path).strip()
            self.answers["/pulls/%d" % number] = ("GET", {
                "number": number, "state": "open", "base": {"ref": "main"},
                "head": {"ref": "pr%d" % number, "sha": sha}})

    def base_head(self):
        return util.git(["ls-remote", self.clone_url, "refs/heads/main"], cwd=self.root).split()[0]

    def build(self, prs):
        return self.ok("train_build", base="main", branch="train/one", prs=prs)

    def test_build_skips_a_conflicting_pull_request(self):
        base = self.base_head()
        answer = self.build([1, 2])
        self.assertEqual(answer["merged"], [1])
        self.assertEqual(answer["conflicted"], [2])
        self.assertEqual(answer["base_head"], base)
        self.assertEqual(self.remote_head("train/one"), answer["head"])
        parents = util.git(["log", "-1", "--format=%P", answer["head"]],
                           cwd=self.bench.bare_dir("demo")).split()
        self.assertEqual(parents[0], base)
        self.assertEqual(self.base_head(), base)

    def test_land_fast_forwards_the_base(self):
        base = self.base_head()
        head = self.build([1])["head"]
        answer = self.ok("train_land", base="main", branch="train/one",
                         expect_base_head=base, head=head)
        self.assertTrue(answer["landed"])
        self.assertEqual(self.base_head(), head)

    def test_land_refuses_when_the_base_moved(self):
        base = self.base_head()
        head = self.build([1])["head"]
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "main", "docs/b.txt", "the base moved\n")
        moved = self.base_head()
        answer = self.refused("train_land", base="main", branch="train/one",
                              expect_base_head=base, head=head)
        self.assertEqual(answer["refused"], "base_moved")
        self.assertEqual(self.base_head(), moved)

    def test_delete_refuses_a_branch_that_is_not_a_train(self):
        answer = self.refused("train_delete", branch="main")
        self.assertEqual(answer["refused"], "not_train")
        self.assertTrue(self.base_head())

    def test_delete_removes_a_train_branch(self):
        self.build([1])
        self.ok("train_delete", branch="train/one")
        self.assertEqual(util.git(["ls-remote", self.clone_url, "refs/heads/train/one"],
                                  cwd=self.root), "")
