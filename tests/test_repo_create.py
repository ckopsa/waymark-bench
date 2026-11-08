"""The tests of fork: the owner from the settings, its own token, the wait
for the fork, and the answer when the fork is there."""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from bench import config as config_module, forge, tools
from bench.tools import Bench

API = "https://api.github.com"


class RepoCase(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="bench-repo-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.bench = Bench(config_module.from_dict({"data_dir": self.root}))
        self.calls = []
        self.repos = {}
        self.hidden = {}
        self.login = "ckopsa"
        self.post_status = None
        original = forge.http
        forge.http = self.fake_http
        self.addCleanup(setattr, forge, "http", original)
        for patcher in (mock.patch.object(tools, "FORK_WAIT_SECONDS", 0),
                        mock.patch.dict(os.environ, {"BENCH_GITHUB_TOKEN": "main-token",
                                                     "BENCH_REPO_TOKEN": "repo-token"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_http(self, method, url, headers, body=None):
        path = url[len(API):]
        self.calls.append((method, path, body, headers.get("Authorization")))
        if headers.get("Authorization") != "Bearer repo-token":
            return 403, '{"message":"Bad credentials"}'
        if path == "/user":
            return 200, json.dumps({"login": self.login})
        if method == "GET" and path.startswith("/repos/"):
            full = path[len("/repos/"):]
            if self.hidden.get(full):
                self.hidden[full] -= 1
                return 404, "{}"
            if full in self.repos:
                return 200, json.dumps(self.repos[full])
            return 404, '{"message":"Not Found"}'
        if method == "POST":
            if self.post_status:
                return self.post_status, '{"message":"Resource not accessible by personal access token"}'
            owner = body.get("organization") or self.login
            made = self.add(owner, body["name"], False, path[len("/repos/"):-len("/forks")])
            self.hidden[made["full_name"]] = 2
            return 202, json.dumps(made)
        return 404, "{}"

    def add(self, owner, name, private, parent=None):
        full = "%s/%s" % (owner, name)
        self.repos[full] = {"name": name, "full_name": full, "html_url": "https://github.com/" + full,
                            "private": private}
        if parent:
            self.repos[full]["parent"] = {"full_name": parent}
        return self.repos[full]

    def posts(self):
        return [(path, body) for method, path, body, _ in self.calls if method == "POST"]


class TestFork(RepoCase):

    def test_fork_forks_the_default_branch_waits_and_answers_the_fork(self):
        answer, refused = tools.call(self.bench, "fork", {"source_repo": "ashmerra/wehavephones"})
        self.assertFalse(refused, answer)
        self.assertEqual(answer, {"full_name": "ckopsa/wehavephones",
                                  "url": "https://github.com/ckopsa/wehavephones",
                                  "private": False, "existed": False})
        self.assertEqual(self.posts(), [("/repos/ashmerra/wehavephones/forks",
                                         {"name": "wehavephones", "default_branch_only": True})])
        self.assertEqual({token for *_, token in self.calls}, {"Bearer repo-token"})

    def test_a_fork_already_there_is_the_answer_and_nothing_is_posted(self):
        self.add("ckopsa", "wehavephones", False, "ashmerra/wehavephones")
        answer, refused = tools.call(self.bench, "fork", {"source_repo": "ashmerra/wehavephones"})
        self.assertFalse(refused, answer)
        self.assertTrue(answer["existed"])
        self.assertEqual(self.posts(), [])

    def test_a_repository_of_the_name_that_is_no_such_fork_is_refused(self):
        self.add("ckopsa", "wehavephones", True)
        answer, refused = tools.call(self.bench, "fork", {"source_repo": "ashmerra/wehavephones"})
        self.assertTrue(refused)
        self.assertEqual(answer["refused"], "name_taken")
        self.assertEqual(self.posts(), [])

    def test_the_owner_is_the_setting_and_an_organization_is_named_to_github(self):
        self.assertNotIn("owner", tools.TOOLS["fork"]["schema"]["properties"])
        with mock.patch.dict(os.environ, {"BENCH_REPO_OWNER": "house-org"}):
            answer, refused = tools.call(self.bench, "fork", {"source_repo": "ashmerra/wehavephones",
                                                              "name": "phones"})
        self.assertFalse(refused, answer)
        self.assertEqual(answer["full_name"], "house-org/phones")
        self.assertEqual(self.posts()[0][1]["organization"], "house-org")

    def test_a_source_that_is_not_owner_slash_name_is_refused_input(self):
        for source in ("wehavephones", "a/b/c", "../x"):
            answer, refused = tools.call(self.bench, "fork", {"source_repo": source})
            self.assertEqual(answer["refused"], "input", source)
        self.assertEqual(self.calls, [])


class TestTheRepoToken(RepoCase):

    def test_without_it_fork_refuses_and_the_main_token_never_stands_in(self):
        with mock.patch.dict(os.environ, {"BENCH_REPO_TOKEN": ""}):
            answer, refused = tools.call(self.bench, "fork", {"source_repo": "ashmerra/wehavephones"})
        self.assertEqual(answer["refused"], "no_repo_token")
        self.assertEqual(self.calls, [])

    def test_a_token_that_may_not_create_is_refused_naming_what_it_needs(self):
        self.post_status = 403
        answer, refused = tools.call(self.bench, "fork", {"source_repo": "ashmerra/wehavephones"})
        self.assertEqual(answer["refused"], "token_lacks_repo_create")
        self.assertIn("Administration: read and write", answer["reason"])
        self.assertNotIn("repo-token", json.dumps(answer))


if __name__ == "__main__":
    unittest.main()
