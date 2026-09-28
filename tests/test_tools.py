"""One test for each tool, and one test for each refusal."""

import json
import os
import shutil
import tempfile
import unittest

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
        with open(os.path.join(path, "docs/a.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "alpha\nbravo\ncharlie\n")

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

    def test_pull_from_head_moves_to_the_remote_branch(self):
        self.prepared()
        other = util.clone(self.root, self.clone_url)
        util.push_change(other, "work", "docs/head.txt", "from the remote branch\n")
        answer = self.ok("pull", branch="work", **{"from": "head"})
        self.assertTrue(answer["merged"])
        self.assertTrue(os.path.isfile(os.path.join(self.worktree(), "docs/head.txt")))


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

    def test_paths_limit_the_check(self):
        path = self.prepared()
        util.write(os.path.join(path, "src/app.py"), "def broken(:\n")
        answer = self.ok("check", branch="work", paths=["README.md"])
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["skipped"], ["README.md"])


if __name__ == "__main__":
    unittest.main()
