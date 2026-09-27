"""The tests of the forge's one HTTP call: a redirect to another host goes
without the credential.

Two small servers on the loopback stand in for the API and for the log
store that GitHub redirects the log of a job to.
"""

import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from bench import forge


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


if __name__ == "__main__":
    unittest.main()
