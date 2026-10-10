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
# the unittest shape waymark-bench names: a dotted module, class or test
PYTHON_SELECT = r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$"
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

    def test_a_green_run_answers_the_lines_a_pattern_matches(self):
        head = self.make_test()
        green = [{"id": 41, "name": "unit", "status": "completed", "conclusion": "success",
                  "steps": []}]
        self.serve(head, "completed", "success", green)
        self.ok("test", branch="work", select="waymark.core-test")
        self.assertNotIn("matches", self.ok("test_result", run_id=31))
        # the scratch ref is gone, and the run's log still reads
        self.assertEqual(self.scratch(), "")
        answer = self.ok("test_result", run_id=31, pattern="^Ran|expected")
        self.assertEqual(answer["conclusion"], "success")
        self.assertEqual(answer["matches"], [{"job": "unit", "line": 1, "text": "Ran 12 tests"},
                                             {"job": "unit", "line": 3, "text": "expected: 1"}])
        self.assertEqual(answer["count"], 2)
        self.assertFalse(answer["truncated"])
        one = self.ok("test_result", run_id=31, pattern="^Ran|expected", limit=1)
        self.assertEqual([row["line"] for row in one["matches"]], [1])
        self.assertEqual(one["count"], 2)
        self.assertTrue(one["truncated"])
        refused = self.refused("test_result", run_id=31, pattern="(")
        self.assertEqual(refused["refused"], "input")
        self.assertEqual(refused["field"], "pattern")

    def test_a_finished_run_answers_the_counts_the_runner_printed(self):
        head = self.make_test()
        green = [{"id": 41, "name": "unit", "status": "completed", "conclusion": "success",
                  "steps": []}]
        self.serve(head, "completed", "success", green)
        stamp = "2026-10-07T00:00:01.0000000Z "
        self.answers["/actions/jobs/41/logs"] = ("GET", "".join(stamp + line + "\n" for line in (
            "test_node (tests.test_tools.TestNode.test_node) ... skipped 'node is not installed'",
            "test_other (tests.test_tools.TestNode.test_other) ... ok",
            "Ran 12 tests in 0.512s",
            "",
            "OK (skipped=1, expected failures=2)")))
        self.ok("test", branch="work", select="waymark.core-test")
        answer = self.ok("test_result", run_id=31)
        self.assertEqual(answer["conclusion"], "success")
        self.assertEqual(answer["tests"], {
            "ran": 12, "failures": 0, "errors": 0, "skipped": 1,
            "skips": [{"test": "tests.test_tools.TestNode.test_node",
                       "reason": "node is not installed"}]})

    def test_a_run_with_no_job_log_answers_no_counts(self):
        head = self.make_test()
        self.serve(head, "completed", "success")
        self.ok("test", branch="work", select="waymark.core-test")
        self.assertIsNone(self.ok("test_result", run_id=31)["tests"])

    def test_a_run_with_no_counts_says_why(self):
        head = self.make_test()
        self.serve(head, "completed", "success")
        self.ok("test", branch="work", select="waymark.core-test")
        self.assertEqual(self.ok("test_result", run_id=31)["tests_missing"],
                         "the run has no job, so it has no log")
        green = [{"id": 41, "name": "unit", "status": "completed", "conclusion": "success",
                  "steps": []}]
        self.serve(head, "completed", "success", green)
        self.answers["/actions/jobs/41/logs"] = ("GET", "lein test\nall done\n")
        answer = self.ok("test_result", run_id=31)
        self.assertIsNone(answer["tests"])
        self.assertEqual(answer["tests_missing"], tools.NO_RAN_LINE)

    def test_a_run_with_counts_has_no_tests_missing(self):
        head = self.make_test()
        green = [{"id": 41, "name": "unit", "status": "completed", "conclusion": "success",
                  "steps": []}]
        self.serve(head, "completed", "success", green)
        self.ok("test", branch="work", select="waymark.core-test")
        answer = self.ok("test_result", run_id=31)
        self.assertEqual(answer["tests"]["ran"], 12)
        self.assertNotIn("tests_missing", answer)

    def skipped_beside(self, *jobs):
        """Gives these jobs and one skipped job, factory, that the forge has no log for."""
        return [{"id": job, "name": "unit%d" % job, "status": "completed",
                 "conclusion": "success", "steps": []} for job in jobs] + [
            {"id": 42, "name": "factory", "status": "completed", "conclusion": "skipped",
             "steps": []}]

    def test_a_green_run_whose_tests_ran_says_so_and_names_the_skipped_job(self):
        head = self.make_test()
        self.serve(head, "completed", "success", self.skipped_beside(41))
        self.ok("test", branch="work", select="waymark.core-test")
        answer = self.ok("test_result", run_id=31)
        self.assertEqual(answer["tests"]["ran"], 12)
        self.assertIs(answer["tests_ran"], True)
        self.assertEqual(answer["jobs_skipped"], ["factory"])
        self.assertFalse([call for call in self.calls if "/jobs/42/logs" in str(call)])

    def test_a_green_run_that_ran_no_test_says_so(self):
        head = self.make_test()
        self.serve(head, "completed", "success", self.skipped_beside(41))
        self.answers["/actions/jobs/41/logs"] = ("GET", "lein test\nall done\n")
        self.ok("test", branch="work", select="waymark.core-test")
        answer = self.ok("test_result", run_id=31)
        self.assertEqual(answer["conclusion"], "success")
        self.assertIsNone(answer["tests"])
        self.assertIs(answer["tests_ran"], False)
        self.assertEqual(answer["tests_missing"],
                         tools.NO_RAN_LINE + "; skipped jobs: factory")

    def test_a_run_whose_jobs_were_all_skipped_ran_no_test(self):
        head = self.make_test()
        self.serve(head, "completed", "success", self.skipped_beside())
        self.ok("test", branch="work", select="waymark.core-test")
        answer = self.ok("test_result", run_id=31)
        self.assertIs(answer["tests_ran"], False)
        self.assertEqual(answer["tests_missing"], "every job of the run was skipped, so "
                         "no test ran; skipped jobs: factory")

    def test_a_log_that_was_not_read_leaves_tests_ran_unknown(self):
        self.make_test()

        class Client:
            def steps(self, run_id):
                return [{"id": 1, "name": "unit", "result": "success"},
                        {"id": 2, "name": "idle", "result": "skipped"}]

            def step_log(self, run_id, job_id):
                raise tools.forge.ForgeError("no log: github has no /jobs/%s/logs" % job_id)

        facts = {}
        repo = self.bench.repo("demo")
        self.assertIsNone(tools._run_counts(self.bench, repo, Client(), 33, [], facts))
        self.assertEqual(facts, {"skipped": ["idle"], "unread": 1})

    def test_a_clojure_run_answers_its_assertions_and_an_unread_log_says_so(self):
        self.make_test()
        logs = {1: "Ran 12 tests containing 40 assertions.\n0 failures, 0 errors.\n"}
        asked = []

        class Client:
            def steps(self, run_id):
                return [{"id": 1, "name": "unit"}, {"id": 2, "name": "more"}][:run_id - 30]

            def step_log(self, run_id, job_id):
                asked.append(job_id)
                if run_id != 31:
                    raise tools.forge.NoAnswer("no answer from api.github.com: timed out")
                return logs[job_id]

        repo = self.bench.repo("demo")
        self.assertEqual(tools._run_counts(self.bench, repo, Client(), 31),
                         {"ran": 12, "assertions": 40, "failures": 0, "errors": 0,
                          "skipped": 0, "skips": []})
        missing, asked[:] = [], []
        self.assertIsNone(tools._run_counts(self.bench, repo, Client(), 32, missing))
        # the forge did not answer for the first log, so the second is not asked for
        self.assertEqual(asked, [1])
        self.assertEqual(len(missing), 1)
        self.assertIn("no job log was read", missing[0])
        self.assertIn("no answer from api.github.com: timed out", missing[0])
        self.assertIn("call test_result again", missing[0])

    def test_a_red_run_answers_the_failing_tests_with_their_lines(self):
        head = self.make_test()
        self.serve(head, "completed", "failure", RED_JOBS)
        self.ok("test", branch="work", select="waymark.core-test")
        answer = self.ok("test_result", run_id=31)
        self.assertEqual(answer["conclusion"], "failure")
        self.assertEqual(answer["failures"], [{"test": "factory-test", "job": "unit",
                                               "lines": ["FAIL in (factory-test)", "expected: 1"]}])

    def test_a_failed_lint_step_answers_its_own_lines_not_the_cleanup(self):
        log = ["##[group]Run actions/checkout@v4", "##[endgroup]",
               "##[group]Run actionlint", "actionlint", "shell: /usr/bin/bash -e {0}",
               "##[endgroup]",
               ".github/workflows/image.yml:12:9: shellcheck reported issue SC2086 [shellcheck]",
               "##[error]Process completed with exit code 1.",
               "Post job cleanup.", "[command]/usr/bin/git version",
               "##[warning]Node.js 20 actions are deprecated."]
        self.assertEqual(tools._failing_tests("actionlint", log), [{
            "test": None, "job": "actionlint",
            "lines": [".github/workflows/image.yml:12:9: shellcheck reported issue SC2086 [shellcheck]",
                      "##[error]Process completed with exit code 1."]}])

    def test_an_execution_error_keeps_its_exception_message(self):
        log = ["Compiling waymark10.checks-assembly",
               "Execution error (ExceptionInfo) at waymark10.checks-assembly/check-unref'd-ids (checks_assembly.clj:40).",
               "kind :ticket field :repo names no id",
               "{:kind :ticket, :field :repo}",
               "",
               "Full report at: /tmp/clojure-1.edn"]
        self.assertEqual(tools._failing_tests("unit", log), [{
            "test": None, "job": "unit",
            "lines": ["Execution error (ExceptionInfo) at waymark10.checks-assembly/check-unref'd-ids (checks_assembly.clj:40).",
                      "kind :ticket field :repo names no id",
                      "{:kind :ticket, :field :repo}"]}])

    def test_an_execution_error_message_stops_at_a_stack_frame(self):
        log = ["Execution error (IllegalArgumentException) at waymark10.core/f (core.clj:3).",
               "no such field :repo", "\tat clojure.lang.RT.f(RT.java:1)", "more"]
        self.assertEqual(tools._failing_tests("unit", log)[0]["lines"],
                         ["Execution error (IllegalArgumentException) at waymark10.core/f (core.clj:3).",
                          "no such field :repo"])

    def test_a_drive_s_failed_check_keeps_the_values_line_above_it(self):
        log = ["  ok the page opens", "  ok the sheet loads", "  ok the replay starts",
               "the replay's sheets: [3,4]",
               "file:///work/waymark10/scripts/ui-drive.mjs:175",
               '  if (!cond) throw new Error("FAILED: " + name);',
               "                   ^", "",
               "Error: FAILED: the replay's sheets match",
               "    at ok (file:///work/waymark10/scripts/ui-drive.mjs:175:20)",
               "##[error]Process completed with exit code 1."]
        self.assertEqual(tools._failing_tests("ui-drive", log), [{
            "test": None, "job": "ui-drive",
            "lines": [log[3], log[4], log[5], log[6], log[8]]}])

    def test_the_lines_above_a_drive_s_marks_stay_in_the_line_budget(self):
        log = []
        for i in range(tools.LOG_AFTER_MARK - 1):
            log += ["the values of check %d" % i, "Error: FAILED: check %d" % i]
        lines = tools._failing_tests("ui-drive", log)[0]["lines"]
        self.assertEqual(lines, [log[0]] + log[1::2])

    def test_a_kaocha_failure_keeps_its_message_expected_and_actual(self):
        log = ["--- unit (clojure.test) ---",
               "FAIL in waymark10.decision-sugar-test/the-canonical-hash (decision_sugar_test.clj:42)",
               "the canonical hash moved; pin \"b2c9\"",
               "expected: (= \"a1f0\" (hash-of decision))",
               "  actual: (not (= \"a1f0\" \"b2c9\"))",
               "",
               "ERROR in waymark10.core-test/boom (core_test.clj:7)",
               "expected: nil",
               "  actual: java.lang.Exception: boom",
               "FAIL in (factory-test) (core_test.clj:9)",
               "expected: 1",
               "",
               "3 tests, 3 assertions, 1 error, 2 failures."]
        self.assertEqual(tools._failing_tests("unit", log), [
            {"test": "waymark10.decision-sugar-test/the-canonical-hash", "job": "unit",
             "lines": [log[1], log[2], log[3], log[4]]},
            {"test": "waymark10.core-test/boom", "job": "unit", "lines": [log[6], log[7], log[8]]},
            {"test": "factory-test", "job": "unit", "lines": [log[9], log[10]]}])

    def test_a_failure_keeps_twenty_lines_and_two_thousand_characters_at_most(self):
        many = ["FAIL in waymark10.core-test/long (core_test.clj:1)"] + ["line %d" % i for i in range(40)]
        self.assertEqual(tools._failing_tests("unit", many)[0]["lines"], many[:tools.TEST_FAILURE_LINES])
        wide = ["FAIL in waymark10.core-test/wide (core_test.clj:1)"] + ["y" * 250] * 19
        lines = tools._failing_tests("unit", wide)[0]["lines"]
        self.assertLessEqual(sum(len(line) for line in lines), tools.TEST_FAILURE_CHARS)
        self.assertEqual(lines[:2], wide[:2])

    def test_an_over_long_failure_keeps_its_head_and_its_error_line(self):
        self.make_test()
        wide = "x" * 400
        logs = {1: "FAIL in (factory-test)\n" + "\n".join("expected: %d %s" % (i, wide) for i in range(8)),
                2: "##[group]Run actionlint\n##[endgroup]\n"
                   + "\n".join("lint finding %d %s" % (i, wide) for i in range(11))
                   + "\n##[error]Process completed with exit code 1.\nPost job cleanup."}

        class Client:
            def steps(self, run_id):
                return [{"id": 1, "name": "unit", "result": "failure"},
                        {"id": 2, "name": "actionlint", "result": "failure"}]

            def step_log(self, run_id, job_id):
                return logs[job_id]

        failures = tools._failures(self.bench, self.bench.repo("demo"), Client(), 31)
        lint = failures[1]
        self.assertEqual(lint["job"], "actionlint")
        self.assertTrue(lint["lines"][0].startswith("lint finding 0"))
        self.assertEqual(lint["lines"][-1], "##[error]Process completed with exit code 1.")
        self.assertLessEqual(sum(len(line) + 1 for failure in failures[:2] for line in failure["lines"])
                             + len("factory-test"), tools.TEST_FAILURE_BYTES)

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

    def test_a_refused_scratch_push_refuses_and_dispatches_nothing(self):
        head = self.make_test()
        path = self.bench.worktree(self.bench.repo("demo"), "work")
        util.write(os.path.join(path, "dirty.txt"), "an edit not yet committed\n")
        hook = os.path.join(self.clone_url[len("file://"):], "hooks", "pre-receive")
        util.write(hook, "#!/bin/sh\necho no scratch here >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        self.serve(head, "completed", "success")
        answer = self.refused("test", branch="work", select="waymark.core-test")
        self.assertEqual(answer["refused"], "scratch_push")
        self.assertTrue(answer["dirty_included"])
        self.assertEqual(answer["paths"], ["dirty.txt"])
        self.assertEqual(self.dispatches(), [])
        self.assertEqual(self.scratch(), "")
        self.assertEqual(util.git(["rev-parse", "HEAD"], cwd=path).strip(), head)
        self.assertIn("dirty.txt", util.git(["status", "--porcelain"], cwd=path))

    def test_a_conflicted_pull_keeps_its_merge_through_a_test_of_the_dirty_worktree(self):
        self.make_test()
        path = self.bench.worktree(self.bench.repo("demo"), "work")
        util.write(os.path.join(path, "docs/a.txt"), "alpha\nours\ncharlie\n")
        util.git(["commit", "-q", "-am", "our line"], cwd=path)
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "main", "docs/a.txt", "alpha\ntheirs\ncharlie\n")
        pulled = self.ok("pull", branch="work", **{"from": "base"})
        self.assertEqual(pulled["conflicts"], ["docs/a.txt"])
        self.serve(util.git(["rev-parse", "HEAD"], cwd=path).strip(), "completed", "success")
        self.assertTrue(self.ok("test", branch="work", select="waymark.core-test")["dirty_included"])
        util.git(["rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=path)
        self.assertTrue(self.ok("status", branch="work")["merge_in_progress"])
        still = self.ok("pull", branch="work", **{"from": "base"})
        self.assertEqual(still["conflicts"], ["docs/a.txt"])
        self.assertTrue(still["merge_in_progress"])
        util.write(os.path.join(path, "docs/a.txt"), "alpha\nboth\ncharlie\n")
        self.assertTrue(self.ok("pull", branch="work", **{"from": "base"})["merged"])
        parents = util.git(["log", "-1", "--format=%P", "HEAD"], cwd=path).split()
        self.assertEqual(len(parents), 2)

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

    def test_a_select_pattern_passes_a_python_module_and_refuses_the_rest(self):
        head = self.make_test(test={"workflow": "tests.yml", "input": "only",
                                    "select_pattern": PYTHON_SELECT})
        for select in ("tests.test_bench_test", "tests.test_bench_test.TestTrain"):
            self.serve(head, "completed", "success")
            self.ok("test", branch="work", select=select)
            self.assertEqual(self.dispatches(),
                             [{"ref": "bench-test/work", "inputs": {"only": select}}])
            self.ok("test_result", run_id=31)
        self.calls = []
        for select in ("test-factory", "factory10.merge-line-test"):
            answer = self.refused("test", branch="work", select=select)
            self.assertEqual(answer["refused"], "input")
            self.assertEqual(answer["field"], "select")
            self.assertEqual(answer["select_pattern"], PYTHON_SELECT)
            self.assertEqual(self.calls, [])

    def test_without_a_select_pattern_the_clojure_shape_applies(self):
        self.make_test()
        self.serve(None, "completed", "success")
        answer = self.refused("test", branch="work", select="tests.test_bench_test")
        self.assertEqual(answer["refused"], "input")
        self.assertIn("factory10.merge-line-test", answer["reason"])
        self.assertEqual(self.calls, [])

    def test_a_select_pattern_that_is_not_a_regex_is_a_config_error(self):
        with self.assertRaises(config_module.ConfigError):
            config_module.test_from_dict("demo", {"workflow": "tests.yml", "input": "only",
                                                  "select_pattern": "("})

    def test_an_order_dispatches_its_namespaces_joined_by_spaces(self):
        head = self.make_test()
        self.serve(head, "completed", "success")
        self.ok("test", branch="work", order=["waymark.a-test", "waymark.b-test"])
        self.assertEqual(self.dispatches(),
                         [{"ref": "bench-test/work",
                           "inputs": {"order": "waymark.a-test waymark.b-test"}}])

    def test_an_order_rides_the_order_input_the_block_names(self):
        head = self.make_test(test={"workflow": "tests.yml", "input": "only",
                                    "order_input": "sequence"})
        self.serve(head, "completed", "success")
        self.ok("test", branch="work", order=["waymark.a-test"])
        self.assertEqual(self.dispatches(),
                         [{"ref": "bench-test/work", "inputs": {"sequence": "waymark.a-test"}}])

    def test_an_order_with_an_invalid_namespace_refuses_before_dispatch(self):
        self.make_test()
        self.serve(None, "completed", "success")
        answer = self.refused("test", branch="work", order=["waymark.a-test", "test-factory"])
        self.assertEqual(answer["refused"], "input")
        self.assertEqual(answer["field"], "order")
        self.assertEqual(answer["select"], "test-factory")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.scratch(), "")

    def test_select_and_order_together_refuse(self):
        self.make_test()
        self.serve(None, "completed", "success")
        answer = self.refused("test", branch="work", select="waymark.a-test",
                              order=["waymark.b-test"])
        self.assertEqual(answer["refused"], "input")
        self.assertEqual(answer["field"], "order")
        self.assertEqual(self.calls, [])

    def test_an_order_input_that_is_not_a_string_is_a_config_error(self):
        with self.assertRaises(config_module.ConfigError):
            config_module.test_from_dict("demo", {"workflow": "tests.yml", "input": "only",
                                                  "order_input": 3})

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
        self.slept = []
        self.after_dispatch = {}
        self.addCleanup(setattr, tools, "_sleep", tools._sleep)
        tools._sleep = self.slept.append
        self.make({"stages": [], "pull_request": PULL_REQUEST})
        # pull requests 1 and 2 change the same line: 2 conflicts once 1 is in
        for number, word in ((1, "delta"), (2, "echo")):
            path = util.clone(self.root, self.clone_url, name="pr%d" % number)
            util.push_change(path, "pr%d" % number, "docs/a.txt", "alpha\n%s\ncharlie\n" % word)
            sha = util.git(["rev-parse", "HEAD"], cwd=path).strip()
            self.answers["/pulls/%d" % number] = ("GET", {
                "number": number, "state": "open", "base": {"ref": "main"},
                "head": {"ref": "pr%d" % number, "sha": sha}})

    def fake_http(self, method, url, headers, body=None):
        if method == "PUT" and url.endswith("/merge") and getattr(self, "merge_refusal", None):
            self.calls.append((method, url.split("/repos/o/r", 1)[-1], body))
            return self.merge_refusal
        answer = LandingCase.fake_http(self, method, url, headers, body)
        if method == "POST" and "/dispatches" in url:
            self.answers.update(self.after_dispatch)
        return answer

    def base_head(self):
        return util.git(["ls-remote", self.clone_url, "refs/heads/main"], cwd=self.root).split()[0]

    def train_run(self, run_id, head, status="completed", conclusion="success"):
        return {"id": run_id, "run_number": run_id, "status": status, "conclusion": conclusion,
                "head_sha": head, "head_branch": "train/one",
                "html_url": "https://github.com/o/r/actions/runs/%d" % run_id, "name": "tests"}

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

    def train_pr(self, mergeable=True):
        pr = {"number": 9, "state": "open", "mergeable": mergeable,
              "base": {"ref": "main"}, "head": {"ref": "train/one"}}
        self.answers.update({"/pulls?": ("GET", []), "/pulls/9/merge": ("PUT", {"sha": "f" * 40}),
                             "/pulls/9": ("GET", pr), "/pulls": ("POST", pr)})
        return pr

    def land(self, base, head, ok=True):
        return (self.ok if ok else self.refused)("train_land", base="main", branch="train/one",
                                                 expect_base_head=base, head=head)

    def opened(self):
        return [body for method, path, body in self.calls if method == "POST" and path == "/pulls"]

    def merges(self):
        return [body for method, path, body in self.calls
                if method == "PUT" and path.endswith("/merge")]

    def test_land_merges_one_pull_request_at_head(self):
        base = self.base_head()
        head = self.build([1])["head"]
        self.train_pr()
        answer = self.land(base, head)
        self.assertEqual((answer["landed"], answer["number"], answer["sha"]), (True, 9, "f" * 40))
        [opened] = self.opened()
        self.assertEqual((opened["title"], opened["head"], opened["base"]),
                         ("Merge train: #1", "train/one", "main"))
        self.assertIn("#1", opened["body"])
        self.assertEqual(self.merges(), [{"sha": head, "merge_method": "merge"}])

    def test_land_waits_then_reuses_the_same_pull_request(self):
        base = self.base_head()
        head = self.build([1])["head"]
        pr = self.train_pr(mergeable=None)
        answer = self.land(base, head)
        self.assertEqual((answer["state"], answer["number"]), ("waiting", 9))
        self.assertNotIn("landed", answer)
        self.assertEqual(self.merges(), [])
        self.answers.update({"/pulls?": ("GET", [pr]),
                             "/pulls/9": ("GET", dict(pr, mergeable=True))})
        answer = self.land(base, head)
        self.assertEqual((answer["landed"], answer["number"]), (True, 9))
        self.assertEqual(len(self.opened()), 1)

    def test_land_refuses_when_the_base_moved(self):
        base = self.base_head()
        head = self.build([1])["head"]
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "main", "docs/b.txt", "the base moved\n")
        moved = self.base_head()
        answer = self.refused("train_land", base="main", branch="train/one",
                              expect_base_head=base, head=head)
        self.assertEqual(answer["refused"], "base_moved")
        self.assertEqual(self.opened(), [])
        self.assertEqual(answer["base_head"], moved)
        self.assertEqual(self.base_head(), moved)

    def test_land_answers_merge_refused_when_github_refuses_the_merge(self):
        base = self.base_head()
        head = self.build([1])["head"]
        self.train_pr()
        self.answers["/pulls/9/merge"] = ("PUT", 405)
        answer = self.land(base.upper(), head, ok=False)
        self.assertEqual((answer["refused"], answer["number"]), ("merge_refused", 9))
        self.assertIn("405", answer["reason"])

    def test_land_answers_base_moved_when_github_says_the_base_was_modified(self):
        base = self.base_head()
        head = self.build([1])["head"]
        self.train_pr()
        self.merge_refusal = (409, '{"message":"Base branch was modified. '
                                   'Review and try the merge again."}')
        answer = self.land(base, head, ok=False)
        self.assertEqual((answer["refused"], answer["base_head"]), ("base_moved", base))

    def test_land_waits_while_a_required_check_is_pending(self):
        base = self.base_head()
        head = self.build([1])["head"]
        self.train_pr()
        self.merge_refusal = (405, '{"message":"Required status check \\"gate\\" is expected."}')
        answer = self.land(base, head)
        self.assertEqual((answer["state"], answer["number"]), ("waiting", 9))
        self.assertNotIn("landed", answer)

    def test_land_waiting_says_whether_it_waits_on_checks_or_on_github(self):
        base = self.base_head()
        head = self.build([1])["head"]
        self.train_pr(mergeable=None)
        self.assertEqual(self.land(base, head)["waits_on"], "github")
        self.train_pr()
        self.merge_refusal = (405, '{"message":"Required status check \\"gate\\" is expected."}')
        self.assertEqual(self.land(base, head)["waits_on"], "checks")

    def test_land_refuses_a_short_sha_as_input(self):
        base = self.base_head()
        head = self.build([1])["head"]
        answer = self.refused("train_land", base="main", branch="train/one",
                              expect_base_head=base[:7], head=head)
        self.assertEqual(answer["refused"], "input")
        self.assertEqual(answer["field"], "expect_base_head")
        self.assertEqual(self.base_head(), base)

    def test_delete_refuses_a_branch_that_is_not_a_train(self):
        answer = self.refused("train_delete", branch="main")
        self.assertEqual(answer["refused"], "not_train")
        self.assertTrue(self.base_head())

    def test_delete_removes_a_train_branch(self):
        self.build([1])
        self.ok("train_delete", branch="train/one")
        self.assertEqual(util.git(["ls-remote", self.clone_url, "refs/heads/train/one"],
                                  cwd=self.root), "")

    def test_checks_dispatches_on_the_train_branch_and_finds_the_new_run(self):
        head = self.build([1])["head"]
        old = self.train_run(30, head)
        self.answers[WORKFLOW + "/dispatches"] = ("POST", {})
        self.answers[WORKFLOW + "/runs"] = ("GET", {"workflow_runs": [old]})
        self.after_dispatch = {WORKFLOW + "/runs": ("GET", {"workflow_runs": [
            self.train_run(31, head, "queued", None), old]})}
        answer = self.ok("train_checks", branch="train/one", workflow="tests.yml")
        self.assertEqual(answer["run_id"], 31)
        self.assertEqual(answer["head"], head)
        self.assertEqual(answer["branch"], "train/one")
        dispatched = [body if isinstance(body, dict) else json.loads(body)
                      for method, path, body in self.calls if method == "POST"]
        self.assertEqual(dispatched, [{"ref": "train/one", "inputs": {}}])
        self.assertEqual(self.slept, [])

    def test_checks_answers_no_run_id_when_no_new_run_shows(self):
        head = self.build([1])["head"]
        self.answers[WORKFLOW + "/dispatches"] = ("POST", {})
        self.answers[WORKFLOW + "/runs"] = ("GET", {"workflow_runs": [self.train_run(30, head)]})
        answer = self.ok("train_checks", branch="train/one", workflow="tests.yml")
        self.assertIsNone(answer["run_id"])
        self.assertEqual(answer["head"], head)
        self.assertEqual(len(self.slept), tools.TEST_FIND_TRIES - 1)

    def test_checks_refuses_a_train_branch_that_is_not_pushed(self):
        answer = self.refused("train_checks", branch="train/none", workflow="tests.yml")
        self.assertEqual(answer["refused"], "not_pushed")

    def test_status_maps_the_run_state(self):
        for status, conclusion, state in (("queued", None, "pending"),
                                          ("in_progress", None, "pending"),
                                          ("completed", "success", "success"),
                                          ("completed", "failure", "failure"),
                                          ("completed", "timed_out", "failure"),
                                          ("completed", "cancelled", "cancelled")):
            with self.subTest(status=status, conclusion=conclusion):
                self.answers["/actions/runs/31"] = ("GET", self.train_run(31, "abc", status, conclusion))
                answer = self.ok("train_status", run_id=31)
                self.assertEqual(answer["state"], state)
                self.assertEqual(answer["run_id"], 31)
                self.assertEqual(answer["head"], "abc")
                self.assertEqual(answer["url"], "https://github.com/o/r/actions/runs/31")

    def test_status_finds_the_run_by_branch_and_head(self):
        self.answers[WORKFLOW + "/runs"] = ("GET", {"workflow_runs": [
            self.train_run(32, "other", "in_progress", None),
            self.train_run(31, "abc", "completed", "failure")]})
        answer = self.ok("train_status", branch="train/one", head="abc", workflow="tests.yml")
        self.assertEqual(answer["run_id"], 31)
        self.assertEqual(answer["state"], "failure")

    def test_status_reads_the_runs_of_every_event(self):
        self.answers[WORKFLOW + "/runs"] = ("GET", {"workflow_runs": [self.train_run(33, "abc")]})
        answer = self.ok("train_status", branch="train/one", head="abc", workflow="tests.yml")
        self.assertEqual((answer["run_id"], answer["state"]), (33, "success"))
        [read] = [path for method, path, _ in self.calls if path.startswith(WORKFLOW + "/runs")]
        self.assertIn("branch=train%2Fone", read)
        self.assertNotIn("event=", read)

    def test_open_opens_the_pull_request_once_and_never_merges(self):
        head = self.build([1])["head"]
        pr = self.train_pr()
        answer = self.ok("train_open", base="main", branch="train/one", head=head)
        self.assertEqual((answer["number"], answer["opened"], answer["head"]), (9, True, head))
        [opened] = self.opened()
        self.assertEqual((opened["title"], opened["head"], opened["base"]),
                         ("Merge train: #1", "train/one", "main"))
        self.answers["/pulls?"] = ("GET", [pr])
        answer = self.ok("train_open", base="main", branch="train/one", head=head)
        self.assertEqual((answer["number"], answer["opened"]), (9, False))
        self.assertEqual(len(self.opened()), 1)
        self.assertEqual(self.merges(), [])

    def test_open_refuses_a_head_the_train_branch_is_not_at(self):
        self.build([1])
        self.train_pr()
        self.refused("train_open", base="main", branch="train/one", head="0" * 40)
        self.assertEqual(self.opened(), [])

    def test_open_refuses_a_train_branch_that_is_not_pushed(self):
        self.refused("train_open", base="main", branch="train/none", head="0" * 40)

    def test_status_is_pending_when_no_run_has_the_head(self):
        self.answers[WORKFLOW + "/runs"] = ("GET", {"workflow_runs": [self.train_run(32, "other")]})
        answer = self.ok("train_status", branch="train/one", head="abc", workflow="tests.yml")
        self.assertEqual(answer, {"repo": "demo", "run_id": None, "state": "pending",
                                  "head": "abc", "url": None})

    def test_status_by_branch_skips_the_stale_run(self):
        self.answers[WORKFLOW + "/runs"] = ("GET", {"workflow_runs": [
            self.train_run(33, "abc", "completed", "cancelled"),
            self.train_run(34, "abc", "in_progress", None)]})
        answer = self.ok("train_status", branch="train/one", head="abc", workflow="tests.yml")
        self.assertEqual(answer["run_id"], 33)
        answer = self.ok("train_status", branch="train/one", head="abc", workflow="tests.yml",
                         skip_run_id=33)
        self.assertEqual(answer["run_id"], 34)
        self.assertEqual(answer["state"], "pending")

    def test_status_by_branch_is_pending_when_only_the_stale_run_has_the_head(self):
        self.answers[WORKFLOW + "/runs"] = ("GET", {"workflow_runs": [
            self.train_run(33, "abc", "completed", "cancelled")]})
        answer = self.ok("train_status", branch="train/one", head="abc", workflow="tests.yml",
                         skip_run_id=33)
        self.assertEqual(answer["run_id"], None)
        self.assertEqual(answer["state"], "pending")
