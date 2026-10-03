"""The tests of dispatch and run_status: the POST, the run id lookup, the
status, and the 403 that names the missing permission.

The forge is a stub: the tests replace forge.http.
"""

import json
import os
import shutil
import tempfile
import unittest

from bench import config as config_module, forge, tools
from bench.tools import Bench

from . import util


RUNS = "/actions/workflows/runner-image.yml/runs"
DISPATCHES = "/actions/workflows/runner-image.yml/dispatches"


def run(run_id, status="queued", conclusion=None):
    return {"id": run_id, "run_number": run_id, "status": status, "conclusion": conclusion,
            "head_sha": "abc", "head_branch": "main", "name": "runner image",
            "html_url": "https://github.com/o/r/actions/runs/%s" % run_id}


class DispatchCase(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="bench-dispatch-")
        self.addCleanup(shutil.rmtree, self.root, True)
        data_dir = os.path.join(self.root, "data")
        os.makedirs(data_dir, exist_ok=True)
        self.bench = Bench(config_module.from_dict({
            "data_dir": data_dir,
            "repos": {"demo": {"clone_url": util.make_origin(self.root),
                               "default_branch": "main",
                               "land": {"stages": [], "pull_request": {
                                   "provider": "github", "owner": "o", "repo": "r"}}}},
        }))
        self.calls = []
        self.runs = [run(70, "completed", "success")]
        self.post_status = 204
        # The run shows after this many reads of the runs that follow the POST.
        self.shows_after = 1
        self.reads = 0
        self.posted = False
        self.jobs = []
        self.slept = []
        original = forge.http
        forge.http = self.stub_http
        self.addCleanup(setattr, forge, "http", original)
        sleep = tools._sleep
        tools._sleep = self.slept.append
        self.addCleanup(setattr, tools, "_sleep", sleep)
        os.environ["BENCH_GITHUB_TOKEN"] = "fake-token"
        self.addCleanup(os.environ.pop, "BENCH_GITHUB_TOKEN", None)

    def stub_http(self, method, url, headers, body=None):
        path, _, query = url.split("/repos/o/r", 1)[-1].partition("?")
        self.calls.append((method, path, query, body))
        if method == "POST" and path == DISPATCHES:
            if self.post_status >= 400:
                return self.post_status, '{"message":"Resource not accessible"}'
            self.posted = True
            return self.post_status, ""
        if path == RUNS:
            found = list(self.runs)
            if self.posted:
                self.reads += 1
                if self.shows_after and self.reads >= self.shows_after:
                    found.insert(0, run(77))
            return 200, json.dumps({"workflow_runs": found})
        if path == "/actions/runs/77":
            return 200, json.dumps(run(77, "completed", "failure"))
        if path == "/actions/runs/77/jobs":
            return 200, json.dumps({"jobs": self.jobs})
        return 404, '{"message":"Not Found"}'

    def call(self, tool, /, **args):
        args.setdefault("repo", "demo")
        return tools.call(self.bench, tool, args)

    def dispatch(self, **args):
        args.setdefault("workflow", "runner-image.yml")
        args.setdefault("why", "the runner image is missing")
        return self.call("dispatch", **args)

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


class TestDispatch(DispatchCase):

    def test_dispatch_posts_the_ref_and_inputs_and_answers_the_new_run(self):
        answer, refused = self.dispatch(inputs={"tag": "v2"})
        self.assertFalse(refused, answer)
        self.assertEqual(answer["run_id"], 77)
        self.assertEqual(answer["run_url"], "https://github.com/o/r/actions/runs/77")
        self.assertEqual(answer["ref"], "main")
        self.assertEqual(answer["workflow"], "runner-image.yml")
        self.assertEqual(len(self.posts()), 1)
        method, path, query, body = self.posts()[0]
        self.assertEqual(path, DISPATCHES)
        self.assertEqual(body, {"ref": "main", "inputs": {"tag": "v2"}})
        self.assertEqual(self.slept, [])

    def test_the_run_id_lookup_waits_for_a_run_that_was_not_there_before(self):
        self.shows_after = 3
        answer, refused = self.dispatch(ref="release")
        self.assertFalse(refused, answer)
        self.assertEqual(answer["run_id"], 77)
        self.assertEqual(self.slept, [2, 2])
        self.assertEqual(self.posts()[0][3]["ref"], "release")

    def test_a_run_that_never_shows_answers_a_null_run_id_within_ten_seconds(self):
        self.shows_after = 0
        answer, refused = self.dispatch()
        self.assertFalse(refused, answer)
        self.assertIsNone(answer["run_id"])
        self.assertTrue(answer["dispatched_at"])
        self.assertLessEqual(sum(self.slept), 10)

    def test_a_403_answers_token_lacks_actions_write(self):
        self.post_status = 403
        answer, refused = self.dispatch()
        self.assertTrue(refused)
        self.assertEqual(answer["refused"], "token_lacks_actions_write")

    def test_dispatch_takes_no_why_when_a_gate_holds_it(self):
        answer, refused = self.call("dispatch", workflow="runner-image.yml")
        self.assertFalse(refused, answer)
        self.assertEqual(answer["run_id"], 77)

    def test_a_bad_input_is_refused_before_the_forge(self):
        for args, field in (({"workflow": "../x.yml"}, "workflow"),
                            ({"inputs": ["tag"]}, "inputs"),
                            ({"why": 7}, "why")):
            answer, refused = self.dispatch(**args)
            self.assertTrue(refused, args)
            self.assertEqual(answer["field"], field)
        self.assertEqual(self.calls, [])

    def test_a_repo_not_enrolled_is_refused(self):
        answer, refused = self.dispatch(repo="other")
        self.assertTrue(refused)
        self.assertEqual(answer["refused"], "repo")
        self.assertEqual(self.calls, [])


class TestRunStatus(DispatchCase):

    def test_status_answers_the_conclusion_and_the_failed_steps(self):
        self.jobs = [
            {"id": 1, "name": "build", "status": "completed", "conclusion": "failure",
             "steps": [{"name": "checkout", "conclusion": "success"},
                       {"name": "docker build", "conclusion": "failure"}]},
            {"id": 2, "name": "lint", "status": "completed", "conclusion": "success",
             "steps": [{"name": "ruff", "conclusion": "success"}]}]
        answer, refused = self.call("run_status", run_id=77)
        self.assertFalse(refused, answer)
        self.assertEqual(answer["status"], "completed")
        self.assertEqual(answer["conclusion"], "failure")
        self.assertEqual(answer["run_url"], "https://github.com/o/r/actions/runs/77")
        self.assertEqual(answer["failed_steps"], [{"job": "build", "steps": ["docker build"]}])

    def test_a_run_id_that_is_no_number_is_refused(self):
        answer, refused = self.call("run_status", run_id="77")
        self.assertTrue(refused)
        self.assertEqual(answer["field"], "run_id")
        self.assertEqual(self.calls, [])

    def test_an_unknown_run_is_a_forge_refusal(self):
        answer, refused = self.call("run_status", run_id=5)
        self.assertTrue(refused)
        self.assertEqual(answer["refused"], "forge")


if __name__ == "__main__":
    unittest.main()
