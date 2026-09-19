"""The tests of the two transports: HTTP and stdio."""

import io
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from bench import mcp
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

    def post(self, message):
        """Sends one JSON-RPC message. Gives (status, answer)."""
        request = urllib.request.Request(
            self.url, data=json.dumps(message).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"},
            method="POST")
        with urllib.request.urlopen(request, timeout=30) as answer:
            body = answer.read()
            return answer.status, (json.loads(body) if body else None)


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

    def test_tools_list_gives_the_twelve_tools_with_schemas(self):
        status, answer = self.post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(status, 200)
        tools = answer["result"]["tools"]
        self.assertEqual(len(tools), 12)
        names = [tool["name"] for tool in tools]
        self.assertEqual(sorted(names), sorted([
            "prepare", "status", "find", "read", "edit", "pull", "submit", "feedback", "discard",
            "enroll", "repos", "unenroll"]))
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
            if tool["name"] in ("enroll", "unenroll"):
                # The enrollment names a repository, and no branch.
                self.assertNotIn("branch", schema["properties"])
                continue
            self.assertIn("branch", schema["properties"])
            if tool["name"] in ("find", "read", "edit"):
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
        self.assertEqual(len(answers[1]["result"]["tools"]), 12)
        self.assertTrue(answers[2]["result"]["structuredContent"]["result"]["created"])


if __name__ == "__main__":
    unittest.main()
