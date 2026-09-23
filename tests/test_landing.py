"""The tests of the landing: the steps, the rebase, the push, the pull
request, the feedback and the call form of the entry point.

The forge is a fake: the tests replace forge.http with a function that
answers canned JSON, and record what the rig asked.
"""

import io
import json
import os
import shutil
import tempfile
import threading
import unittest
from contextlib import redirect_stdout

from bench import __main__ as entry
from bench import config as config_module
from bench import forge, mcp, tools
from bench.tools import Bench

from . import util


def land_config(root, clone_url, land, name="demo"):
    data_dir = os.path.join(root, "data")
    os.makedirs(data_dir, exist_ok=True)
    return config_module.from_dict({
        "data_dir": data_dir,
        "repos": {name: {"clone_url": clone_url, "default_branch": "main",
                         "deny": ["*.pem", ".env*"], "land": land}},
    })


STAGES = [
    {"name": "setup", "command": "echo setup done"},
    {"name": "test", "command": "echo tests pass"},
]


class LandingCase(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="bench-land-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.clone_url = util.make_origin(self.root)
        self.calls = []
        self.answers = {}
        self.original_http = forge.http
        forge.http = self.fake_http
        self.addCleanup(setattr, forge, "http", self.original_http)
        os.environ["BENCH_GITHUB_TOKEN"] = "fake-token"
        self.addCleanup(os.environ.pop, "BENCH_GITHUB_TOKEN", None)

    def make(self, land):
        self.bench = Bench(land_config(self.root, self.clone_url, land))
        return self.bench

    def fake_http(self, method, url, headers, body=None):
        path = url.split("/repos/o/r", 1)[-1].split("/repositories/w/s", 1)[-1]
        self.calls.append((method, path, body))
        self.assertNotIn("fake-token", json.dumps(body or {}))
        for key, answer in self.answers.items():
            if path.startswith(key) and (isinstance(answer, tuple) and answer[0] == method
                                         or not isinstance(answer, tuple)):
                payload = answer[1] if isinstance(answer, tuple) else answer
                if isinstance(payload, str):
                    return 200, payload
                return 200, json.dumps(payload)
        return 404, "{}"

    def call(self, name, **args):
        # a submit answers at once by default; these tests read the
        # finished landing, so they ask for the wait the old default gave
        if name == "submit":
            args.setdefault("wait", 600)
        args.setdefault("repo", "demo")
        return tools.call(self.bench, name, args)

    def ok(self, name, **args):
        answer, refused = self.call(name, **args)
        self.assertFalse(refused, answer)
        return answer

    def refused(self, name, **args):
        answer, refused = self.call(name, **args)
        self.assertTrue(refused, answer)
        return answer

    def prepared(self, branch="work"):
        self.ok("prepare", branch=branch)
        return self.bench.wt_dir("demo", branch)

    def change(self, path, name="docs/a.txt", text="alpha\ndelta\ncharlie\n"):
        util.write(os.path.join(path, name), text)

    def remote_head(self, branch):
        bare = self.bench.bare_dir("demo")
        util.git(["fetch", "origin"], cwd=bare)
        return util.git(["rev-parse", "refs/remotes/origin/" + branch], cwd=bare).strip()

    def names(self, landing):
        return [(s["name"], s["state"]) for s in landing["steps"]]


class TestLandingSteps(LandingCase):

    def test_submit_lands_through_the_steps_and_pushes(self):
        self.make({"stages": STAGES})
        path = self.prepared()
        self.change(path)
        answer = self.ok("submit", branch="work", message="change one line")
        landing = answer["landing"]
        self.assertEqual(landing["state"], "landed")
        self.assertTrue(answer["pushed"])
        self.assertTrue(answer["committed"])
        self.assertEqual(self.names(landing), [
            ("rebase", "passed"), ("setup", "passed"), ("test", "passed"),
            ("push", "passed"), ("pull_request", "skipped")])
        self.assertIn("tests pass", landing["steps"][2]["output"])
        self.assertEqual(landing["steps"][2]["exit_code"], 0)
        self.assertEqual(self.remote_head("work"), answer["commit"])
        self.assertEqual(landing["head"], answer["commit"])
        self.assertFalse(landing["running"])

    def test_the_record_keeps_who_landed_it_and_what_it_answers(self):
        # the engine mirrors the record and wakes the next seat on its
        # outcome; that seat finds its work from these three fields
        self.make({"stages": STAGES})
        path = self.prepared()
        self.change(path)
        answer = self.ok("submit", branch="work", message="change one line",
                         seat="migration-engineer", sitting="s-1", **{"for": "verdict-9"})
        with open(self.bench.landings.get(self.bench.repo("demo"), "work").path()) as handle:
            record = json.load(handle)
        self.assertEqual(answer["landing"]["seat"], "migration-engineer")
        self.assertEqual((record["seat"], record["sitting"], record["for"]),
                         ("migration-engineer", "s-1", "verdict-9"))

    def test_a_step_with_commit_commits_what_it_changed(self):
        self.make({"stages": [
            {"name": "format", "command": "printf 'formatted\\n' >> docs/a.txt", "commit": "auto-format"},
        ]})
        path = self.prepared()
        self.change(path)
        answer = self.ok("submit", branch="work", message="change one line",
                         trailers=["Seat: clerk-1"])
        landing = answer["landing"]
        self.assertEqual(landing["state"], "landed")
        formatted = landing["steps"][1]
        self.assertEqual(formatted["name"], "format")
        self.assertIsNotNone(formatted["commit"])
        self.assertNotEqual(formatted["commit"], answer["commit"])
        self.assertEqual(self.remote_head("work"), formatted["commit"])
        message = util.git(["log", "-1", "--format=%B", formatted["commit"]], cwd=path)
        self.assertIn("auto-format", message)
        self.assertIn("Seat: clerk-1", message)
        self.assertEqual(landing["head"], formatted["commit"])

    def test_a_failed_step_refuses_with_its_output_and_locations(self):
        self.make({"stages": [
            {"name": "test", "command": "echo 'src/app.py:3: assertion failed'; echo boom; exit 4"},
            {"name": "never", "command": "echo never"},
        ]})
        path = self.prepared()
        self.change(path)
        answer = self.refused("submit", branch="work", message="break it")
        self.assertEqual(answer["refused"], "landing_failed")
        self.assertEqual(answer["step"], "test")
        landing = answer["landing"]
        self.assertEqual(landing["state"], "failed")
        self.assertEqual(landing["failed_step"], "test")
        self.assertIn("exit code 4", landing["reason"])
        test = landing["steps"][1]
        self.assertEqual(test["exit_code"], 4)
        self.assertIn("boom", test["output"])
        self.assertEqual(self.names(landing), [("rebase", "passed"), ("test", "failed")])
        self.assertFalse(landing["pushed"])
        # The commit is on the branch, not on the remote.
        bare = self.bench.bare_dir("demo")
        util.git(["fetch", "origin"], cwd=bare)
        self.assertNotIn("work", util.git(["branch", "-r"], cwd=bare))
        # The feedback tool gives the failure as one finding, with the location.
        feedback = self.ok("feedback", branch="work")
        finding = feedback["findings"][0]
        self.assertEqual(finding["source"], "landing")
        self.assertEqual(finding["step"], "test")
        self.assertEqual(finding["locations"], [{"path": "src/app.py", "line": 3}])
        self.assertIn("forge: no pull_request block", feedback["unavailable"][0])

    def test_a_step_that_times_out_is_killed(self):
        self.make({"stages": [{"name": "slow", "command": "sleep 30", "timeout": 1}]})
        path = self.prepared()
        self.change(path)
        answer = self.refused("submit", branch="work", message="slow")
        self.assertEqual(answer["refused"], "landing_failed")
        self.assertIn("timed out", answer["reason"])
        self.assertEqual(answer["landing"]["steps"][1]["exit_code"], -1)

    def test_the_step_environment_carries_the_configured_names(self):
        self.make({"env": {"BENCH_TEST_HOME": "~/somewhere"},
                   "stages": [{"name": "env", "command": "echo $BENCH_TEST_HOME $BENCH_BRANCH $BENCH_TARGET"}]})
        path = self.prepared()
        self.change(path)
        answer = self.ok("submit", branch="work", message="env")
        output = answer["landing"]["steps"][1]["output"]
        self.assertIn(os.path.expanduser("~/somewhere"), output)
        self.assertIn("work main", output)


class TestLandingRebase(LandingCase):

    def test_the_landing_rebases_onto_the_moved_target(self):
        self.make({"stages": []})
        path = self.prepared()
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "main", "docs/b.txt", "bravo\n", message="main moved")
        self.change(path)
        answer = self.ok("submit", branch="work", message="mine")
        landing = answer["landing"]
        self.assertEqual(landing["state"], "landed")
        self.assertTrue(landing["rebased"])
        self.assertNotEqual(landing["head"], answer["commit"])
        parent = util.git(["rev-parse", "HEAD^"], cwd=path).strip()
        self.assertEqual(parent, self.remote_head("main"))
        self.assertEqual(self.remote_head("work"), landing["head"])
        self.assertTrue(os.path.isfile(os.path.join(path, "docs/b.txt")))

    def test_a_rebase_conflict_fails_and_leaves_the_worktree_clean(self):
        self.make({"stages": []})
        path = self.prepared()
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "main", "docs/a.txt", "theirs\n", message="main moved")
        self.change(path, text="mine\n")
        answer = self.refused("submit", branch="work", message="mine")
        self.assertEqual(answer["refused"], "landing_failed")
        self.assertEqual(answer["step"], "rebase")
        self.assertIn("docs/a.txt", answer["reason"])
        status = self.ok("status", branch="work")
        self.assertEqual(status["dirty"], 0)
        self.assertEqual(status["landing"]["state"], "failed")
        with open(os.path.join(path, "docs/a.txt")) as handle:
            self.assertEqual(handle.read(), "mine\n")

    def test_a_branch_with_a_merge_keeps_it_and_merges_the_moved_target(self):
        self.make({"stages": []})
        path = self.prepared()
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "main", "docs/a.txt", "theirs\n", message="main moved")
        self.change(path, text="mine\n")
        util.git(["commit", "-am", "mine"], cwd=path)
        util.git(["fetch", "origin"], cwd=self.bench.bare_dir("demo"))
        # The seat merges the target in and resolves the conflict itself.
        with self.assertRaises(Exception):
            util.git(["merge", "origin/main"], cwd=path)
        util.write(os.path.join(path, "docs/a.txt"), "mine\ntheirs\n")
        util.git(["commit", "-am", "resolve"], cwd=path)
        resolved = util.git(["rev-parse", "HEAD"], cwd=path).strip()
        util.push_change(other, "main", "docs/b.txt", "bravo\n", message="main moved again")
        self.change(path, name="docs/c.txt", text="charlie\n")
        answer = self.ok("submit", branch="work", message="more")
        landing = answer["landing"]
        self.assertEqual(landing["state"], "landed", landing)
        self.assertEqual(self.remote_head("work"), landing["head"])
        util.git(["merge-base", "--is-ancestor", resolved, "HEAD"], cwd=path)
        util.git(["merge-base", "--is-ancestor", "origin/main", "HEAD"], cwd=path)
        with open(os.path.join(path, "docs/a.txt")) as handle:
            self.assertEqual(handle.read(), "mine\ntheirs\n")
        self.assertTrue(os.path.isfile(os.path.join(path, "docs/b.txt")))

    def test_a_second_landing_pushes_with_a_lease(self):
        self.make({"stages": []})
        path = self.prepared()
        other = util.clone(self.root, self.clone_url)
        self.change(path)
        first = self.ok("submit", branch="work", message="one")
        util.push_change(other, "main", "docs/b.txt", "bravo\n", message="main moved")
        self.change(path, text="alpha\necho\ncharlie\n")
        second = self.ok("submit", branch="work", message="two")
        self.assertTrue(second["landing"]["rebased"])
        self.assertEqual(self.remote_head("work"), second["landing"]["head"])
        self.assertNotEqual(first["landing"]["head"], second["landing"]["head"])
        log = util.git(["log", "--format=%s", "origin/main..HEAD"], cwd=path).split()
        self.assertEqual(log, ["two", "one"])

    def test_without_rebase_the_landing_pushes_the_branch_as_it_is(self):
        self.make({"rebase": False, "stages": []})
        path = self.prepared()
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "main", "docs/b.txt", "bravo\n", message="main moved")
        self.change(path)
        answer = self.ok("submit", branch="work", message="mine")
        self.assertEqual(answer["landing"]["steps"][0]["name"], "push")
        self.assertFalse(answer["landing"]["rebased"])
        self.assertFalse(os.path.isfile(os.path.join(path, "docs/b.txt")))


