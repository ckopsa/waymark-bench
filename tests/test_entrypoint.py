"""The tests of the image's entrypoint: the deps cache links to the volume."""

import os
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "entrypoint.sh")


class TestEntrypoint(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="bench-entrypoint-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = os.path.join(self.root, "root")
        self.data = os.path.join(self.root, "data")

    def run_entrypoint(self, *command):
        env = dict(os.environ, HOME=self.home, BENCH_CACHE_ROOT=self.data)
        return subprocess.run(["sh", SCRIPT, *command], env=env,
                              capture_output=True, text=True, check=True)

    def assert_linked(self):
        for name in ("m2", "gitlibs"):
            link = os.path.join(self.home, "." + name)
            self.assertTrue(os.path.islink(link), link)
            self.assertEqual(os.readlink(link), os.path.join(self.data, name))
            self.assertTrue(os.path.isdir(os.path.join(self.data, name)))

    def test_it_makes_both_links_and_runs_the_command(self):
        done = self.run_entrypoint("echo", "ran")
        self.assertEqual(done.stdout.strip(), "ran")
        self.assert_linked()

    def test_a_second_run_changes_nothing_and_keeps_the_cache(self):
        self.run_entrypoint("true")
        kept = os.path.join(self.data, "m2", "kept.jar")
        with open(kept, "w") as f:
            f.write("jar")
        self.run_entrypoint("true")
        self.assert_linked()
        self.assertTrue(os.path.exists(os.path.join(self.home, ".m2", "kept.jar")))
        self.assertEqual(os.listdir(os.path.join(self.data, "m2")), ["kept.jar"])

    def test_a_real_directory_in_the_links_place_gives_way(self):
        os.makedirs(os.path.join(self.home, ".m2", "repository"))
        self.run_entrypoint("true")
        self.assert_linked()


if __name__ == "__main__":
    unittest.main()
