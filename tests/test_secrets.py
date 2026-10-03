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
        self.tokens = []
        self.main_reads = False
        self.put_status = 204
        original = forge.http
        forge.http = self.fake_http
        self.addCleanup(setattr, forge, "http", original)
        os.environ["BENCH_GITHUB_TOKEN"] = "fake-token"
        self.addCleanup(os.environ.pop, "BENCH_GITHUB_TOKEN", None)
        os.environ["BENCH_SECRETS_TOKEN"] = "secrets-token"
        self.addCleanup(os.environ.pop, "BENCH_SECRETS_TOKEN", None)

    def fake_http(self, method, url, headers, body=None):
        path = url.split("/repos/o/r", 1)[-1]
        self.calls.append((method, path, body))
        self.tokens.append(headers.get("Authorization"))
        if path == "":
            return 200, "{}"
        # The Secrets API answers the secrets token; the main one only when a test says so.
        if (path.startswith("/actions/secrets") and not self.main_reads
                and headers.get("Authorization") != "Bearer secrets-token"):
            return 403, '{"message":"Resource not accessible by personal access token"}'
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


class TestSecretsToken(SecretCase):
    """secret_set and secret_list use BENCH_SECRETS_TOKEN; all else uses the main token."""

    def setUp(self):
        super().setUp()
        original = forge.token_scopes
        forge.token_scopes = lambda url, headers: None
        self.addCleanup(setattr, forge, "token_scopes", original)

    def check(self):
        found = tools.check_credential(self.bench, self.bench.config.repos["demo"])
        self.assertNotIn("secrets-token", json.dumps(found))
        self.assertNotIn("fake-token", json.dumps(found))
        return found

    def test_set_and_list_use_the_secrets_token(self):
        self.assertFalse(self.set_secret()[1])
        self.assertFalse(self.call("secret_list")[1])
        self.assertEqual(len(self.tokens), 4)
        self.assertEqual(set(self.tokens), {"Bearer secrets-token"})

    def test_an_unset_secrets_token_refuses_by_name(self):
        os.environ.pop("BENCH_SECRETS_TOKEN")
        for answer, refused in (self.set_secret(), self.call("secret_list")):
            self.assertTrue(refused)
            self.assertEqual(answer["refused"], "no_secrets_token")
        self.assertEqual(self.calls, [])

    def test_every_other_call_uses_the_main_token(self):
        client = forge.GitHub("o", "r")
        for path in ("", "/pulls/1", "/actions/runs/1", "/commits/main/status"):
            try:
                client.request("GET", client.url(path))
            except forge.ForgeError:
                pass
        self.assertEqual(len(self.tokens), 4)
        self.assertEqual(set(self.tokens), {"Bearer fake-token"})
        # Without the main token nothing falls back to the secrets token.
        os.environ.pop("BENCH_GITHUB_TOKEN")
        with self.assertRaises(forge.ForgeError):
            client.request("GET", client.url(""))
        self.assertEqual(len(self.tokens), 4)

    def test_the_check_sends_the_secrets_token_to_the_secrets_api_only(self):
        self.check()
        sent = [path for (_, path, _), token in zip(self.calls, self.tokens)
                if token == "Bearer secrets-token"]
        self.assertEqual(sent, ["/actions/secrets?per_page=1"])

    def test_the_check_reports_the_secrets_token_apart_from_the_main_one(self):
        found = self.check()
        self.assertEqual(found["token"], "fine_grained")
        self.assertEqual(found["secrets_token"], {"set": True, "ok": True})
        self.assertEqual(found["warnings"], [])

    def test_the_check_warns_when_the_main_token_reads_secrets(self):
        self.main_reads = True
        self.assertEqual(self.check()["warnings"], ["main_token_reads_secrets"])

    def test_the_check_reports_an_unset_secrets_token(self):
        os.environ.pop("BENCH_SECRETS_TOKEN")
        self.assertEqual(self.check()["secrets_token"], {"set": False, "ok": None})


if __name__ == "__main__":
    unittest.main()