class TestLandingGuards(LandingCase):

    def test_submit_answers_at_once_unless_it_is_asked_to_wait(self):
        # a waymark engine gives up on a call after 30 seconds and marks the
        # server dark, so a bare submit must not hold the call for the suite
        self.make({"stages": [{"name": "slow", "command": "sleep 2"}]})
        path = self.prepared()
        self.change(path)
        answer, refused = tools.call(self.bench, "submit",
                                     {"repo": "demo", "branch": "work", "message": "slow"})
        self.assertFalse(refused, answer)
        self.assertEqual(answer["landing"]["state"], "running")
        self.assertTrue(answer["landing"]["running"])
        self.bench.landings.get(self.bench.repo("demo"), "work").wait(30)
        self.assertEqual(self.ok("status", branch="work")["landing"]["state"], "landed")

    def test_a_running_landing_refuses_edit_and_is_followed_by_status(self):
        self.make({"stages": [{"name": "slow", "command": "sleep 2"}]})
        path = self.prepared()
        self.change(path)
        answer = self.ok("submit", branch="work", message="slow", wait=0)
        self.assertEqual(answer["landing"]["state"], "running")
        self.assertTrue(answer["landing"]["running"])
        refused = self.refused("edit", branch="work", path="docs/a.txt", old="alpha", new="x")
        self.assertEqual(refused["refused"], "landing_running")
        self.assertEqual(self.refused("pull", branch="work", **{"from": "base"})["refused"],
                         "landing_running")
        self.assertEqual(self.refused("discard", branch="work")["refused"], "landing_running")
        self.assertEqual(self.refused("submit", branch="work", message="x")["refused"],
                         "landing_running")
        # Reads go on.
        self.ok("read", branch="work", path="docs/a.txt")
        self.bench.landings.get(self.bench.repo("demo"), "work").wait(30)
        status = self.ok("status", branch="work")
        self.assertEqual(status["landing"]["state"], "landed")
        self.assertEqual(status["ahead"], 1)

    def test_a_clean_worktree_may_land_again_after_a_failure_but_not_after_a_landing(self):
        marker = os.path.join(self.root, "pass")
        self.make({"stages": [{"name": "test", "command": "test -f %s" % marker}]})
        path = self.prepared()
        self.change(path)
        failed = self.refused("submit", branch="work", message="one")
        self.assertEqual(failed["refused"], "landing_failed")
        util.write(marker, "now it passes\n")
        retried = self.ok("submit", branch="work", message="retry")
        self.assertFalse(retried["committed"])
        self.assertEqual(retried["commit"], failed["commit"])
        self.assertEqual(retried["landing"]["state"], "landed")
        again = self.refused("submit", branch="work", message="again")
        self.assertEqual(again["refused"], "nothing_to_commit")

    def test_each_attempt_keeps_a_file_the_next_submit_does_not_touch(self):
        # the branch's file is overwritten by the retry; the engine reads
        # the attempts, so a finished one must never change again
        marker = os.path.join(self.root, "pass")
        self.make({"stages": [{"name": "test", "command": "test -f %s" % marker}]})
        path = self.prepared()
        self.change(path)
        self.refused("submit", branch="work", message="one")
        attempts = os.path.join(self.bench.repo_dir("demo"), "attempts", "work")
        [first] = os.listdir(attempts)
        with open(os.path.join(attempts, first)) as handle:
            failed = handle.read()
        util.write(marker, "now it passes\n")
        self.ok("submit", branch="work", message="retry")
        self.assertEqual(len(os.listdir(attempts)), 2)
        with open(os.path.join(attempts, first)) as handle:
            self.assertEqual(handle.read(), failed)
        self.assertEqual(json.loads(failed)["state"], "failed")

    def test_the_target_branch_is_refused(self):
        self.make({"target": "release", "stages": []})
        util.push_change(util.clone(self.root, self.clone_url), "release", "r.txt", "r\n")
        path = self.prepared("release")
        self.change(path)
        self.assertEqual(self.refused("submit", branch="release", message="x")["refused"],
                         "default_branch")

    def test_a_landing_cut_by_a_restart_reads_as_failed(self):
        self.make({"stages": []})
        path = self.prepared()
        repo = self.bench.repo("demo")
        item = self.bench.landings.get(repo, "work", create=True)
        item.state.update({"state": "running", "started": "2026-01-01T00:00:00Z",
                           "steps": [{"name": "test", "state": "running", "output": ""}]})
        item.save()
        fresh = Bench(self.bench.config)
        loaded = fresh.landings.get(repo, "work")
        self.assertEqual(loaded.state["state"], "failed")
        self.assertEqual(loaded.state["failed_step"], "test")
        self.assertIn("restarted", loaded.state["reason"])
        self.change(path)
        self.bench = fresh
        self.assertEqual(self.ok("submit", branch="work", message="after")["landing"]["state"],
                         "landed")

    def test_discard_with_drop_forgets_the_landing(self):
        self.make({"stages": []})
        path = self.prepared()
        self.change(path)
        self.ok("submit", branch="work", message="one")
        self.ok("discard", branch="work", drop_branch=True)
        self.assertIsNone(self.bench.landings.get(self.bench.repo("demo"), "work"))
        self.ok("prepare", branch="work")
        self.assertIsNone(self.ok("status", branch="work")["landing"])


