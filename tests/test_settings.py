"""The tests of the settings: the prefix, the .env file, the secrets."""

import os
import shutil
import tempfile
import unittest

from bench import forge, git, settings


class TestSettings(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="bench-settings-")
        self.addCleanup(shutil.rmtree, self.root, True)
        for name in ("BENCH_GIT_TOKEN", "BENCH_BITBUCKET_USER", "BENCH_BITBUCKET_TOKEN",
                     "BENCH_GITHUB_TOKEN", "BENCH_ENV_FILE", "BENCH_HOST"):
            self.addCleanup(os.environ.pop, name, None)
        os.environ["BENCH_ENV_FILE"] = os.path.join(self.root, "none.env")

    def test_the_environment_gives_the_settings_and_the_secrets_never_print(self):
        os.environ["BENCH_GIT_TOKEN"] = "git-secret"
        os.environ["BENCH_BITBUCKET_USER"] = "me@example.com"
        os.environ["BENCH_BITBUCKET_TOKEN"] = "bb-secret"
        os.environ["BENCH_HOST"] = "0.0.0.0"
        current = settings.load()
        self.assertEqual(current.host, "0.0.0.0")
        self.assertEqual(current.bitbucket_user, "me@example.com")
        self.assertEqual(current.secret("git_token"), "git-secret")
        self.assertEqual(sorted(current.secrets()), ["bb-secret", "git-secret"])
        self.assertIsNone(current.secret("github_token"))
        shown = repr(current) + str(current) + current.model_dump_json()
        self.assertNotIn("git-secret", shown)
        self.assertNotIn("bb-secret", shown)
        self.assertEqual(settings.scrub("push https://x:git-secret@host bb-secret"),
                         "push https://x:***@host ***")
        self.assertEqual(git.scrub("git-secret"), "***")
        self.assertEqual(forge.scrub("bb-secret"), "***")

    def test_a_dot_env_file_gives_the_settings(self):
        path = os.path.join(self.root, "rig.env")
        with open(path, "w") as handle:
            handle.write("BENCH_GITHUB_TOKEN=gh-secret\nBENCH_URL=http://rig.local:9/mcp/\nOTHER=x\n")
        os.environ["BENCH_ENV_FILE"] = path
        current = settings.load()
        self.assertEqual(current.secret("github_token"), "gh-secret")
        self.assertEqual(current.url, "http://rig.local:9/mcp/")
        self.assertEqual(forge.GitHub("o", "r").credential(), "gh-secret")

    def test_git_carries_the_token_to_the_helper_only_when_it_is_set(self):
        self.assertNotIn("credential.helper=", git.config_args(None))
        self.assertNotIn("BENCH_GIT_TOKEN", git.environment(None))
        args = git.config_args("t")
        self.assertIn("credential.helper=", args)
        self.assertIn("credential.helper=" + git.CREDENTIAL_HELPER, args)
        self.assertEqual(git.environment("t")["BENCH_GIT_TOKEN"], "t")

    def test_the_forge_credentials_come_from_the_settings(self):
        with self.assertRaises(forge.ForgeError):
            forge.GitHub("o", "r").credential()
        os.environ["BENCH_GIT_TOKEN"] = "git-secret"
        self.assertEqual(forge.GitHub("o", "r").credential(), "git-secret")
        os.environ["BENCH_GITHUB_TOKEN"] = "gh-secret"
        self.assertEqual(forge.GitHub("o", "r").credential(), "gh-secret")
        os.environ["BENCH_BITBUCKET_USER"] = "me"
        os.environ["BENCH_BITBUCKET_TOKEN"] = "bb-secret"
        self.assertEqual(forge.Bitbucket("w", "s").credential(), ("me", "bb-secret"))
        headers = forge.Bitbucket("w", "s").headers()
        self.assertTrue(headers["Authorization"].startswith("Basic "))
