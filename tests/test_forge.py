"""The tests of the forge's one HTTP call: a redirect to another host goes
without the credential.

Two small servers on the loopback stand in for the API and for the log
store that GitHub redirects the log of a job to.
"""

import os
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from bench import forge, tools


PROXIES = ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")


def serve(answer):
    """Starts a server that answers every GET with answer(handler). Gives
    (the server, its base URL, the list of the headers of each request)."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(dict(self.headers))
            status, headers, text = answer(self)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            body = text.encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d" % server.server_port, seen


class TestRedirect(unittest.TestCase):

    def setUp(self):
        env = {name: "" for name in PROXIES}
        env.update({"BENCH_GITHUB_TOKEN": "fake-token", "no_proxy": "*", "NO_PROXY": "*"})
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        for name in PROXIES:
            os.environ.pop(name, None)

        store, self.store_url, self.store_seen = serve(
            lambda h: (200, {"Content-Type": "text/plain"}, "step 1\nFAILED test_one\n"))
        self.addCleanup(store.server_close)
        self.addCleanup(store.shutdown)

        def api_answer(handler):
            if handler.headers.get("Authorization") != "Bearer fake-token":
                return 401, {}, '{"message":"Bad credentials"}'
            if "/actions/jobs/7/logs" in handler.path:
                return 302, {"Location": self.store_url + "/logs/7?sig=signed"}, ""
            return 404, {}, "{}"

        api, self.api_url, self.api_seen = serve(api_answer)
        self.addCleanup(api.server_close)
        self.addCleanup(api.shutdown)
        self.client = forge.GitHub("o", "r")
        self.client.api = self.api_url + "/repos"

    def test_the_log_follows_the_redirect_without_the_token(self):
        text = self.client.step_log(1, 7)
        self.assertEqual(text, "step 1\nFAILED test_one\n")
        self.assertEqual(self.api_seen[0].get("Authorization"), "Bearer fake-token")
        self.assertEqual(len(self.store_seen), 1)
        self.assertNotIn("Authorization", self.store_seen[0])
        self.assertNotIn("fake-token", repr(self.store_seen[0]))

    def test_a_refusal_on_the_first_hop_still_reads_as_refused(self):
        os.environ["BENCH_GITHUB_TOKEN"] = "another-token"
        text = self.client.step_log(1, 7)
        self.assertEqual(text, "(no log: github refused the credential (401))")
        self.assertEqual(self.store_seen, [])

    def test_a_redirect_on_the_same_host_keeps_the_token(self):
        seen = []

        def answer(handler):
            seen.append(handler.headers.get("Authorization"))
            if handler.path == "/first":
                return 302, {"Location": "/second"}, ""
            return 200, {}, "ok"

        server, url, _ = serve(answer)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        status, text = forge.http("GET", url + "/first", {"Authorization": "Bearer fake-token"})
        self.assertEqual((status, text), (200, "ok"))
        self.assertEqual(seen, ["Bearer fake-token", "Bearer fake-token"])


class TestThrottle(unittest.TestCase):
    """A spent rate limit is a throttle with its reset, not a refused credential."""

    def setUp(self):
        env = {name: "" for name in PROXIES}
        env.update({"BENCH_GITHUB_TOKEN": "fake-token", "no_proxy": "*", "NO_PROXY": "*"})
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        for name in PROXIES:
            os.environ.pop(name, None)

    def refusal(self, status, headers, text):
        server, url, _ = serve(lambda h: (status, headers, text))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = forge.GitHub("o", "r")
        client.api = url + "/repos"
        with self.assertRaises(forge.ForgeError) as raised:
            client.request("GET", client.url("/pulls"))
        return raised.exception

    def test_a_403_with_nothing_remaining_is_a_throttle_with_its_reset(self):
        exc = self.refusal(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1790888400"},
                           '{"message":"API rate limit exceeded"}')
        self.assertEqual(exc.reset, "2026-10-01T21:00:00Z")
        self.assertEqual(str(exc), "github's rate limit is spent until 2026-10-01T21:00:00Z")
        self.assertEqual(exc.status, 403)

    def test_a_retry_after_or_a_body_that_names_the_limit_is_a_throttle(self):
        with mock.patch.object(forge.time, "time", return_value=1790888400):
            after = self.refusal(429, {"Retry-After": "30"}, "{}")
            body = self.refusal(403, {}, '{"message":"You have exceeded a secondary rate limit"}')
        self.assertEqual(after.reset, "2026-10-01T21:00:30Z")
        self.assertEqual(body.reset, "2026-10-01T21:01:00Z")
        self.assertNotIn("refused the credential", str(after) + str(body))

    def test_a_plain_403_is_still_a_refused_credential(self):
        exc = self.refusal(403, {"X-RateLimit-Remaining": "4999"}, '{"message":"no"}')
        self.assertIsNone(exc.reset)
        self.assertEqual(str(exc), "github refused the credential (403)")

    def test_the_text_of_an_answer_carries_its_headers(self):
        server, url, _ = serve(lambda h: (200, {"X-RateLimit-Remaining": "12"}, "ok"))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        status, text = forge.http("GET", url, {})
        self.assertEqual((status, text), (200, "ok"))
        self.assertEqual(text.headers.get("x-ratelimit-remaining"), "12")


def a_run(run_id, conclusion, completed_at, status="completed"):
    """Gives one run of the required job `tests`, as check-runs or jobs spell it."""
    return {"id": run_id, "name": "tests", "status": status, "conclusion": conclusion,
            "completed_at": completed_at}


FAILED_FIRST = [a_run(1, "failure", "2026-10-01T10:00:00Z"),
                a_run(2, "success", "2026-10-01T10:05:00Z")]
GREEN_FIRST = [a_run(1, "success", "2026-10-01T10:00:00Z"),
               a_run(2, "failure", "2026-10-01T10:05:00Z")]


class TestNewestRun(unittest.TestCase):
    """A required check with several runs on one commit reads as its newest
    run, on the check-runs path and on the Actions jobs that stand in for it."""

    def setUp(self):
        patch = mock.patch.dict(os.environ, {"BENCH_GITHUB_TOKEN": "fake-token"})
        patch.start()
        self.addCleanup(patch.stop)

    def client(self, check_runs=None, jobs=None):
        """Gives a GitHub whose request answers from memory: the check runs of
        the commit, or 403 on them and `jobs` ({run id: its jobs}) in their
        place."""
        client = forge.GitHub("o", "r")

        def request(method, url, *args, **kwargs):
            path = urllib.parse.urlsplit(url).path
            if path.endswith("/check-runs"):
                if check_runs is None:
                    raise forge.ForgeError("github refused (403)", 403)
                return {"check_runs": check_runs}
            if path.endswith("/actions/runs"):
                return {"workflow_runs": [{"id": run} for run in jobs]}
            if path.endswith("/jobs"):
                return {"jobs": jobs[int(path.split("/")[-2])]}
            if path.endswith("/status"):
                return {"statuses": []}
            if path.endswith("/pulls/5"):
                return {"head": {"sha": "abc"}, "mergeable": True}
            raise AssertionError("no answer for %s %s" % (method, url))

        client.request = request
        return client

    def test_a_failed_run_then_a_green_one_reads_green(self):
        for runs in (FAILED_FIRST, FAILED_FIRST[::-1]):
            self.assertEqual(self.client(runs).check_states("abc"), {"tests": "success"})

    def test_a_green_run_then_a_failed_one_reads_red(self):
        for runs in (GREEN_FIRST, GREEN_FIRST[::-1]):
            self.assertEqual(self.client(runs).check_states("abc"), {"tests": "failure"})

    def test_the_jobs_that_stand_in_read_the_newest_run_too(self):
        failed, green = FAILED_FIRST
        for jobs in ({1: [failed], 2: [green]}, {2: [green], 1: [failed]}):
            self.assertEqual(self.client(jobs=jobs).check_states("abc"), {"tests": "success"})
        green, failed = GREEN_FIRST
        for jobs in ({1: [green], 2: [failed]}, {2: [failed], 1: [green]}):
            self.assertEqual(self.client(jobs=jobs).check_states("abc"), {"tests": "failure"})

    def test_the_same_completed_at_takes_the_highest_id(self):
        runs = [a_run(2, "success", "2026-10-01T10:00:00Z"),
                a_run(1, "failure", "2026-10-01T10:00:00Z")]
        self.assertEqual(self.client(runs).check_states("abc"), {"tests": "success"})

    def test_a_run_that_has_not_completed_is_the_newest(self):
        runs = [a_run(2, None, None, status="in_progress"),
                a_run(1, "failure", "2026-10-01T10:00:00Z")]
        self.assertEqual(self.client(runs).check_states("abc"), {"tests": "pending"})

    def test_the_merge_reads_red_only_from_the_newest_run(self):
        answer = self.client(GREEN_FIRST).merge_when_green(5, "abc", ["tests"])
        self.assertEqual(answer, {"state": "red", "failed": ["tests"]})
        client = self.client(jobs={1: [FAILED_FIRST[0]], 2: [FAILED_FIRST[1]]})
        client.merge_pull_request = lambda number, head_sha, method: {"sha": "m"}
        answer = client.merge_when_green(5, "abc", ["tests"])
        self.assertEqual(answer, {"state": "merged", "sha": "m"})


GREEN_RUN = {"id": 31, "url": "https://github.com/o/r/actions/runs/31", "branch": "bench-test/work",
             "commit": "abc", "state": "completed", "result": "success",
             "created": "2026-10-07T10:00:00Z", "completed": "2026-10-07T10:03:00Z"}


class TestNoAnswer(unittest.TestCase):
    """A lookup that nothing answers is made again inside the wait of
    test_result; it is never the run's answer."""

    def setUp(self):
        self.now = [0.0]
        self.slept = []
        fakes = {"_clock": lambda: self.now[0], "_sleep": self.sleep, "_test_spec": lambda repo: {},
                 "_dirty_included": lambda *args: False, "_run_counts": lambda *args: None,
                 "_drop_scratch": lambda *args: True}
        for name, fake in fakes.items():
            patch = mock.patch.object(tools, name, fake)
            patch.start()
            self.addCleanup(patch.stop)

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now[0] += seconds

    def result(self, reads, **args):
        """Answers test_result for run 31; each read of the run takes the next of reads."""
        client = mock.Mock()
        client.pipeline.side_effect = reads
        repo = mock.Mock()
        repo.name = "demo"
        bench = mock.Mock()
        bench.repo.return_value = repo
        with mock.patch.object(tools.forge, "client", return_value=client):
            return tools.test_result(bench, dict(args, run_id=31))

    def test_the_one_http_call_names_a_network_error_no_answer(self):
        with mock.patch.dict(os.environ, {"no_proxy": "*", "NO_PROXY": "*"}):
            with self.assertRaises(forge.NoAnswer) as raised:
                forge.http("GET", "http://127.0.0.1:1/runs/31", {})
        self.assertIsNone(raised.exception.status)
        self.assertIn("no answer from 127.0.0.1:1", str(raised.exception))

    def test_one_failed_lookup_then_a_good_one_answers_the_run(self):
        missed = forge.NoAnswer("no answer from api.github.com: name resolution")
        answer = self.result([missed, GREEN_RUN])
        self.assertEqual(answer["conclusion"], "success")
        self.assertEqual(answer["run_id"], 31)
        self.assertEqual(answer["duration_s"], 180)
        self.assertNotIn("note", answer)
        self.assertEqual(self.slept, [5])

    def test_a_lookup_that_fails_every_time_answers_pending_with_the_note(self):
        missed = forge.NoAnswer("no answer from api.github.com: name resolution")
        answer = self.result(lambda run_id: (_ for _ in ()).throw(missed), wait_seconds=10)
        self.assertEqual(answer["conclusion"], "pending")
        self.assertEqual(answer["run_id"], 31)
        self.assertIn("no answer from api.github.com", answer["note"])
        self.assertIn("call test_result again", answer["note"])
        self.assertEqual(self.slept, [5, 5])

    def test_an_http_error_of_the_forge_is_still_a_refusal(self):
        with self.assertRaises(tools.Refusal):
            self.result([forge.ForgeError("github answered 500: no", 500)])
        self.assertEqual(self.slept, [])


if __name__ == "__main__":
    unittest.main()