class TestLandingConfig(unittest.TestCase):

    def test_a_reserved_or_repeated_stage_name_is_refused(self):
        for stages in ([{"name": "push", "command": "x"}],
                       [{"name": "a", "command": "x"}, {"name": "a", "command": "y"}],
                       [{"name": "a"}], [{"name": "bad name", "command": "x"}]):
            with self.assertRaises(config_module.ConfigError):
                config_module.from_dict({"data_dir": "/tmp/x", "repos": {
                    "r": {"clone_url": "u", "land": {"stages": stages}}}})

    def test_the_land_block_defaults(self):
        config = config_module.from_dict({"data_dir": "/tmp/x", "repos": {
            "r": {"clone_url": "git@bitbucket.org:w/s.git", "default_branch": "dev",
                  "land": {"pull_request": True, "env": {"H": "~/h"}}}}})
        land = config.repo("r").land
        self.assertEqual(land.target, "dev")
        self.assertTrue(land.rebase)
        self.assertEqual(land.stages, [])
        self.assertEqual(land.pull_request, {})
        self.assertEqual(land.env["H"], os.path.expanduser("~/h"))
        self.assertEqual(forge.detect("git@bitbucket.org:w/s.git"), ("bitbucket", "w", "s"))
        self.assertEqual(forge.detect("https://github.com/o/r"), ("github", "o", "r"))
        self.assertIsNone(forge.detect("file:///tmp/origin.git"))
        self.assertIsNone(config_module.from_dict({"data_dir": "/tmp/x", "repos": {
            "r": {"clone_url": "u", "land": False}}}).repo("r").land)


