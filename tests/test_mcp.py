"""The tests of the two transports: HTTP and stdio."""

import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from bench import __main__ as entry
from bench import git, mcp
from bench.tools import Bench

from . import util


class TransportCase(unittest.TestCase):
    """One HTTP server on a free port, over a real origin."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="bench-mcp-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.clone_url = util.make_origin(self.root)
        self.bench = Bench(util.make_config(self.root, self.clone_url))
        self.httpd = mcp.serve_http(self.bench, 0)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%s/mcp/" % self.httpd.server_address[1]

    def post(self, message, headers=None):
        """Sends one JSON-RPC message, with more headers when given. Gives (status, answer)."""
        request = urllib.request.Request(
            self.url, data=json.dumps(message).encode("utf-8"),
            headers=dict({"Content-Type": "application/json",
                          "Accept": "application/json, text/event-stream"}, **(headers or {})),
            method="POST")
        with urllib.request.urlopen(request, timeout=30) as answer:
            body = answer.read()
            return answer.status, (json.loads(body) if body else None)


class TestSecretHeader(TransportCase):
    """The engine's X-Secret-Ref header: the call's git token when the environment has none."""

    def setUp(self):
        TransportCase.setUp(self)
        patcher = mock.patch.dict(os.environ, {"BENCH_ENV_FILE": os.path.join(self.root, "none.env")})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("BENCH_GIT_TOKEN", None)

    def git_in_a_call(self, headers=None):
        """Runs one git command in one HTTP call. Gives [the token git got, its scrubbed output]."""
        seen = []

        def run(command, **kwargs):
            seen.append(kwargs["env"].get("BENCH_GIT_TOKEN"))
            return subprocess.CompletedProcess(command, 0, "out %s" % seen[-1], "")

        def call(bench, name, args):
            with mock.patch.object(git.subprocess, "run", run):
                seen.append(git.run(["status"])[1])
            return {"ok": True}, None

        with mock.patch.object(mcp.tools, "call", call):
            self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "status", "arguments": {}}}, headers)
        return seen

    def test_the_header_gives_git_its_token_when_the_environment_has_none(self):
        self.assertEqual(self.git_in_a_call({"X-Secret-Ref": "header-secret"}),
                         ["header-secret", "out ***"])

    def test_without_the_header_or_the_environment_git_has_no_token(self):
        self.assertEqual(self.git_in_a_call(), [None, "out None"])

    def test_the_environment_token_wins_over_the_header(self):
        os.environ["BENCH_GIT_TOKEN"] = "env-secret"
        self.assertEqual(self.git_in_a_call({"X-Secret-Ref": "header-secret"}),
                         ["env-secret", "out ***"])

    def test_the_header_value_is_in_no_log_line_and_no_answer(self):
        captured = io.StringIO()
        with mock.patch("sys.stderr", captured):
            status, answer = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": "repos", "arguments": {}}},
                                       {"X-Secret-Ref": "header-secret"})
        self.assertEqual(status, 200)
        self.assertIn("tool=repos", captured.getvalue())
        self.assertNotIn("header-secret", captured.getvalue())
        self.assertNotIn("header-secret", json.dumps(answer))


class TestHttp(TransportCase):

    def test_initialize_and_the_notification(self):
        status, answer = self.post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                    "params": {"protocolVersion": mcp.PROTOCOL_VERSION,
                                               "capabilities": {},
                                               "clientInfo": {"name": "test", "version": "1"}}})
        self.assertEqual(status, 200)
        self.assertEqual(answer["id"], 1)
        self.assertEqual(answer["result"]["serverInfo"]["name"], "bench")
        self.assertIn("tools", answer["result"]["capabilities"])
        status, body = self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(status, 202)
        self.assertIsNone(body)

    def test_tools_list_gives_the_thirty_one_tools_with_schemas(self):
        status, answer = self.post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(status, 200)
        tools = answer["result"]["tools"]
        self.assertEqual(len(tools), 36)
        hints = {tool["name"]: tool.get("annotations", {}).get("readOnlyHint") for tool in tools}
        self.assertTrue(hints["status"])
        self.assertTrue(hints["read"])
        self.assertIsNone(hints["submit"])
        self.assertIsNone(hints["prepare"])
        self.assertEqual(mcp.READ_ONLY - set(hints), set())
        names = [tool["name"] for tool in tools]
        self.assertEqual(sorted(names), sorted([
            "prepare", "status", "diff", "find", "read", "symbols", "read_symbol", "history", "check", "edit",
            "edit_many", "pull", "conflicts", "submit", "feedback", "log", "discard", "merge", "update_branch",
            "compare_heads", "rerun", "test", "test_result", "enroll", "repos", "unenroll", "train_build", "train_checks",
            "train_open", "train_status", "train_land", "train_delete", "secret_set",
            "secret_list", "dispatch", "run_status"]))
        for tool in tools:
            self.assertTrue(tool["description"])
            schema = tool["inputSchema"]
            self.assertEqual(schema["type"], "object")
            self.assertIn("seat", schema["properties"])
            self.assertIn("sitting", schema["properties"])
            if tool["name"] == "repos":
                # The tool takes the marks only.
                self.assertNotIn("repo", schema["properties"])
                continue
            self.assertIn("repo", schema["properties"])
            self.assertIn("repo", schema["required"])
            if tool["name"] in ("enroll", "unenroll", "secret_set", "secret_list",
                                "dispatch", "run_status"):
                # The enrollment, the secrets and a workflow run name a repository, and no branch.
                self.assertNotIn("branch", schema["properties"])
                continue
            if tool["name"] == "update_branch":
                # The tool names a pull request by its number, as merge does.
                self.assertIn("number", schema["required"])
                continue
            if tool["name"] == "compare_heads":
                # The tool names commits and a base branch, not a work branch.
                self.assertIn("head", schema["required"])
                continue
            self.assertIn("branch", schema["properties"])
            if tool["name"] in ("find", "read", "symbols", "read_symbol", "history", "edit", "edit_many"):
                self.assertIn("allow", schema["properties"])

    def test_tools_call_gives_text_and_structured_content(self):
        status, answer = self.post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                    "params": {"name": "prepare",
                                               "arguments": {"repo": "demo", "branch": "work"}}})
        self.assertEqual(status, 200)
        result = answer["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["content"][0]["type"], "text")
        text = json.loads(result["content"][0]["text"])
        self.assertEqual(text, result["structuredContent"]["result"])
        self.assertTrue(text["created"])
        self.assertEqual(text["branch"], "work")

    def test_a_refusal_is_an_answer_with_is_error(self):
        status, answer = self.post({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                    "params": {"name": "submit",
                                               "arguments": {"repo": "demo", "branch": "main",
                                                             "message": "no"}}})
        self.assertEqual(status, 200)
        result = answer["result"]
        self.assertTrue(result["isError"])
        self.assertNotIn("error", answer)
        self.assertEqual(result["structuredContent"]["result"]["refused"], "default_branch")

    def test_an_unknown_method_gives_a_json_rpc_error(self):
        status, answer = self.post({"jsonrpc": "2.0", "id": 5, "method": "tools/explode"})
        self.assertEqual(status, 200)
        self.assertEqual(answer["error"]["code"], -32601)


class TestStdio(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="bench-stdio-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.bench = Bench(util.make_config(self.root, util.make_origin(self.root)))

    def test_stdio_answers_tools_list_and_a_call(self):
        lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "prepare",
                                   "arguments": {"repo": "demo", "branch": "work"}}}),
        ]
        output = io.StringIO()
        mcp.serve_stdio(self.bench, io.StringIO("\n".join(lines) + "\n"), output)
        answers = [json.loads(row) for row in output.getvalue().splitlines()]
        self.assertEqual([answer["id"] for answer in answers], [1, 2, 3])
        self.assertEqual(len(answers[1]["result"]["tools"]), 36)
        self.assertTrue(answers[2]["result"]["structuredContent"]["result"]["created"])


class TestHost(TransportCase):
    """--host and BENCH_HOST: the address the HTTP transport binds."""

    def bound(self, argv, env):
        clean = {key: value for key, value in os.environ.items() if key != "BENCH_HOST"}
        clean["BENCH_ENV_FILE"] = os.path.join(self.root, "none.env")
        clean.update(env)
        with mock.patch.dict(os.environ, clean, clear=True):
            args = entry.parse_args(["--http", "0"] + argv)
        httpd = mcp.serve_http(self.bench, args.http, host=args.host)
        self.addCleanup(httpd.server_close)
        return httpd.server_address[0]

    def test_host_loopback_binds_loopback(self):
        self.assertEqual(self.bound(["--host", "127.0.0.1"], {"BENCH_HOST": "0.0.0.0"}), "127.0.0.1")

    def test_no_flag_binds_what_it_binds_today(self):
        self.assertEqual(self.bound([], {}), "127.0.0.1")
        self.assertEqual(self.bound([], {"BENCH_HOST": "0.0.0.0"}), "0.0.0.0")


if __name__ == "__main__":
    unittest.main()
