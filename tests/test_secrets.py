"""The tests of secret_set and secret_list: the seal, the PUT, the list,
and the value absent from every answer and log line.

The forge is a fake: the tests replace forge.http.
"""

import base64
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr

from nacl import public

from bench import config as config_module, forge, tools
from bench.tools import Bench

from . import util


VALUE = "s3cr3t-oauth-value"


class SecretCase(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="bench-secret-")
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
        self.private = public.PrivateKey.generate()
        self.key = base64.b64encode(bytes(self.private.public_key)).decode("ascii")
        self.calls = []
        self.put_status = 204
        original = forge.http
        forge.http = self.fake_http
        self.addCleanup(setattr, forge, "http", original)
        os.environ["BENCH_GITHUB_TOKEN"] = "fake-token"
        self.addCleanup(os.environ.pop, "BENCH_GITHUB_TOKEN", None)

    def fake_http(self, method, url, headers, body=None):
        path = url.split("/repos/o/r", 1)[-1]
        self.calls.append((method, path, body))
        if path == "/actions/secrets/public-key":
            return 200, json.dumps({"key_id": "k1", "key": self.key})
        if method == "PUT":
            # A forge that echoes what it was given: the refusal must still hide it.
            return self.put_status, "" if self.put_status < 400 else '{"message":"bad %s"}' % VALUE
        if path.startswith("/actions/secrets/"):
            return 200, json.dumps({"name": "TS_OAUTH_SECRET", "updated_at": "2026-10-03T00:00:00Z"})
        if path.startswith("/actions/secrets"):
            return 200, json.dumps({"total_count": 1, "secrets": [
                {"name": "TS_OAUTH_SECRET", "created_at": "2026-10-01T00:00:00Z",
                 "updated_at": "2026-10-03T00:00:00Z"}]})
        return 404, "{}"

    def call(self, tool, /, **args):
        args.setdefault("repo", "demo")
        err = io.StringIO()
        with redirect_stderr(err):
            answer, refused = tools.call(self.bench, tool, args)
        self.assertNotIn(VALUE, err.getvalue())
        self.assertNotIn(VALUE, json.dumps(answer))
        return answer, refused

    def set_secret(self, **args):
        return self.call("secret_set", name="TS_OAUTH_SECRET", value=VALUE,
                         why="home-infrastructure needs it", **args)


class TestSeal(SecretCase):

    def test_a_sealed_value_opens_with_the_private_key(self):
        sealed = forge.seal(self.key, VALUE)
        self.assertNotIn(VALUE, sealed)
        opened = public.SealedBox(self.private).decrypt(base64.b64decode(sealed))
        self.assertEqual(opened.decode("utf-8"), VALUE)


class TestSecretSet(SecretCase):

    def test_set_puts_the_sealed_value_and_answers_no_value(self):
        answer, refused = self.set_secret()
        self.assertFalse(refused, answer)
        self.assertEqual(answer, {"repo": "demo", "name": "TS_OAUTH_SECRET",
                                  "updated_at": "2026-10-03T00:00:00Z"})
        puts = [c for c in self.calls if c[0] == "PUT"]
        self.assertEqual(len(puts), 1)
        method, path, body = puts[0]
        self.assertEqual(path, "/actions/secrets/TS_OAUTH_SECRET")
        self.assertEqual(sorted(body), ["encrypted_value", "key_id"])
        self.assertEqual(body["key_id"], "k1")
        opened = public.SealedBox(self.private).decrypt(base64.b64decode(body["encrypted_value"]))
        self.assertEqual(opened.decode("utf-8"), VALUE)

    def test_an_http_error_hides_the_value(self):
        self.put_status = 422
        answer, refused = self.set_secret()
        self.assertTrue(refused)
        self.assertEqual(answer["refused"], "forge")

    def test_a_repo_not_enrolled_is_refused(self):
        answer, refused = self.set_secret(repo="other")
        self.assertTrue(refused)
        self.assertEqual(answer["refused"], "repo")
        self.assertEqual(self.calls, [])

    def test_a_bad_name_is_refused(self):
        answer, refused = self.call("secret_set", name="GITHUB_TOKEN", value=VALUE, why="no")
        self.assertTrue(refused)
        self.assertEqual(answer["field"], "name")
        self.assertEqual(self.calls, [])

    def test_the_value_is_a_secret_reference(self):
        schema = tools.TOOLS["secret_set"]["schema"]
        self.assertIs(schema["properties"]["value"]["x-secret-ref"], True)


class TestSecretList(SecretCase):

    def test_list_answers_names_and_updated_at_only(self):
        answer, refused = self.call("secret_list")
        self.assertFalse(refused, answer)
        self.assertEqual(answer["secrets"], [{"name": "TS_OAUTH_SECRET",
                                              "updated_at": "2026-10-03T00:00:00Z"}])


if __name__ == "__main__":
    unittest.main()