GITHUB_PR = {"number": 7, "html_url": "https://github.com/o/r/pull/7", "state": "open",
             "title": "change one line", "head": {"ref": "work", "sha": "abc123"},
             "base": {"ref": "main"}, "node_id": "PR_node_7"}
GRAPHQL = "https://api.github.com/graphql"
AUTO_MERGE_ON = {"data": {"enablePullRequestAutoMerge": {"pullRequest": {"number": 7}}}}


class TestPullRequestAndFeedback(LandingCase):

    def github(self):
        return self.make({"stages": [], "pull_request": {"provider": "github", "owner": "o", "repo": "r"}})

    def github_auto_merge(self):
        return self.make({"stages": [], "pull_request": {
            "provider": "github", "owner": "o", "repo": "r", "auto_merge": True}})

    def graphql_calls(self):
        return [body for method, path, body in self.calls if path == GRAPHQL]

    def test_the_landing_opens_the_pull_request_one_time(self):
        self.github()
        path = self.prepared()
        self.answers = {"/pulls?": ("GET", []), "/pulls": ("POST", GITHUB_PR)}
        self.change(path)
        answer = self.ok("submit", branch="work", message="change one line",
                         description="the body")
        pr = answer["landing"]["pull_request"]
        self.assertTrue(pr["created"])
        self.assertEqual(pr["url"], "https://github.com/o/r/pull/7")
        self.assertEqual(pr["number"], 7)
        posted = [body for method, path, body in self.calls if method == "POST"]
        self.assertEqual(posted, [{"title": "change one line", "body": "the body",
                                   "head": "work", "base": "main"}])
        # The second landing finds the pull request and does not open another.
        self.answers = {"/pulls?": ("GET", [GITHUB_PR])}
        self.calls = []
        self.change(path, text="alpha\necho\ncharlie\n")
        again = self.ok("submit", branch="work", message="two")
        self.assertFalse(again["landing"]["pull_request"]["created"])
        self.assertEqual([m for m, _, _ in self.calls], ["GET"])

    def test_auto_merge_turns_auto_merge_on_for_the_pull_request(self):
        self.github_auto_merge()
        path = self.prepared()
        self.answers = {"/pulls?": ("GET", []), "/pulls": ("POST", GITHUB_PR),
                        GRAPHQL: ("POST", AUTO_MERGE_ON)}
        self.change(path)
        answer = self.ok("submit", branch="work", message="change one line")
        landing = answer["landing"]
        self.assertEqual(landing["steps"][-1]["state"], "passed")
        self.assertEqual(landing["pull_request"]["number"], 7)
        self.assertEqual(landing["auto_merge"], {"enabled": True, "refused": None})
        asked = self.graphql_calls()
        self.assertEqual(len(asked), 1)
        self.assertIn("enablePullRequestAutoMerge", asked[0]["query"])
        self.assertEqual(asked[0]["variables"], {"id": "PR_node_7"})

    def test_a_refused_auto_merge_is_a_finding_and_keeps_the_pull_request(self):
        self.github_auto_merge()
        path = self.prepared()
        refusal = {"errors": [{"message": "Pull request Auto merge is not allowed "
                                          "for this repository"}]}
        self.answers = {"/pulls?": ("GET", []), "/pulls": ("POST", GITHUB_PR),
                        GRAPHQL: ("POST", refusal)}
        self.change(path)
        answer = self.ok("submit", branch="work", message="change one line")
        landing = answer["landing"]
        self.assertEqual(landing["steps"][-1]["state"], "passed")
        self.assertEqual(landing["pull_request"]["number"], 7)
        self.assertFalse(landing["auto_merge"]["enabled"])
        self.assertIn("not allowed", landing["auto_merge"]["refused"])
        self.answers = {"/pulls?": ("GET", [GITHUB_PR])}
        feedback = self.ok("feedback", branch="work")
        self.assertEqual(feedback["pull_request"]["number"], 7)
        found = [f for f in feedback["findings"] if f["source"] == "auto_merge"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "warning")
        self.assertIn("not allowed", found[0]["message"])
        self.assertEqual(found[0]["url"], "https://github.com/o/r/pull/7")

    def test_no_auto_merge_in_the_block_asks_the_forge_for_none(self):
        self.github()
        path = self.prepared()
        self.answers = {"/pulls?": ("GET", []), "/pulls": ("POST", GITHUB_PR)}
        self.change(path)
        answer = self.ok("submit", branch="work", message="change one line")
        self.assertEqual(answer["landing"]["pull_request"]["number"], 7)
        self.assertIsNone(answer["landing"]["auto_merge"])
        self.assertEqual(self.graphql_calls(), [])

    def test_pull_request_false_lands_without_one(self):
        self.github()
        path = self.prepared()
        self.change(path)
        answer = self.ok("submit", branch="work", message="quiet", pull_request=False)
        self.assertEqual(answer["landing"]["steps"][-1]["state"], "skipped")
        self.assertEqual(self.calls, [])

    def test_a_forge_refusal_fails_the_pull_request_step(self):
        self.github()
        path = self.prepared()
        self.change(path)
        forge.http = lambda method, url, headers, body=None: (403, '{"message":"no"}')
        answer = self.refused("submit", branch="work", message="x")
        self.assertEqual(answer["step"], "pull_request")
        self.assertIn("refused the credential", answer["reason"])
        self.assertTrue(answer["landing"]["pushed"])

    def test_feedback_turns_the_github_answers_into_findings(self):
        self.github()
        path = self.prepared()
        self.answers = {"/pulls?": ("GET", []), "/pulls": ("POST", GITHUB_PR)}
        self.change(path)
        self.ok("submit", branch="work", message="change one line")
        self.answers = {
            "/pulls?": ("GET", [GITHUB_PR]),
            "/actions/runs?": {"workflow_runs": [
                {"id": 11, "run_number": 3, "status": "completed", "conclusion": "failure",
                 "head_sha": "abc123", "html_url": "https://github.com/o/r/actions/runs/11",
                 "name": "tests"}]},
            "/actions/runs/11/jobs": {"jobs": [
                {"id": 21, "name": "unit", "status": "completed", "conclusion": "failure",
                 "steps": [{"name": "pytest", "conclusion": "failure"}]}]},
            "/actions/jobs/21/logs": "collected 3 items\nFAILED tests/test_app.py:12: boom\n",
            "/commits/abc123/check-runs": {"check_runs": [
                {"name": "sonar", "status": "completed", "conclusion": "failure",
                 "output": {"title": "Quality gate failed"}, "html_url": "https://sonar/x"}]},
            "/issues/7/comments": [{"id": 1, "user": {"login": "reviewer"}, "body": "please rename",
                                    "created_at": "2026-09-18T00:00:00Z", "html_url": "c1"}],
            "/pulls/7/comments": [{"id": 2, "user": {"login": "reviewer"}, "body": "wrong here",
                                   "path": "docs/a.txt", "line": 2, "created_at": "2026-09-18T00:01:00Z",
                                   "html_url": "c2"}],
        }
        feedback = self.ok("feedback", branch="work")
        self.assertEqual(feedback["pull_request"]["number"], 7)
        self.assertEqual(feedback["unavailable"], [])
        sources = [(f["source"], f["severity"]) for f in feedback["findings"]]
        self.assertEqual(sources, [("pipeline", "error"), ("status", "error"),
                                   ("review", "comment"), ("review", "comment")])
        pipeline = feedback["findings"][0]
        self.assertEqual(pipeline["step"], "unit")
        self.assertEqual(pipeline["locations"], [{"path": "tests/test_app.py", "line": 12}])
        self.assertEqual(feedback["findings"][1]["message"], "Quality gate failed")
        inline = feedback["findings"][3]
        self.assertEqual((inline["path"], inline["line"], inline["author"]), ("docs/a.txt", 2, "reviewer"))
        self.assertEqual(feedback["pipelines"][0]["steps"][0]["failed_steps"], ["pytest"])

    def test_feedback_turns_the_bitbucket_answers_into_findings(self):
        self.make({"stages": [], "pull_request": {"provider": "bitbucket", "workspace": "w", "repo": "s"}})
        os.environ["BENCH_BITBUCKET_USER"] = "me"
        os.environ["BENCH_BITBUCKET_TOKEN"] = "app-password"
        self.addCleanup(os.environ.pop, "BENCH_BITBUCKET_USER", None)
        self.addCleanup(os.environ.pop, "BENCH_BITBUCKET_TOKEN", None)
        path = self.prepared()
        pr = {"id": 547, "state": "OPEN", "title": "t", "links": {"html": {"href": "https://bb/pr/547"}},
              "source": {"branch": {"name": "work"}, "commit": {"hash": "def456"}},
              "destination": {"branch": {"name": "main"}},
              "participants": [{"user": {"display_name": "Ada"}, "approved": False,
                                "state": "changes_requested"}],
              "task_count": 1}
        self.answers = {
            "/pullrequests?": ("GET", {"values": [pr]}),
            "/pipelines/?": {"values": [
                {"uuid": "{p1}", "build_number": 9, "state": {"name": "COMPLETED", "result": {"name": "FAILED"}},
                 "target": {"ref_name": "work", "type": "pipeline_ref_target", "commit": {"hash": "def456"}},
                 "created_on": "2026-09-18T00:00:00Z"},
                {"uuid": "{p0}", "build_number": 8, "state": {"name": "COMPLETED", "result": {"name": "SUCCESSFUL"}},
                 "target": {"ref_name": "other"}}]},
            "/pipelines/{p1}/steps/?": {"values": [
                {"uuid": "{s1}", "name": "Install dependencies, lint, build and run tests",
                 "state": {"name": "COMPLETED", "result": {"name": "FAILED"}}, "duration_in_seconds": 300}]},
            "/pipelines/{p1}/steps/{s1}/log": "ruff check src\nsrc/app.py:3:1: E999 bad\n",
            "/commit/def456/statuses": {"values": [
                {"key": "sonar", "name": "SonarCloud", "state": "FAILED", "description": "Quality gate failed",
                 "url": "https://sonar/x"}]},
            "/pullrequests/547/comments": {"values": [
                {"id": 1, "user": {"display_name": "Ada"}, "content": {"raw": "rename this"},
                 "inline": {"path": "src/app.py", "to": 3}, "created_on": "2026-09-18T00:00:00Z",
                 "links": {"html": {"href": "c1"}}},
                {"id": 2, "deleted": True, "content": {"raw": "gone"}}]},
        }
        self.change(path)
        self.ok("submit", branch="work", message="x", pull_request=False)
        feedback = self.ok("feedback", branch="work")
        self.assertEqual(feedback["unavailable"], [])
        self.assertEqual(feedback["pull_request"]["changes_requested"], ["Ada"])
        sources = [f["source"] for f in feedback["findings"]]
        self.assertEqual(sources, ["review", "pipeline", "status", "review"])
        self.assertEqual(feedback["findings"][0]["message"], "changes requested by Ada")
        self.assertEqual(feedback["findings"][1]["locations"], [{"path": "src/app.py", "line": 3}])
        self.assertEqual(feedback["findings"][1]["url"], "https://bitbucket.org/w/s/pipelines/results/9")
        self.assertEqual(len(feedback["pipelines"]), 1)
        self.assertEqual(len(feedback["comments"]), 1)
        self.assertNotIn("app-password", json.dumps(feedback))

    def test_feedback_names_what_it_cannot_reach(self):
        self.github()
        path = self.prepared()
        self.change(path)
        self.ok("submit", branch="work", message="x", pull_request=False)
        forge.http = lambda method, url, headers, body=None: (500, "down")
        feedback = self.ok("feedback", branch="work")
        self.assertIsNone(feedback["pull_request"])
        self.assertTrue(any(item.startswith("pull_request:") for item in feedback["unavailable"]))
        self.assertTrue(any(item.startswith("pipelines:") for item in feedback["unavailable"]))
        self.assertEqual(feedback["findings"], [])


class TestCallForm(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="bench-call-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.clone_url = util.make_origin(self.root)
        self.bench = Bench(util.make_config(self.root, self.clone_url))
        self.httpd = mcp.serve_http(self.bench, 0)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%s/mcp/" % self.httpd.server_address[1]

    def run_call(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = entry.main(["call"] + list(argv) + ["--url", self.url])
        return code, (json.loads(out.getvalue()) if out.getvalue().strip() else None)

    def test_call_drives_a_tool_and_exits_one_on_a_refusal(self):
        code, answer = self.run_call("prepare", "repo=demo", "branch=work")
        self.assertEqual(code, 0)
        self.assertTrue(answer["created"])
        code, answer = self.run_call("find", "repo=demo", "branch=work", "mode=tree", "depth=1")
        self.assertEqual(code, 0)
        self.assertEqual(answer["depth"], 1)
        code, answer = self.run_call("read", "repo=demo", "branch=work", "path=keys/server.pem")
        self.assertEqual(code, 1)
        self.assertEqual(answer["refused"], "denied")
        code, answer = self.run_call("edit", "repo=demo", "branch=work", "path=new.txt",
                                     "create=true", "new=hello")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.bench.wt_dir("demo", "work"), "new.txt")))
