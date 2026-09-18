"""One test for each tool, and one test for each refusal."""

import os
import shutil
import tempfile
import unittest

from bench import tools
from bench.tools import Bench

from . import util


class BenchCase(unittest.TestCase):
    """A real git origin, a real data directory, no network."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="bench-test-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.clone_url = util.make_origin(self.root)
        self.config = util.make_config(self.root, self.clone_url)
        self.bench = Bench(self.config)

    # -------------------------------------------------------------- tools

    def call(self, name, **args):
        """Calls one tool. Gives (answer, refused)."""
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

    def worktree(self, branch="work"):
        return self.bench.wt_dir("demo", branch)

    def prepared(self, branch="work"):
        self.ok("prepare", branch=branch)
        return self.worktree(branch)


class TestPrepare(BenchCase):

    def test_prepare_makes_the_worktree_and_is_idempotent(self):
        first = self.ok("prepare", branch="work")
        self.assertTrue(first["created"])
        self.assertEqual(first["base"], "main")
        self.assertEqual(first["dirty"], 0)
        self.assertEqual(first["head"], first["base_head"])
        self.assertTrue(os.path.isfile(os.path.join(self.worktree(), "README.md")))
        second = self.ok("prepare", branch="work")
        self.assertFalse(second["created"])
        self.assertEqual(second["head"], first["head"])

    def test_prepare_refuses_a_bad_branch_name(self):
        answer = self.refused("prepare", branch="../escape")
        self.assertEqual(answer["refused"], "branch")


class TestStatus(BenchCase):

    def test_status_gives_the_changed_paths(self):
        path = self.prepared()
        clean = self.ok("status", branch="work")
        self.assertEqual(clean["dirty"], 0)
        self.assertEqual(clean["ahead"], 0)
        self.assertEqual(clean["behind"], 0)
        util.write(os.path.join(path, "docs/a.txt"), "alpha\ndelta\n")
        dirty = self.ok("status", branch="work")
        self.assertEqual(dirty["dirty"], 1)
        self.assertEqual(dirty["paths"], ["docs/a.txt"])


class TestFind(BenchCase):

    def test_find_tree_gives_paths_with_sizes(self):
        self.prepared()
        answer = self.ok("find", branch="work", mode="tree", depth=2)
        paths = [entry["path"] for entry in answer["entries"]]
        self.assertIn("README.md", paths)
        self.assertIn("src/app.py", paths)
        self.assertNotIn("keys/server.pem", paths)
        for entry in answer["entries"]:
            if entry["path"] == "README.md":
                self.assertGreater(entry["size"], 0)

    def test_find_glob_gives_the_paths_that_match(self):
        self.prepared()
        answer = self.ok("find", branch="work", mode="glob", pattern="*.py")
        self.assertEqual(answer["paths"], ["src/app.py"])

    def test_find_grep_gives_counts_then_lines(self):
        self.prepared()
        answer = self.ok("find", branch="work", mode="grep", pattern="TODO")
        self.assertEqual(answer["files"], [{"path": "src/app.py", "count": 2}])
        self.assertEqual(len(answer["lines"]), 2)
        self.assertEqual(answer["lines"][0]["path"], "src/app.py")
        self.assertIn("TODO", answer["lines"][0]["text"])

    def test_find_grep_caps_the_lines_and_gives_dropped(self):
        path = self.prepared()
        util.write(os.path.join(path, "many.txt"), "needle\n" * 50)
        answer = self.ok("find", branch="work", mode="grep", pattern="needle", max_matches=5)
        self.assertEqual(len(answer["lines"]), 5)
        self.assertGreater(answer["dropped"], 0)

    def test_find_diff_gives_the_change_against_base(self):
        path = self.prepared()
        util.write(os.path.join(path, "docs/a.txt"), "alpha\ndelta\ncharlie\n")
        util.write(os.path.join(path, "new.txt"), "a new file\n")
        answer = self.ok("find", branch="work", mode="diff")
        self.assertIn("docs/a.txt", answer["diff"])
        self.assertIn("+delta", answer["diff"])
        self.assertIn("new.txt", answer["diff"])
        self.assertEqual(answer["files"], 2)


class TestRead(BenchCase):

    def test_read_gives_lines_with_numbers(self):
        self.prepared()
        answer = self.ok("read", branch="work", path="docs/a.txt")
        self.assertEqual([item["line"] for item in answer["lines"]], [1, 2, 3])
        self.assertEqual(answer["lines"][1]["text"], "bravo")
        self.assertEqual(answer["total_lines"], 3)
        self.assertTrue(answer["eof"])
        self.assertTrue(answer["hash"])

    def test_read_gives_a_range_and_eof_false(self):
        self.prepared()
        answer = self.ok("read", branch="work", path="docs/a.txt", offset=2, limit=1)
        self.assertEqual(answer["lines"], [{"line": 2, "text": "bravo"}])
        self.assertFalse(answer["eof"])

    def test_read_of_a_ref_gives_the_base_file(self):
        path = self.prepared()
        util.write(os.path.join(path, "docs/a.txt"), "changed\n")
        answer = self.ok("read", branch="work", path="docs/a.txt", ref="base")
        self.assertEqual(answer["lines"][0]["text"], "alpha")

    def test_read_with_if_hash_answers_unchanged(self):
        self.prepared()
        first = self.ok("read", branch="work", path="docs/a.txt")
        again = self.ok("read", branch="work", path="docs/a.txt", if_hash=first["hash"])
        self.assertTrue(again["unchanged"])
        self.assertEqual(again["hash"], first["hash"])
        self.assertNotIn("lines", again)

    def test_read_refuses_a_denied_path(self):
        self.prepared()
        answer = self.refused("read", branch="work", path="keys/server.pem")
        self.assertEqual(answer["refused"], "denied")
        self.assertEqual(answer["pattern"], "*.pem")

    def test_read_refuses_a_path_outside_the_worktree(self):
        self.prepared()
        answer = self.refused("read", branch="work", path="../../etc/passwd")
        self.assertEqual(answer["refused"], "denied")
        self.assertIn("outside", answer["reason"])

    def test_read_refuses_a_missing_file(self):
        self.prepared()
        answer = self.refused("read", branch="work", path="no/such.txt")
        self.assertEqual(answer["refused"], "not_found")


class TestEdit(BenchCase):

    def test_edit_replaces_one_text(self):
        path = self.prepared()
        answer = self.ok("edit", branch="work", path="src/app.py",
                         old="MARKER_ONE = 'one'", new="MARKER_ONE = 'two'")
        self.assertTrue(answer["hash"])
        with open(os.path.join(path, "src/app.py"), encoding="utf-8") as handle:
            self.assertIn("MARKER_ONE = 'two'", handle.read())

    def test_edit_creates_moves_and_deletes(self):
        path = self.prepared()
        self.ok("edit", branch="work", path="docs/b.txt", create=True, new="bravo\n")
        self.assertTrue(os.path.isfile(os.path.join(path, "docs/b.txt")))
        moved = self.ok("edit", branch="work", path="docs/b.txt", move_to="docs/c.txt")
        self.assertEqual(moved["path"], "docs/c.txt")
        self.assertFalse(os.path.exists(os.path.join(path, "docs/b.txt")))
        gone = self.ok("edit", branch="work", path="docs/c.txt", delete=True)
        self.assertTrue(gone["deleted"])
        self.assertFalse(os.path.exists(os.path.join(path, "docs/c.txt")))

    def test_edit_refuses_an_old_text_that_is_not_unique(self):
        self.prepared()
        answer = self.refused("edit", branch="work", path="src/app.py",
                              old="    # TODO", new="    # DONE")
        self.assertEqual(answer["refused"], "found")
        self.assertEqual(answer["found"], 2)

    def test_edit_refuses_a_protected_path(self):
        self.prepared()
        answer = self.refused("edit", branch="work", path=".github/workflows/ci.yml",
                              old="name: ci", new="name: gate")
        self.assertEqual(answer["refused"], "protected")
        permitted = self.ok("edit", branch="work", path=".github/workflows/ci.yml",
                            old="name: ci", new="name: gate", allow_protected=True)
        self.assertTrue(permitted["hash"])

    def test_edit_refuses_a_denied_path(self):
        self.prepared()
        answer = self.refused("edit", branch="work", path="keys/server.pem",
                              create=True, new="x")
        self.assertEqual(answer["refused"], "denied")

    def test_edit_refuses_more_than_one_operation(self):
        self.prepared()
        answer = self.refused("edit", branch="work", path="docs/a.txt",
                              delete=True, move_to="docs/z.txt")
        self.assertEqual(answer["refused"], "operation")


class TestPull(BenchCase):

    def test_pull_from_base_merges_the_base_branch(self):
        self.prepared()
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "main", "docs/new.txt", "from the other person\n")
        answer = self.ok("pull", branch="work", **{"from": "base"})
        self.assertTrue(answer["merged"])
        self.assertEqual(answer["conflicts"], [])
        self.assertTrue(os.path.isfile(os.path.join(self.worktree(), "docs/new.txt")))

    def test_pull_from_base_leaves_the_markers_on_a_conflict(self):
        path = self.prepared()
        util.write(os.path.join(path, "docs/a.txt"), "alpha\nours\ncharlie\n")
        self.ok("submit", branch="work", message="our line", trailers=["Seat: test"])
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "main", "docs/a.txt", "alpha\ntheirs\ncharlie\n")
        answer = self.ok("pull", branch="work", **{"from": "base"})
        self.assertFalse(answer["merged"])
        self.assertEqual(answer["conflicts"], ["docs/a.txt"])
        with open(os.path.join(path, "docs/a.txt"), encoding="utf-8") as handle:
            self.assertIn("<<<<<<<", handle.read())

    def test_pull_from_head_moves_to_the_remote_branch(self):
        self.prepared()
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "work", "docs/head.txt", "from the remote branch\n")
        answer = self.ok("pull", branch="work", **{"from": "head"})
        self.assertTrue(answer["merged"])
        self.assertTrue(os.path.isfile(os.path.join(self.worktree(), "docs/head.txt")))


class TestSubmit(BenchCase):

    def test_submit_commits_with_trailers_and_pushes(self):
        path = self.prepared()
        util.write(os.path.join(path, "docs/a.txt"), "alpha\ndelta\ncharlie\n")
        answer = self.ok("submit", branch="work", message="change one line",
                         trailers=["Seat: clerk-1", "Sitting: 42"])
        self.assertTrue(answer["pushed"])
        self.assertEqual(answer["files"], 1)
        self.assertEqual(answer["lines_added"], 1)
        self.assertEqual(answer["lines_removed"], 1)
        message = util.git(["log", "-1", "--format=%B", answer["commit"]], cwd=path)
        self.assertIn("change one line", message)
        self.assertIn("Seat: clerk-1", message)
        self.assertIn("Sitting: 42", message)
        remote = util.git(["ls-remote", self.clone_url, "refs/heads/work"], cwd=path)
        self.assertIn(answer["commit"], remote)
        self.assertEqual(self.ok("status", branch="work")["dirty"], 0)

    def test_submit_refuses_a_clean_worktree(self):
        self.prepared()
        answer = self.refused("submit", branch="work", message="nothing here")
        self.assertEqual(answer["refused"], "nothing_to_commit")

    def test_submit_refuses_the_default_branch(self):
        answer = self.refused("submit", branch="main", message="on the default branch")
        self.assertEqual(answer["refused"], "default_branch")
        self.assertEqual(answer["default_branch"], "main")

    def test_submit_refuses_a_change_over_the_ceiling(self):
        path = self.prepared()
        util.write(os.path.join(path, "big.txt"), "line\n" * 40)
        answer = self.refused("submit", branch="work", message="a large change", max_lines=10)
        self.assertEqual(answer["refused"], "over_ceiling")
        self.assertEqual(answer["lines"], 40)
        self.assertEqual(answer["max_lines"], 10)

    def test_submit_refuses_a_push_that_does_not_land(self):
        path = self.prepared()
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "work", "docs/other.txt", "another person was first\n")
        util.write(os.path.join(path, "docs/a.txt"), "alpha\nours\ncharlie\n")
        answer = self.refused("submit", branch="work", message="our change")
        self.assertEqual(answer["refused"], "push_rejected")
        self.assertIn("pull", answer["remedy"])


class TestDiscard(BenchCase):

    def test_discard_puts_the_worktree_back(self):
        path = self.prepared()
        util.write(os.path.join(path, "docs/a.txt"), "alpha\nchanged\n")
        util.write(os.path.join(path, "extra.txt"), "an extra file\n")
        answer = self.ok("discard", branch="work")
        self.assertEqual(answer["dirty"], 0)
        self.assertFalse(os.path.exists(os.path.join(path, "extra.txt")))
        with open(os.path.join(path, "docs/a.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "alpha\nbravo\ncharlie\n")

    def test_discard_with_drop_branch_removes_the_worktree(self):
        path = self.prepared()
        answer = self.ok("discard", branch="work", drop_branch=True)
        self.assertTrue(answer["dropped"])
        self.assertFalse(os.path.isdir(path))
        missing = self.refused("status", branch="work")
        self.assertEqual(missing["refused"], "no_worktree")


class TestInput(BenchCase):

    def test_an_unknown_repository_is_refused(self):
        answer, refused = tools.call(self.bench, "status", {"repo": "ghost", "branch": "work"})
        self.assertTrue(refused)
        self.assertEqual(answer["refused"], "repo")

    def test_an_unknown_tool_is_refused(self):
        answer, refused = tools.call(self.bench, "explode", {})
        self.assertTrue(refused)
        self.assertEqual(answer["refused"], "unknown_tool")


if __name__ == "__main__":
    unittest.main()
