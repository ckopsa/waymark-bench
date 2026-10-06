"""One test for each tool, and one test for each refusal."""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from bench import config as config_module, tools
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
        # a submit answers at once by default; these tests read the
        # finished landing, so they ask for the wait the old default gave
        if name == "submit":
            args.setdefault("wait", 600)
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
        self.assertEqual(first["behind"], 0)
        self.assertIsNone(first["behind_remote"])
        self.assertEqual(first["note"], "")
        self.assertTrue(os.path.isfile(os.path.join(self.worktree(), "README.md")))
        second = self.ok("prepare", branch="work")
        self.assertFalse(second["created"])
        self.assertEqual(second["head"], first["head"])

    def test_prepare_runs_the_setup_step_until_it_succeeds_once(self):
        self.assertIsNone(self.ok("prepare", branch="work")["setup"], "no setup, no step")
        repo = self.config.repo("demo")
        repo.setup = config_module.setup_from_dict(
            "demo", {"command": "echo run >> setup.log; test -f ready"})
        failed = self.ok("prepare", branch="fresh")["setup"]
        self.assertTrue(failed["ran"])
        self.assertFalse(failed["ok"], "a failed setup is reported, not raised")
        util.write(os.path.join(self.worktree("fresh"), "ready"), "")
        passed = self.ok("prepare", branch="fresh")["setup"]
        self.assertEqual((passed["ran"], passed["ok"], passed["exit_code"]), (True, True, 0),
                         "a failed setup runs again on the next prepare")
        again = self.ok("prepare", branch="fresh")["setup"]
        self.assertEqual(again, {"ran": False, "ok": True}, "once it succeeds, never again")
        with open(os.path.join(self.worktree("fresh"), "setup.log")) as handle:
            self.assertEqual(handle.read().split(), ["run", "run"])

    def test_setup_needs_a_command(self):
        with self.assertRaises(config_module.ConfigError):
            config_module.setup_from_dict("demo", {"timeout": 5})

    def test_prepare_says_a_worktree_on_its_base_is_old(self):
        # The case that read old code: a worktree of the base branch itself,
        # prepared once, and the remote moved on.
        self.prepared("main")
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "main", "docs/new.txt", "from the other person\n")
        again = self.ok("prepare", branch="main")
        self.assertFalse(again["created"])
        self.assertNotEqual(again["head"], again["base_head"])
        self.assertEqual(again["behind"], 1)
        self.assertEqual(again["behind_remote"], 1)
        self.assertIn("use pull from head", again["note"])
        self.ok("pull", branch="main", **{"from": "head"})
        current = self.ok("prepare", branch="main")
        self.assertEqual(current["behind_remote"], 0)
        self.assertEqual(current["note"], "")

    def test_prepare_says_a_worktree_is_behind_its_remote_branch(self):
        self.prepared()
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "work", "docs/head.txt", "from the remote branch\n")
        again = self.ok("prepare", branch="work")
        self.assertEqual(again["behind"], 0)
        self.assertEqual(again["behind_remote"], 1)
        self.assertEqual(again["note"],
                         "the worktree is 1 commit behind origin/work: use pull from head")

    def test_prepare_says_a_worktree_is_behind_its_base(self):
        self.prepared()
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "main", "docs/new.txt", "from the other person\n")
        again = self.ok("prepare", branch="work")
        self.assertEqual(again["behind"], 1)
        self.assertIsNone(again["behind_remote"])
        self.assertEqual(again["note"],
                         "the worktree is 1 commit behind the base main: pull from base merges it in")

    def test_prepare_refuses_a_bad_branch_name(self):
        answer = self.refused("prepare", branch="../escape")
        self.assertEqual(answer["refused"], "branch")

    def test_prepare_drops_stray_paths_when_the_head_is_pushed(self):
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "work", "docs/head.txt", "from the remote branch\n")
        path = self.prepared()
        util.write(os.path.join(path, "docs/a.txt"), "stray\n")
        util.write(os.path.join(path, "docs/stray.txt"), "stray\n")
        util.write(os.path.join(path, "keys/kept.pem"), "denied\n")
        again = self.ok("prepare", branch="work")
        self.assertEqual(sorted(again["dropped"]), ["docs/a.txt", "docs/stray.txt"])
        self.assertEqual(again["dirty_paths"], ["keys/kept.pem"])
        self.assertFalse(os.path.exists(os.path.join(path, "docs/stray.txt")))
        with open(os.path.join(path, "docs/a.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "alpha\nbravo\ncharlie\n")

    def test_prepare_keeps_dirty_paths_when_commits_are_not_pushed(self):
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "work", "docs/head.txt", "from the remote branch\n")
        path = self.prepared()
        util.write(os.path.join(path, "docs/local.txt"), "a local commit\n")
        util.git(["add", "-A"], cwd=path)
        util.git(["commit", "-m", "not pushed"], cwd=path)
        util.write(os.path.join(path, "docs/a.txt"), "work in progress\n")
        again = self.ok("prepare", branch="work")
        self.assertEqual(again["dropped"], [])
        self.assertEqual(again["dirty"], 1)
        self.assertEqual(again["dirty_paths"], ["docs/a.txt"])
        with open(os.path.join(path, "docs/a.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "work in progress\n")

    def test_prepare_keeps_the_paths_the_edit_tools_wrote(self):
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "work", "docs/head.txt", "from the remote branch\n")
        path = self.prepared()
        self.ok("edit", branch="work", path="docs/a.txt", old="bravo", new="delta")
        self.ok("edit", branch="work", path="docs/made.txt", new="made\n", create=True)
        util.write(os.path.join(path, "docs/stray.txt"), "stray\n")
        again = self.ok("prepare", branch="work")
        self.assertEqual(again["dropped"], ["docs/stray.txt"])
        self.assertEqual(sorted(again["dirty_paths"]), ["docs/a.txt", "docs/made.txt"])
        self.assertFalse(os.path.exists(os.path.join(path, "docs/stray.txt")))
        self.assertTrue(os.path.isfile(os.path.join(path, "docs/made.txt")))
        with open(os.path.join(path, "docs/a.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "alpha\ndelta\ncharlie\n")

    def test_prepare_keeps_an_untracked_directory_the_edit_tools_wrote_into(self):
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "work", "docs/head.txt", "from the remote branch\n")
        path = self.prepared()
        self.ok("edit", branch="work", path="docs/new-dir/x.txt", new="made\n", create=True)
        util.write(os.path.join(path, "docs/stray.txt"), "stray\n")
        again = self.ok("prepare", branch="work")
        self.assertEqual(again["dropped"], ["docs/stray.txt"])
        self.assertEqual(again["dirty_paths"], ["docs/new-dir/"])
        self.assertFalse(os.path.exists(os.path.join(path, "docs/stray.txt")))
        with open(os.path.join(path, "docs/new-dir/x.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "made\n")

    def test_prepare_drops_a_path_written_outside_the_edit_tools(self):
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "work", "docs/head.txt", "from the remote branch\n")
        path = self.prepared()
        self.ok("edit", branch="work", path="docs/made.txt", new="made\n", create=True)
        util.write(os.path.join(path, "docs/a.txt"), "half a pull\n")
        again = self.ok("prepare", branch="work")
        self.assertEqual(again["dropped"], ["docs/a.txt"])
        self.assertEqual(again["dirty_paths"], ["docs/made.txt"])
        with open(os.path.join(path, "docs/a.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "alpha\nbravo\ncharlie\n")

    def test_submit_empties_the_ledger_of_written_paths(self):
        path = self.prepared()
        self.ok("edit", branch="work", path="docs/a.txt", old="bravo", new="delta")
        self.assertEqual(tools.written_paths(path), {"docs/a.txt"})
        self.ok("submit", branch="work", message="our line", trailers=["Seat: test"])
        self.assertEqual(tools.written_paths(path), set())


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

    def test_find_without_mode_gives_a_tree(self):
        self.prepared()
        answer = self.ok("find", branch="work", path="")
        self.assertEqual(answer["mode"], "tree")
        self.assertIn("README.md", [entry["path"] for entry in answer["entries"]])
        schema = tools.TOOLS["find"]["schema"]
        self.assertNotIn("mode", schema["required"])
        self.assertEqual(schema["properties"]["mode"]["default"], "tree")

    def test_find_with_a_pattern_and_no_mode_greps(self):
        self.prepared()
        answer = self.ok("find", branch="work", pattern="TODO")
        self.assertEqual(answer["mode"], "grep")
        self.assertEqual(answer["files"], [{"path": "src/app.py", "count": 2}])
        self.assertIn("grep", tools.TOOLS["find"]["schema"]["properties"]["mode"]["description"])

    def test_find_with_mode_tree_and_a_pattern_gives_a_tree(self):
        self.prepared()
        answer = self.ok("find", branch="work", mode="tree", pattern="TODO")
        self.assertEqual(answer["mode"], "tree")
        self.assertIn("README.md", [entry["path"] for entry in answer["entries"]])

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

    def test_find_grep_reads_alternation_and_inline_flags(self):
        # a basic regex answered both of these with nothing, and a seat
        # took the silence for "the field is not in this repository"
        path = self.prepared()
        util.write(os.path.join(path, "funds.py"),
                   "allvue_fund_identifier = Column()\nFA_CODE = 1\n")
        answer = self.ok("find", branch="work", mode="grep",
                         pattern="(?i)allvue|fa_code")
        self.assertEqual(answer["files"], [{"path": "funds.py", "count": 2}])
        answer = self.ok("find", branch="work", mode="grep", pattern="nothing|allvue_")
        self.assertEqual(len(answer["lines"]), 1)

    def test_find_grep_ignore_case(self):
        path = self.prepared()
        util.write(os.path.join(path, "funds.py"), "ALLVUE_ID = 1\n")
        self.assertEqual(self.ok("find", branch="work", mode="grep",
                                 pattern="allvue_id")["lines"], [])
        answer = self.ok("find", branch="work", mode="grep", pattern="allvue_id",
                         ignore_case=True)
        self.assertEqual(len(answer["lines"]), 1)

    def test_find_grep_refuses_a_pattern_it_cannot_read(self):
        self.prepared()
        answer = self.refused("find", branch="work", mode="grep", pattern="(unclosed")
        self.assertEqual(answer["refused"], "grep")

    def test_find_diff_gives_the_change_against_base(self):
        path = self.prepared()
        util.write(os.path.join(path, "docs/a.txt"), "alpha\ndelta\ncharlie\n")
        util.write(os.path.join(path, "new.txt"), "a new file\n")
        answer = self.ok("find", branch="work", mode="diff")
        self.assertIn("docs/a.txt", answer["diff"])
        self.assertIn("+delta", answer["diff"])
        self.assertIn("new.txt", answer["diff"])
        self.assertEqual(answer["files"], 2)


class TestHistory(BenchCase):

    def subjects(self, answer):
        return [commit["subject"] for commit in answer["commits"]]

    def test_history_by_path(self):
        path = self.prepared()
        util.write(os.path.join(path, "docs/a.txt"), "alpha\nbravo\ndelta\n")
        util.git(["commit", "-am", "change a"], cwd=path)
        util.write(os.path.join(path, "README.md"), "# demo\n")
        util.git(["commit", "-am", "change the readme"], cwd=path)
        answer = self.ok("history", branch="work", path="docs/a.txt")
        self.assertEqual(self.subjects(answer), ["change a", "the first commit"])
        self.assertEqual(len(answer["commits"][0]["sha"]), 40)
        self.assertRegex(answer["commits"][0]["date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(self.subjects(self.ok("history", branch="work", limit=1)),
                         ["change the readme"])

    def test_history_by_pickaxe(self):
        path = self.prepared()
        util.write(os.path.join(path, "src/app.py"), "def main():\n    return 2\n")
        util.git(["commit", "-am", "drop the marker"], cwd=path)
        answer = self.ok("history", branch="work", pickaxe="MARKER_ONE")
        self.assertEqual(self.subjects(answer), ["drop the marker", "the first commit"])
        self.assertEqual(self.ok("history", branch="work", pickaxe="no such text")["commits"], [])

    def test_history_all_reads_the_other_branches(self):
        self.prepared()
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "side", "docs/side.txt", "side\n", message="a side change")
        self.ok("prepare", branch="work")
        self.assertEqual(self.ok("history", branch="work", path="docs/side.txt")["commits"], [])
        answer = self.ok("history", branch="work", path="docs/side.txt", all=True)
        self.assertEqual(self.subjects(answer), ["a side change"])

    def test_history_refuses_a_denied_path(self):
        self.prepared()
        self.assertEqual(self.refused("history", branch="work", path="keys/server.pem")["refused"],
                         "denied")
        self.assertEqual(self.refused("history", branch="work", allow=["src/**"])["refused"],
                         "denied")


CLOJURE_FIXTURE = r'''(ns demo.core
  (:require [clojure.string :as str]))

(defn greet
  "Says hi (to someone)."
  [name]
  (str "hi) " name))

(defn- secret [] \) )

(g/defguard can-read?
  [x]
  x)

(defmulti area :shape)

(defmethod area :square [s]
  (* (:side s) (:side s)))

(defmethod area :circle [c]
  ;; a comment with ( paren
  (* 3 (:r c)))

(def ^:private limit 10)
'''

PYTHON_FIXTURE = '''import os


@decorator
def top(a):
    """Doc with ) paren."""
    return a


class Thing:
    def method(self):
        return ")"

    async def later(self):
        return 1


async def fetch():
    return None
'''


class TestSymbols(BenchCase):

    def with_fixtures(self):
        path = self.prepared()
        util.write(os.path.join(path, "lib/core.clj"), CLOJURE_FIXTURE)
        util.write(os.path.join(path, "lib/things.py"), PYTHON_FIXTURE)
        return path

    def test_find_symbols_gives_clojure_definitions_with_their_lines(self):
        self.with_fixtures()
        answer = self.ok("symbols", branch="work", path="lib/core.clj")
        self.assertEqual(
            [(item["name"], item["kind"], item["line"], item["end_line"])
             for item in answer["symbols"]],
            [("greet", "defn", 4, 7), ("secret", "defn-", 9, 9),
             ("can-read?", "g/defguard", 11, 13), ("area", "defmulti", 15, 15),
             ("area", "defmethod", 17, 18), ("area", "defmethod", 20, 22),
             ("limit", "def", 24, 24)])
        self.assertEqual({item["path"] for item in answer["symbols"]}, {"lib/core.clj"})

    def test_find_symbols_gives_python_definitions_and_methods(self):
        self.with_fixtures()
        answer = self.ok("symbols", branch="work", path="lib/things.py")
        self.assertEqual(
            [(item["name"], item["kind"], item["line"], item["end_line"])
             for item in answer["symbols"]],
            [("top", "def", 4, 7), ("Thing", "class", 10, 15),
             ("Thing.method", "method", 11, 12), ("Thing.later", "method", 14, 15),
             ("fetch", "async def", 18, 19)])

    def test_find_symbols_over_a_directory_with_a_pattern(self):
        self.with_fixtures()
        answer = self.ok("symbols", branch="work", path="lib", pattern="^(area|top)$")
        self.assertEqual([(item["path"], item["name"]) for item in answer["symbols"]],
                         [("lib/core.clj", "area")] * 3 + [("lib/things.py", "top")])

    def test_read_by_symbol_gives_exactly_the_form(self):
        self.with_fixtures()
        answer = self.ok("read_symbol", branch="work", path="lib/core.clj", symbol="greet")
        self.assertEqual(answer["matches"], 1)
        definition = answer["definitions"][0]
        self.assertEqual((definition["line"], definition["end_line"]), (4, 7))
        self.assertEqual([line["line"] for line in definition["lines"]], [4, 5, 6, 7])
        self.assertEqual(definition["lines"][-1]["text"], '  (str "hi) " name))')
        method = self.ok("read_symbol", branch="work", path="lib/things.py", symbol="Thing.method")
        self.assertEqual([line["text"] for line in method["definitions"][0]["lines"]],
                         ["    def method(self):", '        return ")"'])

    def test_read_by_symbol_gives_each_defmethod(self):
        self.with_fixtures()
        answer = self.ok("read_symbol", branch="work", path="lib/core.clj", symbol="area")
        self.assertEqual([(item["kind"], item["line"], item["end_line"])
                          for item in answer["definitions"]],
                         [("defmulti", 15, 15), ("defmethod", 17, 18), ("defmethod", 20, 22)])

    def test_read_by_an_unknown_symbol_names_close_matches(self):
        self.with_fixtures()
        answer = self.refused("read_symbol", branch="work", path="lib/core.clj", symbol="greeet")
        self.assertEqual(answer["refused"], "not_found")
        self.assertIn("greet", answer["close"])

    def test_find_refuses_mode_symbols(self):
        self.with_fixtures()
        answer = self.refused("find", branch="work", mode="symbols", path="lib")
        self.assertEqual(answer["field"], "mode")
        self.assertIn("symbols tool", answer["reason"])

    def test_find_tree_of_a_file_says_to_read_it(self):
        self.with_fixtures()
        answer = self.refused("find", branch="work", mode="tree", path="lib/core.clj")
        self.assertEqual(answer["refused"], "is_file")
        self.assertIn("read tool", answer["remedy"])

    def test_read_of_a_directory_says_to_list_it(self):
        self.with_fixtures()
        answer = self.refused("read", branch="work", path="lib")
        self.assertEqual(answer["refused"], "is_directory")
        self.assertIn("tree", answer["remedy"])
        answer = self.refused("read", branch="work", path="docs", ref="base")
        self.assertEqual(answer["refused"], "is_directory")

    def test_find_glob_and_grep_need_pattern(self):
        self.with_fixtures()
        for mode in ("glob", "grep"):
            answer = self.refused("find", branch="work", mode=mode, path="lib")
            self.assertEqual(answer["field"], "pattern")
            self.assertIn(mode, answer["reason"])
        self.assertIn("pattern", tools.TOOLS["symbols"]["schema"]["required"])
        self.assertIn("require pattern", tools.TOOLS["find"]["description"])

    def test_read_refuses_symbol(self):
        self.with_fixtures()
        answer = self.refused("read", branch="work", path="lib/core.clj", symbol="greet")
        self.assertEqual(answer["field"], "symbol")
        self.assertIn("read_symbol", answer["reason"])


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

    def test_read_refuses_start_and_end_and_names_offset_and_limit(self):
        self.prepared()
        answer = self.refused("read", branch="work", path="docs/a.txt", start=2, end=3)
        self.assertEqual(answer["refused"], "input")
        self.assertEqual(answer["unknown"], ["end", "start"])
        self.assertIn("offset", answer["reason"])
        self.assertIn("limit", answer["reason"])
        properties = tools.TOOLS["read"]["schema"]["properties"]
        self.assertIn("first line", properties["offset"]["description"])
        self.assertIn("count of lines", properties["limit"]["description"])

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

    def test_a_long_read_gives_120_lines_and_a_note(self):
        path = self.prepared()
        util.write(os.path.join(path, "docs/long.txt"), "".join("line %d\n" % i for i in range(1, 301)))
        answer = self.ok("read", branch="work", path="docs/long.txt", limit=300)
        self.assertEqual([item["line"] for item in answer["lines"]], list(range(1, 121)))
        self.assertFalse(answer["eof"])
        self.assertEqual(answer["note"], "for a definition use bench__read_symbol; for more, page with offset")
        self.assertNotIn("note", self.ok("read", branch="work", path="docs/long.txt", limit=10))


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
        self.assertEqual(answer["reason"], "a write under .github/ or .claude/ needs this "
                         "seat's scope to name the path in its bench.edit filter")
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
        self.assertIn("new with create: true", answer["reason"])

    def test_edit_takes_content_as_new_for_a_new_file(self):
        path = self.prepared()
        self.ok("edit", branch="work", path="docs/d.txt", content="delta\n")
        with open(os.path.join(path, "docs/d.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "delta\n")
        self.ok("edit", branch="work", path="docs/e.txt", content="echo\n", create=True)
        self.assertTrue(os.path.isfile(os.path.join(path, "docs/e.txt")))

    def test_edit_refuses_content_beside_old_naming_new(self):
        self.prepared()
        answer = self.refused("edit", branch="work", path="src/app.py",
                              old="MARKER_ONE = 'one'", content="MARKER_ONE = 'two'")
        self.assertEqual(answer["field"], "content")
        self.assertIn("new", answer["reason"])

    def test_edit_refuses_content_in_create_naming_the_shape(self):
        self.prepared()
        answer = self.refused("edit", branch="work", path="docs/f.txt", create="foxtrot\n")
        self.assertEqual(answer["field"], "create")
        self.assertIn("new with create: true", answer["reason"])

    def test_edit_applies_a_list_of_edits_across_two_files(self):
        path = self.prepared()
        answer = self.ok("edit_many", branch="work", edits=[
            {"path": "src/app.py", "old": "MARKER_ONE = 'one'", "new": "MARKER_ONE = 'two'"},
            {"path": "docs/a.txt", "old": "alpha", "new": "ALPHA"},
            {"path": "docs/a.txt", "old": "bravo", "new": "BRAVO"},
        ])
        self.assertEqual([item["path"] for item in answer["edits"]],
                         ["src/app.py", "docs/a.txt", "docs/a.txt"])
        self.assertNotIn("content", answer["edits"][0])
        with open(os.path.join(path, "src/app.py"), encoding="utf-8") as handle:
            self.assertIn("MARKER_ONE = 'two'", handle.read())
        with open(os.path.join(path, "docs/a.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "ALPHA\nBRAVO\ncharlie\n")

    def test_edit_list_with_a_missing_old_writes_none_and_names_the_item(self):
        path = self.prepared()
        answer = self.refused("edit_many", branch="work", edits=[
            {"path": "src/app.py", "old": "MARKER_ONE = 'one'", "new": "MARKER_ONE = 'two'"},
            {"path": "docs/h.txt", "new": "hotel\n", "create": True},
            {"path": "docs/a.txt", "old": "zulu", "new": "ZULU"},
        ])
        self.assertEqual(answer["refused"], "found")
        self.assertEqual(answer["item"], 3)
        with open(os.path.join(path, "src/app.py"), encoding="utf-8") as handle:
            self.assertIn("MARKER_ONE = 'one'", handle.read())
        self.assertFalse(os.path.exists(os.path.join(path, "docs/h.txt")))

    def test_edit_list_with_a_protected_path_refuses_the_whole_list(self):
        path = self.prepared()
        answer = self.refused("edit_many", branch="work", edits=[
            {"path": "docs/a.txt", "old": "alpha", "new": "ALPHA"},
            {"path": ".github/workflows/ci.yml", "old": "name: ci", "new": "name: gate"},
        ])
        self.assertEqual(answer["refused"], "protected")
        self.assertEqual(answer["item"], 2)
        self.assertIn("its bench.edit filter", answer["reason"])
        with open(os.path.join(path, "docs/a.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "alpha\nbravo\ncharlie\n")

    def test_edit_list_with_allow_protected_writes_a_protected_path(self):
        path = self.prepared()
        answer = self.ok("edit_many", branch="work", allow_protected=True, edits=[
            {"path": "docs/a.txt", "old": "alpha", "new": "ALPHA"},
            {"path": ".github/workflows/ci.yml", "old": "name: ci", "new": "name: gate"},
        ])
        self.assertEqual([item["path"] for item in answer["edits"]],
                         ["docs/a.txt", ".github/workflows/ci.yml"])
        with open(os.path.join(path, ".github/workflows/ci.yml"), encoding="utf-8") as handle:
            self.assertIn("name: gate", handle.read())
        with open(os.path.join(path, "docs/a.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "ALPHA\nbravo\ncharlie\n")

    def test_edit_list_with_an_empty_old_creates_a_protected_path(self):
        path = self.prepared()
        made = ".github/workflows/new.yml"
        answer = self.refused("edit_many", branch="work", edits=[
            {"path": made, "old": "", "new": "name: new\n"},
        ])
        self.assertEqual(answer["refused"], "protected")
        self.assertFalse(os.path.exists(os.path.join(path, made)))
        answer = self.ok("edit_many", branch="work", allow_protected=True, edits=[
            {"path": made, "old": "", "new": "name: new\n"},
        ])
        self.assertEqual([item["path"] for item in answer["edits"]], [made])
        self.assertTrue(answer["edits"][0]["hash"])
        with open(os.path.join(path, made), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "name: new\n")
        sibling = self.refused("edit_many", branch="work", edits=[
            {"path": ".github/workflows/other.yml", "old": "", "new": "name: other\n"},
        ])
        self.assertEqual(sibling["refused"], "protected")
        self.assertFalse(os.path.exists(os.path.join(path, ".github/workflows/other.yml")))

    def test_edit_with_create_makes_a_protected_path(self):
        path = self.prepared()
        made = ".github/workflows/made.yml"
        answer = self.ok("edit", branch="work", path=made, create=True,
                         new="name: made\n", allow_protected=True)
        self.assertTrue(answer["hash"])
        self.assertTrue(os.path.isfile(os.path.join(path, made)))
        missing = self.refused("edit", branch="work", path=".github/workflows/none.yml",
                               old="name: ci", new="name: gate", allow_protected=True)
        self.assertEqual(missing["refused"], "not_found")
        self.assertIn("create: true", missing["remedy"])

    def test_edit_list_beside_the_fields_of_one_edit_is_refused(self):
        self.prepared()
        answer = self.refused("edit_many", branch="work", path="docs/a.txt", delete=True,
                              edits=[{"path": "docs/a.txt", "old": "alpha", "new": "ALPHA"}])
        self.assertEqual(answer["field"], "edits")
        self.assertEqual(answer["beside"], ["path", "delete"])

    def test_edit_list_is_capped(self):
        self.prepared()
        answer = self.refused("edit_many", branch="work",
                              edits=[{"path": "docs/a.txt", "delete": True}] * 51)
        self.assertEqual(answer["field"], "edits")

    def test_edit_refuses_edits(self):
        path = self.prepared()
        answer = self.refused("edit", branch="work",
                              edits=[{"path": "docs/a.txt", "old": "alpha", "new": "ALPHA"}])
        self.assertEqual(answer["field"], "edits")
        self.assertIn("edit_many", answer["reason"])
        with open(os.path.join(path, "docs/a.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "alpha\nbravo\ncharlie\n")

    def test_edit_list_follows_the_file_from_edit_to_edit(self):
        path = self.prepared()
        self.ok("edit_many", branch="work", edits=[
            {"path": "docs/i.txt", "new": "india\n", "create": True},
            {"path": "docs/i.txt", "move_to": "docs/j.txt"},
            {"path": "docs/j.txt", "old": "india", "new": "juliett"},
        ])
        self.assertFalse(os.path.exists(os.path.join(path, "docs/i.txt")))
        with open(os.path.join(path, "docs/j.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "juliett\n")

    def test_edit_creates_from_new_with_create_true(self):
        path = self.prepared()
        self.ok("edit", branch="work", path="docs/g.txt", new="golf\n", create=True)
        self.assertTrue(os.path.isfile(os.path.join(path, "docs/g.txt")))

    def test_a_missed_old_names_the_nearest_line(self):
        self.prepared()
        answer = self.refused("edit", branch="work", path="docs/a.txt",
                              old="alpha\n  bravo", new="ALPHA\nBRAVO")
        self.assertEqual(answer["found"], 0)
        self.assertEqual(answer["nearest"], {"line": 2, "text": "bravo", "old": "  bravo"})
        answer = self.refused("edit", branch="work", path="docs/a.txt", old="charlee", new="C")
        self.assertEqual(answer["nearest"]["line"], 3)

    def test_a_missed_old_gives_the_block_to_paste(self):
        self.prepared()
        answer = self.refused("edit_many", branch="work", edits=[
            {"path": "docs/a.txt", "old": "alpha\n  bravo", "new": "ALPHA\nBRAVO"}])
        self.assertEqual(answer["block"], {"line": 1, "text": "alpha\nbravo"})
        self.assertIn("create: true, and no old", tools.TOOLS["edit_many"]["description"])

    def test_an_old_found_twice_names_its_lines(self):
        path = self.prepared()
        util.write(os.path.join(path, "docs/a.txt"), "alpha\nbravo\nalpha\n")
        answer = self.refused("edit", branch="work", path="docs/a.txt", old="alpha", new="A")
        self.assertEqual((answer["found"], answer["lines"]), (2, [1, 3]))
        self.assertIn("2 times", answer["remedy"])

    def test_edit_list_names_every_bad_item(self):
        path = self.prepared()
        answer = self.refused("edit_many", branch="work", edits=[
            {"path": "docs/a.txt", "old": "alpha", "new": "ALPHA"},
            {"path": "docs/a.txt", "old": "zulu", "new": "ZULU"},
            {"path": "docs/a.txt", "delete": True, "move_to": "docs/z.txt"},
            {"path": "docs/a.txt", "old": "bravo", "new": "BRAVO", "why": "a reason"},
        ])
        self.assertEqual((answer["refused"], answer["item"]), ("found", 2))
        self.assertEqual([(item["item"], item["refused"]) for item in answer["items"]],
                         [(2, "found"), (3, "operation"), (4, "input")])
        self.assertEqual(answer["items"][2]["field"], "why")
        with open(os.path.join(path, "docs/a.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "alpha\nbravo\ncharlie\n")

    def test_edit_schema_types_create_and_delete_as_booleans(self):
        properties = tools.TOOLS["edit"]["schema"]["properties"]
        self.assertEqual(properties["create"]["type"], "boolean")
        self.assertEqual(properties["delete"]["type"], "boolean")
        self.assertIn("new with create: true", properties["create"]["description"])
        self.assertIn("new with create: true", tools.TOOLS["edit"]["description"])


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
        self.assertTrue(answer["merge_in_progress"])
        util.git(["rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=path)
        self.assertTrue(self.ok("status", branch="work")["merge_in_progress"])

    def test_a_conflicted_pull_gives_the_lines_of_its_markers(self):
        path = self.prepared()
        util.write(os.path.join(path, "docs/a.txt"), "alpha\nours\ncharlie\n")
        self.ok("submit", branch="work", message="our line", trailers=["Seat: test"])
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "main", "docs/a.txt", "alpha\ntheirs\ncharlie\n")
        answer = self.ok("pull", branch="work", **{"from": "base"})
        expected = [{"path": "docs/a.txt", "ranges": [{"start": 2, "end": 6}]}]
        self.assertEqual(answer["markers"], expected)
        self.assertEqual(self.ok("status", branch="work")["markers"], expected)
        self.assertEqual(self.ok("pull", branch="work", **{"from": "base"})["markers"], expected)

    def test_pull_again_commits_a_resolved_merge(self):
        path = self.prepared()
        util.write(os.path.join(path, "docs/a.txt"), "alpha\nours\ncharlie\n")
        self.ok("submit", branch="work", message="our line", trailers=["Seat: test"])
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "main", "docs/a.txt", "alpha\ntheirs\ncharlie\n")
        self.assertFalse(self.ok("pull", branch="work", **{"from": "base"})["merged"])
        still = self.ok("pull", branch="work", **{"from": "base"})
        self.assertEqual(still["conflicts"], ["docs/a.txt"])
        self.assertTrue(still["merge_in_progress"])
        util.write(os.path.join(path, "docs/a.txt"), "alpha\nboth\ncharlie\n")
        answer = self.ok("pull", branch="work", **{"from": "base"})
        self.assertTrue(answer["merged"])
        self.assertEqual(answer["conflicts"], [])
        parents = util.git(["log", "-1", "--format=%P", "HEAD"], cwd=path).split()
        self.assertEqual(len(parents), 2)
        status = self.ok("status", branch="work")
        self.assertFalse(status["merge_in_progress"])
        self.assertEqual(status["behind"], 0)
        self.assertEqual(status["dirty"], 0)

    def test_pull_that_fails_leaves_the_worktree_as_it_was(self):
        path = self.prepared()
        util.write(os.path.join(path, "docs/mine.txt"), "my edit\n")
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "main", "docs/new.txt", "from the other person\n")
        real = tools.git.run

        def half_failed(argv, cwd=None, **kwargs):
            if list(argv[:2]) == ["merge", "--no-edit"]:
                # a merge that wrote files and then stopped without a conflict
                util.write(os.path.join(cwd, "docs/a.txt"), "half\n")
                util.write(os.path.join(cwd, "docs/half.txt"), "half\n")
                return 1, "", "error: the merge stopped"
            return real(argv, cwd=cwd, **kwargs)

        with mock.patch.object(tools.git, "run", side_effect=half_failed):
            answer = self.refused("pull", branch="work", **{"from": "base"})
        self.assertEqual(answer["refused"], "merge_failed")
        self.assertEqual(sorted(answer["reset"]), ["docs/a.txt", "docs/half.txt"])
        self.assertEqual(tools.status_paths(path), ["docs/mine.txt"])
        with open(os.path.join(path, "docs/a.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "alpha\nbravo\ncharlie\n")

    def test_pull_puts_back_the_uncommitted_edits(self):
        path = self.prepared()
        util.write(os.path.join(path, "docs/a.txt"), "alpha\nmine\ncharlie\n")
        util.write(os.path.join(path, "docs/mine.txt"), "my edit\n")
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "main", "docs/new.txt", "from the other person\n")
        answer = self.ok("pull", branch="work", **{"from": "base"})
        self.assertTrue(answer["merged"])
        self.assertEqual(answer["reapplied"], [{"path": "docs/a.txt", "status": "restored"},
                                               {"path": "docs/mine.txt", "status": "restored"}])
        self.assertTrue(os.path.isfile(os.path.join(path, "docs/new.txt")))
        with open(os.path.join(path, "docs/a.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "alpha\nmine\ncharlie\n")
        self.assertEqual(sorted(tools.status_paths(path)), ["docs/a.txt", "docs/mine.txt"])

    def test_pull_names_the_edits_that_meet_the_merge(self):
        path = self.prepared()
        util.write(os.path.join(path, "docs/a.txt"), "alpha\nmine\ncharlie\n")
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "main", "docs/a.txt", "alpha\ntheirs\ncharlie\n")
        answer = self.ok("pull", branch="work", **{"from": "base"})
        self.assertTrue(answer["merged"])
        self.assertEqual(answer["conflicts"], [])
        self.assertEqual(answer["reapplied"], [{"path": "docs/a.txt", "status": "conflicted"}])
        with open(os.path.join(path, "docs/a.txt"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("<<<<<<<", text)
        self.assertIn("mine", text)
        self.assertIn("theirs", text)
        status = self.ok("status", branch="work")
        self.assertFalse(status["merge_in_progress"])
        self.assertEqual(status["paths"], ["docs/a.txt"])

    def test_pull_from_head_moves_to_the_remote_branch(self):
        self.prepared()
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "work", "docs/head.txt", "from the remote branch\n")
        answer = self.ok("pull", branch="work", **{"from": "head"})
        self.assertTrue(answer["merged"])
        self.assertTrue(os.path.isfile(os.path.join(self.worktree(), "docs/head.txt")))


class TestDiff(BenchCase):

    def test_diff_gives_each_dirty_path_against_head(self):
        path = self.prepared()
        util.write(os.path.join(path, "docs/a.txt"), "alpha\nmine\ncharlie\n")
        util.write(os.path.join(path, "docs/mine.txt"), "my edit\n")
        answer = self.ok("diff", branch="work")
        files = {item["path"]: item for item in answer["files"]}
        self.assertEqual(sorted(files), ["docs/a.txt", "docs/mine.txt"])
        self.assertEqual(files["docs/a.txt"]["status"], "changed")
        self.assertIn("-bravo", files["docs/a.txt"]["diff"])
        self.assertIn("+mine", files["docs/a.txt"]["diff"])
        self.assertEqual(files["docs/mine.txt"]["status"], "untracked")
        self.assertIn("+my edit", files["docs/mine.txt"]["diff"])
        self.assertFalse(answer["truncated"])
        only = self.ok("diff", branch="work", paths=["docs/mine.txt"])
        self.assertEqual([item["path"] for item in only["files"]], ["docs/mine.txt"])

    def test_diff_cuts_each_text_and_the_whole_and_says_so(self):
        path = self.prepared()
        long = "".join("line %d\n" % number for number in range(500))
        util.write(os.path.join(path, "docs/a.txt"), long)
        util.write(os.path.join(path, "docs/b.txt"), long)
        answer = self.ok("diff", branch="work", max_bytes=512, max_total=700)
        self.assertTrue(answer["truncated"])
        first, second = answer["files"]
        self.assertTrue(first["truncated"])
        self.assertEqual(len(first["diff"].encode("utf-8")), 512)
        self.assertGreater(first["bytes"], 512)
        self.assertTrue(second["truncated"])
        self.assertEqual(len(second["diff"].encode("utf-8")), 188)


class TestConflicts(BenchCase):

    def state(self, path):
        return (util.git(["rev-parse", "HEAD"], cwd=path),
                util.git(["status", "--porcelain", "--untracked-files=all"], cwd=path))

    def test_conflicts_answers_nothing_for_a_clean_merge(self):
        path = self.prepared()
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "main", "docs/new.txt", "from the other person\n")
        before = self.state(path)
        answer = self.ok("conflicts", branch="work")
        self.assertEqual(answer["paths"], [])
        self.assertEqual(answer["base"], "main")
        self.assertEqual(self.state(path), before)
        self.assertFalse(os.path.exists(os.path.join(path, "docs/new.txt")))

    def test_conflicts_answers_the_conflicted_file(self):
        path = self.prepared()
        util.write(os.path.join(path, "docs/a.txt"), "alpha\nours\ncharlie\n")
        self.ok("submit", branch="work", message="our line", trailers=["Seat: test"])
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "main", "docs/a.txt", "alpha\ntheirs\ncharlie\n")
        before = self.state(path)
        answer = self.ok("conflicts", branch="work")
        self.assertEqual(answer["paths"], ["docs/a.txt"])
        self.assertEqual(self.state(path), before)
        with open(os.path.join(path, "docs/a.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "alpha\nours\ncharlie\n")

    def test_conflicts_refuses_a_dirty_worktree(self):
        path = self.prepared()
        util.write(os.path.join(path, "docs/a.txt"), "alpha\nedited\ncharlie\n")
        before = self.state(path)
        answer = self.refused("conflicts", branch="work")
        self.assertEqual(answer["refused"], "dirty")
        self.assertEqual(answer["paths"], ["docs/a.txt"])
        self.assertEqual(self.state(path), before)


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

    HOSTED = "name: ci\non: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps: []\n"

    def test_submit_refuses_an_added_hosted_runs_on(self):
        path = self.prepared()
        util.write(os.path.join(path, ".github/workflows/new.yml"), self.HOSTED)
        answer = self.refused("submit", branch="work", message="a hosted runner")
        self.assertEqual(answer["refused"], "hosted_runner")
        finding = answer["findings"][0]
        self.assertEqual((finding["path"], finding["line"]), (".github/workflows/new.yml", 5))
        self.assertIn("ubuntu-latest", finding["message"])
        self.assertIn("waymark", answer["house_labels"])
        self.assertEqual(self.ok("status", branch="work")["dirty"], 1)

    def test_submit_passes_a_house_runs_on(self):
        path = self.prepared()
        util.write(os.path.join(path, ".github/workflows/new.yml"),
                   self.HOSTED.replace("ubuntu-latest", "waymark"))
        self.assertTrue(self.ok("submit", branch="work", message="a house runner")["pushed"])

    def test_submit_passes_a_hosted_runs_on_the_change_does_not_touch(self):
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "main", ".github/workflows/old.yml", self.HOSTED)
        path = self.prepared()
        util.write(os.path.join(path, ".github/workflows/old.yml"),
                   self.HOSTED + "    timeout-minutes: 5\n")
        answer = self.ok("check", branch="work")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["hosted_runs_on"], [
            {"path": ".github/workflows/old.yml", "line": 5, "text": "runs-on: ubuntu-latest",
             "allowed": False}])
        self.assertTrue(self.ok("submit", branch="work", message="a timeout")["pushed"])

    def test_submit_refuses_an_expression_fallback_to_a_hosted_runner(self):
        path = self.prepared()
        util.write(os.path.join(path, ".github/workflows/ci.yml"), self.HOSTED.replace(
            "ubuntu-latest", "${{ vars.RUNNER || 'ubuntu-latest' }}"))
        answer = self.refused("submit", branch="work", message="a fallback")
        self.assertEqual(answer["refused"], "hosted_runner")
        self.assertEqual(answer["findings"][0]["line"], 5)

    def test_submit_refuses_a_hosted_matrix_value_that_feeds_runs_on(self):
        path = self.prepared()
        util.write(os.path.join(path, ".github/workflows/ci.yml"), self.HOSTED.replace(
            "    runs-on: ubuntu-latest\n",
            "    strategy:\n      matrix:\n        os: [waymark, macos-14]\n"
            "    runs-on: ${{ matrix.os }}\n"))
        answer = self.refused("submit", branch="work", message="a matrix")
        self.assertEqual(answer["refused"], "hosted_runner")
        self.assertEqual(answer["findings"][0]["line"], 7)

    def test_submit_passes_a_hosted_runs_on_in_a_listed_workflow(self):
        self.config.repo("demo").hosted_workflows = [".github/workflows/ansible.yml"]
        path = self.prepared()
        util.write(os.path.join(path, ".github/workflows/ansible.yml"), self.HOSTED)
        answer = self.ok("check", branch="work")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["hosted_runs_on"], [
            {"path": ".github/workflows/ansible.yml", "line": 5,
             "text": "runs-on: ubuntu-latest", "allowed": True}])
        self.assertTrue(self.ok("submit", branch="work", message="a listed workflow")["pushed"])

    def test_submit_refuses_a_hosted_runs_on_in_an_unlisted_workflow(self):
        self.config.repo("demo").hosted_workflows = [".github/workflows/ansible.yml"]
        path = self.prepared()
        util.write(os.path.join(path, ".github/workflows/ansible.yml"), self.HOSTED)
        util.write(os.path.join(path, ".github/workflows/app-image.yml"), self.HOSTED)
        answer = self.refused("submit", branch="work", message="an unlisted workflow")
        self.assertEqual(answer["refused"], "hosted_runner")
        self.assertEqual([f["path"] for f in answer["findings"]],
                         [".github/workflows/app-image.yml"])

    def test_hosted_workflows_absent_or_null_is_an_empty_list(self):
        self.assertEqual(self.config.repo("demo").hosted_workflows, [])
        self.assertEqual(config_module.hosted_workflows_from_dict("demo", None), [])
        self.assertEqual(self.config.repo("demo").to_dict()["hosted_workflows"], [])
        with self.assertRaises(config_module.ConfigError):
            config_module.hosted_workflows_from_dict("demo", "ansible.yml")

    def test_submit_refuses_a_change_over_the_ceiling(self):
        path = self.prepared()
        util.write(os.path.join(path, "big.txt"), "line\n" * 40)
        answer = self.refused("submit", branch="work", message="a large change", max_lines=10)
        self.assertEqual(answer["refused"], "over_ceiling")
        self.assertEqual(answer["lines"], 40)
        self.assertEqual(answer["max_lines"], 10)
        base_head = util.git(["rev-parse", "refs/remotes/origin/main"], cwd=path).strip()
        self.assertEqual(answer["against"], base_head)
        self.assertEqual(answer["target"], "main")

    def test_submit_ceiling_does_not_count_a_merged_base(self):
        # A conflict fix merges the base in; the base's own lines are on
        # both sides of the pull request's diff, so they do not count.
        path = self.prepared()
        util.write(os.path.join(path, "docs/a.txt"), "alpha\nours\ncharlie\n")
        self.ok("submit", branch="work", message="our line", trailers=["Seat: test"])
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "main", "big.txt", "line\n" * 40)
        util.push_change(other, "main", "docs/a.txt", "alpha\ntheirs\ncharlie\n")
        pulled = self.ok("pull", branch="work", **{"from": "base"})
        self.assertEqual(pulled["conflicts"], ["docs/a.txt"])
        util.write(os.path.join(path, "docs/a.txt"), "alpha\nboth\ncharlie\n")
        answer = self.ok("submit", branch="work", message="fix the conflict", max_lines=10)
        self.assertEqual(answer["lines_added"] + answer["lines_removed"], 2)
        self.assertEqual(answer["files"], 1)
        parents = util.git(["log", "-1", "--format=%P", answer["commit"]], cwd=path).split()
        self.assertEqual(len(parents), 2)

    def test_submit_ceiling_counts_the_whole_change_across_rounds(self):
        path = self.prepared()
        util.write(os.path.join(path, "one.txt"), "line\n" * 8)
        self.ok("submit", branch="work", message="round one", max_lines=10)
        util.write(os.path.join(path, "two.txt"), "line\n" * 8)
        answer = self.refused("submit", branch="work", message="round two", max_lines=10)
        self.assertEqual(answer["refused"], "over_ceiling")
        self.assertEqual(answer["lines"], 16)

    def test_submit_over_the_ceiling_keeps_a_merge_in_progress(self):
        path = self.prepared()
        util.write(os.path.join(path, "docs/a.txt"), "alpha\nours\ncharlie\n")
        self.ok("submit", branch="work", message="our line", trailers=["Seat: test"])
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "main", "docs/a.txt", "alpha\ntheirs\ncharlie\n")
        self.assertFalse(self.ok("pull", branch="work", **{"from": "base"})["merged"])
        util.write(os.path.join(path, "docs/a.txt"), "alpha\nboth\ncharlie\n")
        # the ceiling counts only the change's own lines, so these put it over
        util.write(os.path.join(path, "mine.txt"), "line\n" * 40)
        answer = self.refused("submit", branch="work", message="the merge", max_lines=10)
        self.assertEqual(answer["refused"], "over_ceiling")
        util.git(["rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=path)
        os.remove(os.path.join(path, "mine.txt"))
        done = self.ok("submit", branch="work", message="the merge", trailers=["Seat: test"])
        parents = util.git(["log", "-1", "--format=%P", done["commit"]], cwd=path).split()
        self.assertEqual(len(parents), 2)

    def test_submit_refuses_a_merge_whose_files_still_hold_markers(self):
        path = self.prepared()
        util.write(os.path.join(path, "docs/a.txt"), "alpha\nours\ncharlie\n")
        self.ok("submit", branch="work", message="our line", trailers=["Seat: test"])
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "main", "docs/a.txt", "alpha\ntheirs\ncharlie\n")
        self.assertFalse(self.ok("pull", branch="work", **{"from": "base"})["merged"])
        head = util.git(["rev-parse", "HEAD"], cwd=path).strip()
        answer = self.refused("submit", branch="work", message="the markers")
        self.assertEqual(answer["refused"], "conflicts")
        self.assertEqual(answer["paths"], ["docs/a.txt"])
        self.assertEqual(util.git(["rev-parse", "HEAD"], cwd=path).strip(), head)
        util.git(["rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=path)

    def test_submit_over_the_ceiling_keeps_the_unmerged_entries(self):
        path = self.prepared()
        util.write(os.path.join(path, "docs/a.txt"), "alpha\nours\ncharlie\n")
        self.ok("submit", branch="work", message="our line", trailers=["Seat: test"])
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "main", "docs/a.txt", "alpha\ntheirs\ncharlie\n")
        self.assertFalse(self.ok("pull", branch="work", **{"from": "base"})["merged"])
        # resolved but not staged; a file of its own puts the change over the ceiling
        util.write(os.path.join(path, "docs/a.txt"), "alpha\nboth\ncharlie\n")
        util.write(os.path.join(path, "mine.txt"), "line\n" * 40)
        answer = self.refused("submit", branch="work", message="the merge", max_lines=10)
        self.assertEqual(answer["refused"], "over_ceiling")
        unmerged = util.git(["diff", "--name-only", "--diff-filter=U"], cwd=path).split()
        self.assertEqual(unmerged, ["docs/a.txt"])

    def test_submit_refuses_a_push_that_does_not_land(self):
        path = self.prepared()
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "work", "docs/other.txt", "another person was first\n")
        util.write(os.path.join(path, "docs/a.txt"), "alpha\nours\ncharlie\n")
        answer = self.refused("submit", branch="work", message="our change")
        self.assertEqual(answer["refused"], "push_rejected")
        self.assertIn("pull", answer["remedy"])

    def test_submit_names_the_end_of_a_failed_hook(self):
        # A hook prints its progress first and its error last: the refusal keeps the end.
        path = self.prepared()
        hooks = util.git(["rev-parse", "--git-path", "hooks"], cwd=path).strip()
        hook = os.path.join(path, hooks, "pre-commit")
        util.write(hook, "#!/bin/sh\nfor i in $(seq 1 100); do echo \"codegen progress line $i\"; done\n"
                         "echo 'ERROR: the real cause' >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        util.write(os.path.join(path, "docs/a.txt"), "alpha\nours\ncharlie\n")
        answer = self.refused("submit", branch="work", message="our change")
        self.assertEqual(answer["refused"], "commit_failed")
        self.assertIn("ERROR: the real cause", answer["reason"])
        self.assertTrue(answer["reason"].startswith("…"))
        self.assertNotIn("progress line 1\n", answer["reason"])


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


class TestNarrow(BenchCase):
    """The narrow call: allow, seat and sitting."""

    def test_read_with_allow_serves_the_path_and_refuses_another(self):
        self.prepared()
        answer = self.ok("read", branch="work", path="docs/a.txt", allow=["docs/**"])
        self.assertEqual(answer["lines"][0]["text"], "alpha")
        refused = self.refused("read", branch="work", path="src/app.py", allow=["docs/**"])
        self.assertEqual(refused["refused"], "denied")
        self.assertEqual(refused["path"], "src/app.py")
        self.assertEqual(refused["allow"], ["docs/**"])

    def test_read_with_an_empty_allow_refuses_every_path(self):
        self.prepared()
        answer = self.refused("read", branch="work", path="docs/a.txt", allow=[])
        self.assertEqual(answer["refused"], "denied")
        self.assertEqual(answer["allow"], [])

    def test_edit_with_allow_refuses_a_path_outside_it_and_writes_nothing(self):
        path = self.prepared()
        answer = self.refused("edit", branch="work", path="src/app.py",
                              old="MARKER_ONE = 'one'", new="MARKER_ONE = 'two'",
                              allow=["docs/**"])
        self.assertEqual(answer["refused"], "denied")
        self.assertEqual(answer["allow"], ["docs/**"])
        with open(os.path.join(path, "src/app.py"), encoding="utf-8") as handle:
            self.assertIn("MARKER_ONE = 'one'", handle.read())
        self.assertEqual(self.ok("status", branch="work")["dirty"], 0)

    def test_find_tree_with_allow_lists_only_the_matching_paths(self):
        self.prepared()
        answer = self.ok("find", branch="work", mode="tree", depth=2, allow=["docs/**"])
        paths = [entry["path"] for entry in answer["entries"]]
        self.assertIn("docs", paths)
        self.assertIn("docs/a.txt", paths)
        self.assertNotIn("README.md", paths)
        self.assertNotIn("src", paths)
        self.assertNotIn("src/app.py", paths)
        self.assertEqual(answer["dropped"], 0)

    def test_find_grep_with_allow_matches_only_in_the_allowed_paths(self):
        path = self.prepared()
        util.write(os.path.join(path, "docs/todo.txt"), "TODO: a note\n")
        answer = self.ok("find", branch="work", mode="grep", pattern="TODO",
                         allow=["docs/**"])
        self.assertEqual(answer["files"], [{"path": "docs/todo.txt", "count": 1}])
        self.assertEqual([item["path"] for item in answer["lines"]], ["docs/todo.txt"])

    def test_a_deny_glob_wins_over_an_allow_glob(self):
        self.prepared()
        answer = self.refused("read", branch="work", path="keys/server.pem",
                              allow=["keys/**"])
        self.assertEqual(answer["refused"], "denied")
        self.assertEqual(answer["pattern"], "*.pem")
        self.assertNotIn("allow", answer)

    def test_a_refusal_gives_the_seat_and_the_sitting_back(self):
        self.prepared()
        answer = self.refused("read", branch="work", path="src/app.py",
                              allow=["docs/**"], seat="clerk-1", sitting="42")
        self.assertEqual(answer["seat"], "clerk-1")
        self.assertEqual(answer["sitting"], "42")

    def test_submit_with_a_seat_and_a_sitting_writes_the_trailers(self):
        path = self.prepared()
        util.write(os.path.join(path, "docs/a.txt"), "alpha\ndelta\ncharlie\n")
        answer = self.ok("submit", branch="work", message="change one line",
                         seat="clerk-1", sitting="42")
        message = util.git(["log", "-1", "--format=%B", answer["commit"]], cwd=path)
        self.assertIn("Waymark-Seat: clerk-1", message)
        self.assertIn("Waymark-Sitting: 42", message)

    def test_the_other_tools_take_allow_and_ignore_it(self):
        answer = self.ok("prepare", branch="work", allow=["docs/**"],
                         seat="clerk-1", sitting="42")
        self.assertTrue(answer["created"])
        self.assertEqual(self.ok("status", branch="work", allow=[])["dirty"], 0)


class TestInput(BenchCase):

    def test_an_unknown_repository_is_refused(self):
        answer, refused = tools.call(self.bench, "status", {"repo": "ghost", "branch": "work"})
        self.assertTrue(refused)
        self.assertEqual(answer["refused"], "repo")

    def test_a_repository_named_as_the_forge_spells_it_is_served(self):
        # the engine names a repository owner/name, and the clone lives
        # under <data_dir>/<owner>/<name>/
        bench = Bench(util.make_config(self.root, self.clone_url, name="ckopsa/demo"))
        answer, refused = tools.call(bench, "prepare", {"repo": "ckopsa/demo", "branch": "work"})
        self.assertFalse(refused, answer)
        self.assertEqual(answer["repo"], "ckopsa/demo")
        self.assertTrue(os.path.isdir(os.path.join(bench.config.data_dir, "ckopsa", "demo", "bare.git")))
        for bad in ("a/b/c", "../demo", "a/../b", "/demo", "demo/"):
            answer, refused = tools.call(bench, "status", {"repo": bad, "branch": "work"})
            self.assertTrue(refused, bad)
            self.assertEqual(answer["refused"], "repo")

    def test_an_unknown_tool_is_refused(self):
        answer, refused = tools.call(self.bench, "explode", {})
        self.assertTrue(refused)
        self.assertEqual(answer["refused"], "unknown_tool")


class TestEnroll(BenchCase):
    """The engine puts a repository on the rig, and takes it off again."""

    def repos_file(self):
        return os.path.join(self.config.data_dir, "repos.json")

    def file_entries(self):
        """Gives the entries of repos.json."""
        with open(self.repos_file(), "r", encoding="utf-8") as handle:
            return json.load(handle)["repos"]

    def fresh(self):
        """Gives a second bench over the same data directory."""
        return Bench(util.make_config(self.root, self.clone_url))

    def test_enroll_clones_and_a_new_bench_serves_the_repository(self):
        answer = self.ok("enroll", repo="acme/second", clone_url=self.clone_url)
        self.assertTrue(answer["cloned"])
        self.assertEqual(answer["name"], "acme/second")
        self.assertEqual(answer["default_branch"], "main")
        self.assertEqual(answer["deny"], config_module.DEFAULT_DENY)
        self.assertIsNone(answer["land"])
        self.assertTrue(os.path.isdir(os.path.join(answer["bare"], "objects")))
        self.assertIn("acme/second", self.file_entries())
        bench = self.fresh()
        made, refused = tools.call(bench, "prepare", {"repo": "acme/second", "branch": "work"})
        self.assertFalse(refused, made)
        self.assertTrue(made["created"])
        state, refused = tools.call(bench, "status", {"repo": "acme/second", "branch": "work"})
        self.assertFalse(refused, state)
        self.assertEqual(state["dirty"], 0)

    def test_enroll_again_replaces_the_entry_and_keeps_the_clone(self):
        self.ok("enroll", repo="second", clone_url=self.clone_url)
        again = self.ok("enroll", repo="second", clone_url=self.clone_url,
                        default_branch="trunk", deny=["*.txt"])
        self.assertFalse(again["cloned"])
        self.assertEqual(again["default_branch"], "trunk")
        self.assertEqual(again["deny"], ["*.txt"])
        entry = self.bench.config.repo("second")
        self.assertEqual(entry.default_branch, "trunk")
        self.assertEqual(self.file_entries()["second"]["deny"], ["*.txt"])

    def test_a_clone_that_fails_writes_no_entry(self):
        answer = self.refused("enroll", repo="ghost", clone_url="file:///nonexistent/x.git",
                              seat="clerk-1", sitting="42")
        self.assertEqual(answer["refused"], "clone_failed")
        self.assertEqual(answer["repo"], "ghost")
        self.assertTrue(answer["reason"])
        self.assertEqual(answer["seat"], "clerk-1")
        self.assertEqual(answer["sitting"], "42")
        self.assertNotIn("ghost", self.bench.config.repos)
        self.assertFalse(os.path.exists(self.repos_file()))

    def test_repos_gives_the_source_of_each_entry(self):
        self.ok("enroll", repo="second", clone_url=self.clone_url)
        answer = self.ok("repos")
        self.assertEqual([item["name"] for item in answer["repos"]], ["demo", "second"])
        items = {item["name"]: item for item in answer["repos"]}
        self.assertEqual(items["second"]["source"], "file")
        self.assertTrue(items["second"]["bare_exists"])
        self.assertEqual(items["second"]["clone_url"], self.clone_url)
        self.assertEqual(items["demo"]["source"], "config")
        self.assertFalse(items["demo"]["bare_exists"])
        self.prepared()
        items = {item["name"]: item for item in self.ok("repos")["repos"]}
        self.assertTrue(items["demo"]["bare_exists"])

    def test_unenroll_keeps_the_clone_and_refuses_a_repository_of_bench_json(self):
        first = self.ok("enroll", repo="second", clone_url=self.clone_url)
        answer = self.ok("unenroll", repo="second")
        self.assertEqual(answer["repo"], "second")
        self.assertEqual(answer["kept"], first["bare"])
        self.assertTrue(os.path.isdir(os.path.join(answer["kept"], "objects")))
        self.assertNotIn("second", self.bench.config.repos)
        self.assertEqual(self.file_entries(), {})
        again = self.ok("enroll", repo="second", clone_url=self.clone_url)
        self.assertFalse(again["cloned"])
        refusal = self.refused("unenroll", repo="demo")
        self.assertEqual(refusal["refused"], "config_repo")
        self.assertIn("demo", self.bench.config.repos)

    def test_an_entry_of_the_file_wins_over_bench_json(self):
        self.ok("enroll", repo="demo", clone_url=self.clone_url, deny=["*.md"])
        bench = self.fresh()
        entry = bench.config.repo("demo")
        self.assertEqual(entry.deny, ["*.md"])
        self.assertEqual(entry.source, "file")
        made, refused = tools.call(bench, "prepare", {"repo": "demo", "branch": "work"})
        self.assertFalse(refused, made)
        answer, refused = tools.call(bench, "read",
                                     {"repo": "demo", "branch": "work", "path": "README.md"})
        self.assertTrue(refused, answer)
        self.assertEqual(answer["refused"], "denied")
        self.assertEqual(answer["pattern"], "*.md")

    def test_bench_json_can_hold_the_data_directory_only(self):
        spec = {"data_dir": os.path.join(self.root, "data")}
        bench = Bench(config_module.from_dict(spec))
        self.assertEqual(bench.config.names(), [])
        answer, refused = tools.call(bench, "enroll",
                                     {"repo": "only", "clone_url": self.clone_url})
        self.assertFalse(refused, answer)
        self.assertEqual(Bench(config_module.from_dict(spec)).config.names(), ["only"])


class TestCheck(BenchCase):
    def test_an_unbalanced_form_gives_its_line_and_col(self):
        path = self.prepared()
        util.write(os.path.join(path, "src/core.clj"),
                   "(ns demo.core)\n\n(defn f [x]\n  (+ x 1)))\n")
        answer = self.ok("check", branch="work")
        self.assertFalse(answer["ok"])
        finding = answer["findings"][0]
        self.assertEqual((finding["path"], finding["line"], finding["col"]), ("src/core.clj", 4, 11))

    def test_a_mismatched_close_names_the_open_form_line(self):
        path = self.prepared()
        util.write(os.path.join(path, "src/core.clj"), "(ns demo.core)\n\n(defn f [x\n  x)\n")
        answer = self.ok("check", branch="work")
        finding = answer["findings"][0]
        self.assertEqual((finding["line"], finding["col"]), (4, 4))
        self.assertIn("line 3", finding["message"])

    def test_strings_regexes_chars_and_comments_hold_no_forms(self):
        path = self.prepared()
        util.write(os.path.join(path, "src/core.clj"),
                   "(ns demo.core)\n(def s \")\")\n(def r #\"(\")\n(def c \\()\n; a ( comment\n")
        answer = self.ok("check", branch="work")
        self.assertEqual([f for f in answer["findings"] if f["path"] == "src/core.clj"
                          and not f["message"].startswith("clj-kondo")], [])

    def test_a_python_syntax_error_is_found(self):
        path = self.prepared()
        util.write(os.path.join(path, "src/app.py"), "def broken(:\n    pass\n")
        answer = self.ok("check", branch="work")
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["findings"][0]["path"], "src/app.py")
        self.assertEqual(answer["findings"][0]["line"], 1)

    def test_a_clean_change_answers_ok(self):
        path = self.prepared()
        util.write(os.path.join(path, "src/app.py"), "VALUE = 1\n")
        util.write(os.path.join(path, "notes.txt"), "a note\n")
        answer = self.ok("check", branch="work")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["findings"], [])
        self.assertEqual(answer["skipped"], ["notes.txt"])
        self.assertEqual(self.ok("status", branch="work")["dirty"], 2)

    def test_a_shell_syntax_error_is_found(self):
        path = self.prepared()
        util.write(os.path.join(path, ".claude/hooks/close.sh"),
                   "#!/bin/bash\nif true; then\n  echo hi\n")
        answer = self.ok("check", branch="work")
        self.assertFalse(answer["ok"])
        finding = answer["findings"][0]
        self.assertEqual(finding["path"], ".claude/hooks/close.sh")
        self.assertIn("syntax error", finding["message"])
        self.assertGreaterEqual(finding["line"], 1)

    def test_a_clean_shell_script_answers_ok(self):
        path = self.prepared()
        util.write(os.path.join(path, "scripts/run.sh"), "#!/bin/bash\nif true; then\n  echo hi\nfi\n")
        answer = self.ok("check", branch="work")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["findings"], [])
        self.assertEqual(answer["skipped"], [])

    def test_paths_limit_the_check(self):
        path = self.prepared()
        util.write(os.path.join(path, "src/app.py"), "def broken(:\n")
        answer = self.ok("check", branch="work", paths=["README.md"])
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["skipped"], ["README.md"])

    CHECK_OUTPUT = ("checking 3 kinds\n"
                    "✗ [unref'd-ids] ticket.parent: names an id no kind defines\n"
                    "  ✗ [missing-label] task: has no label\n"
                    "done\n")

    def test_the_check_step_gives_a_finding_for_each_cross_line(self):
        path = self.prepared()
        util.write(os.path.join(path, "check.out"), self.CHECK_OUTPUT)
        self.config.repo("demo").check = config_module.check_from_dict(
            "demo", {"command": "cat check.out; exit 1"})
        answer = self.ok("check", branch="work")
        self.assertFalse(answer["ok"])
        self.assertEqual([(f["kind"], f["field"], f["sentence"]) for f in answer["findings"]],
                         [("unref'd-ids", "ticket.parent", "names an id no kind defines"),
                          ("missing-label", "task", "has no label")])

    def test_a_failed_check_step_with_no_cross_line_gives_its_tail(self):
        self.prepared()
        self.config.repo("demo").check = config_module.check_from_dict(
            "demo", {"command": "echo could not start; exit 3"})
        answer = self.ok("check", branch="work")
        self.assertFalse(answer["ok"])
        self.assertEqual(len(answer["findings"]), 1)
        self.assertIn("could not start", answer["findings"][0]["output"])

    def test_the_check_prepare_runs_before_the_command(self):
        self.prepared()
        self.config.repo("demo").check = config_module.check_from_dict(
            "demo", {"prepare": "echo '✗ [prep] order: prepare ran first' > check.out",
                     "command": "cat check.out; exit 1"})
        answer = self.ok("check", branch="work")
        self.assertEqual([(f["kind"], f["field"], f["sentence"]) for f in answer["findings"]],
                         [("prep", "order", "prepare ran first")])

    def test_a_failed_check_prepare_is_one_finding_and_the_command_does_not_run(self):
        path = self.prepared()
        self.config.repo("demo").check = config_module.check_from_dict(
            "demo", {"prepare": "echo no dependencies; exit 4",
                     "command": "touch command.ran; echo '✗ [cmd] x: ran'; exit 1"})
        answer = self.ok("check", branch="work")
        self.assertFalse(answer["ok"])
        self.assertEqual(len(answer["findings"]), 1)
        self.assertIn("prepare", answer["findings"][0]["sentence"])
        self.assertIn("no dependencies", answer["findings"][0]["output"])
        self.assertFalse(os.path.exists(os.path.join(path, "command.ran")))

    def test_the_check_prepare_survives_the_round_trip(self):
        step = config_module.check_from_dict("demo", {"prepare": "make deps", "command": "make check"})
        again = config_module.check_from_dict("demo", step.to_dict())
        self.assertEqual((again.prepare, again.command), ("make deps", "make check"))
        with self.assertRaises(config_module.ConfigError):
            config_module.check_from_dict("demo", {"prepare": 3, "command": "make check"})

    def test_a_passing_check_step_answers_ok(self):
        self.prepared()
        self.config.repo("demo").check = config_module.check_from_dict(
            "demo", {"command": "echo all kinds assemble"})
        answer = self.ok("check", branch="work")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["findings"], [])

    def test_a_slow_check_step_answers_pending_then_finished(self):
        self.prepared()
        self.config.repo("demo").check = config_module.check_from_dict(
            "demo", {"command": "sleep 1; echo '✗ [slow] step: took its time'; exit 1"})
        first = self.ok("check", branch="work", wait=0)
        self.assertTrue(first["pending"], first)
        self.assertIsNone(first["ok"])
        answer = self.ok("check", branch="work", check_id=first["check_id"], wait=20)
        self.assertFalse(answer["pending"], answer)
        self.assertEqual((answer["state"], answer["exit_code"]), ("finished", 1))
        self.assertEqual([f["kind"] for f in answer["findings"]], ["slow"])

    def test_a_check_step_past_its_timeout_answers_timed_out(self):
        self.prepared()
        self.config.repo("demo").check = config_module.check_from_dict(
            "demo", {"command": "sleep 30", "timeout": 1})
        answer = self.ok("check", branch="work", wait=20)
        self.assertFalse(answer["pending"], answer)
        self.assertEqual(answer["state"], "timed_out")
        self.assertFalse(answer["ok"])

    def test_a_repo_with_no_check_step_answers_in_one_call(self):
        self.prepared()
        answer = self.ok("check", branch="work")
        self.assertTrue(answer["ok"], answer)
        self.assertNotIn("check_id", answer)
        self.assertNotIn("pending", answer)

    def test_an_unknown_check_id_is_refused(self):
        self.prepared()
        answer = self.refused("check", branch="work", check_id="nope")
        self.assertEqual(answer["refused"], "unknown_check")

    ODD_LET = "(ns demo.core)\n\n(defn f []\n  (let [x] x))\n"

    def test_check_says_when_clj_kondo_is_not_installed(self):
        path = self.prepared()
        util.write(os.path.join(path, "src/core.clj"), self.ODD_LET)
        with mock.patch.object(tools.shutil, "which", return_value=None):
            answer = self.ok("check", branch="work")
        self.assertTrue(answer["ok"])
        self.assertIn("clj-kondo not installed", answer["unavailable"][0])

    BROKEN_WORKFLOW = "name: tests\non:\n  push: [main\njobs: {}\n"

    def test_check_says_when_no_yaml_parser_is_on_the_rig(self):
        path = self.prepared()
        util.write(os.path.join(path, ".github/workflows/ci.yml"), self.BROKEN_WORKFLOW)
        with mock.patch.dict(sys.modules, {"yaml": None}):
            answer = self.ok("check", branch="work")
        self.assertTrue(answer["ok"])
        self.assertNotIn(".github/workflows/ci.yml", answer["skipped"])
        self.assertIn("no YAML parser", answer["unavailable"][0])

    @unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML is not installed")
    def test_a_github_yaml_parse_error_is_found(self):
        path = self.prepared()
        util.write(os.path.join(path, ".github/workflows/ci.yml"), self.BROKEN_WORKFLOW)
        util.write(os.path.join(path, ".github/workflows/ok.yaml"), "name: ok\non: push\n")
        answer = self.ok("check", branch="work")
        self.assertFalse(answer["ok"])
        self.assertEqual([f["path"] for f in answer["findings"]], [".github/workflows/ci.yml"])
        self.assertGreater(answer["findings"][0]["line"], 1)

    def test_check_finds_an_added_hosted_runs_on(self):
        path = self.prepared()
        util.write(os.path.join(path, ".github/workflows/ci.yml"),
                   "name: ci\non: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps: []\n")
        answer = self.ok("check", branch="work")
        self.assertFalse(answer["ok"])
        finding = answer["findings"][0]
        self.assertEqual((finding["path"], finding["line"]), (".github/workflows/ci.yml", 5))
        self.assertIn("waymark", finding["message"])
        self.assertEqual(answer["hosted_runs_on"], [
            {"path": ".github/workflows/ci.yml", "line": 5, "text": "runs-on: ubuntu-latest",
             "allowed": False}])

    @unittest.skipUnless(shutil.which("clj-kondo"), "clj-kondo is not on PATH")
    def test_clj_kondo_names_an_unbalanced_let_binding(self):
        path = self.prepared()
        util.write(os.path.join(path, "src/core.clj"), self.ODD_LET)
        answer = self.ok("check", branch="work")
        self.assertFalse(answer["ok"])
        self.assertTrue([f for f in answer["findings"]
                         if f["path"] == "src/core.clj" and f["message"].startswith("clj-kondo")])


class TestLogMarkers(unittest.TestCase):

    def test_a_kaocha_exception_info_line_is_marked(self):
        self.assertTrue(tools.LOG_MARKERS.search("ExceptionInfo: the tier was sniffed {:tier :gold}"))
        self.assertFalse(tools.LOG_MARKERS.search("waymark10.shard-test/case-1 passed"))


if __name__ == "__main__":
    unittest.main()
