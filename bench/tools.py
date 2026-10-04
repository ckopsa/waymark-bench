"""The thirty-five tools of the bench.

Each tool is a function over a Bench object. Each function validates its
input, applies the caps, and gives a dictionary. A refusal is a Refusal
exception with a name and its data. No tool gives a stack trace.
"""

import calendar
import difflib
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

from . import config as config_module, forge, git, landing as landing_module, settings, symbols


DEFAULT_MAX_BYTES = 16384
CEILING_MAX_BYTES = 65536
DEFAULT_MAX_MATCHES = 200
# A read answers DEFAULT_LIMIT lines at most: a larger limit is cut to it, with READ_NOTE.
DEFAULT_LIMIT = 120
CEILING_LIMIT = 1000000
READ_NOTE = "for a definition use bench__read_symbol; for more, page with offset"
DEFAULT_DEPTH = 2
CEILING_DEPTH = 12
DEFAULT_HISTORY = 20
CEILING_HISTORY = 100
# submit starts the landing and answers at once unless it is asked to
# wait. A landing runs a repo's whole test suite, and the engine that
# brokers a seat's calls gives up on any call after 30 seconds and marks
# the server dark - so a default that waited cut every seat off the
# rig. The way to follow a landing is status or feedback. A wait asked
# for is cut to 28 seconds, under the gate's 30, as test_result's is.
DEFAULT_WAIT = 0
CEILING_WAIT = 28
# test answers at once with the run it dispatched, and test_result reads that
# run for a bounded wait. The engine's gate gives every tools/call 30 seconds,
# so the wait is the setting BENCH_TEST_WAIT (25 by default) and never past 28.
CEILING_TEST_WAIT = 28
TEST_POLL_SECONDS = 5
# test looks for the run it started about fifteen seconds: 8 reads, 2 s apart.
TEST_FIND_TRIES = 8
TEST_FIND_SECONDS = 2
# a dispatch's time is stamped this early, for the forge's clock
TEST_SKEW_SECONDS = 10
# the failing tests of a red run, in this many bytes at most
TEST_FAILURE_BYTES = 4096
TEST_LINE_CHARS = 300
# a failure keeps its header and the lines after it, in this many lines and characters
TEST_FAILURE_LINES = 20
TEST_FAILURE_CHARS = 2000
# clojure.test's FAIL in (test) (file:line), or kaocha's FAIL in ns/test (file:line)
FAILED_TEST = re.compile(r"(?:FAIL|ERROR) in (?:\(([^)]+)\)|([^\s(]+))")
TEST_PREFIX = "bench-test/"
# a Clojure test namespace (dotted, its last segment ending in -test), or ns/test-name
TEST_SELECT = re.compile(r"^(?:[A-Za-z_][\w-]*\.)+[A-Za-z_][\w-]*-test"
                         r"(?:/[A-Za-z_*+!?<>=-][\w*+!?<>='-]*)?$")
_sleep = time.sleep
_clock = time.monotonic
PROTECTED_PREFIXES = (".github/", ".claude/")
BRANCH_CHARS = re.compile(r"^[A-Za-z0-9._/-]+$")
# A repository name is the key in bench.json. The engine names a
# repository as the forge spells it, owner/name, and the clone then
# lives under <data_dir>/<owner>/<name>/. One slash at most, and no
# part that walks upward.
REPO_CHARS = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*(/[A-Za-z0-9_-][A-Za-z0-9._-]*)?$")
GREP_LINE = re.compile(r"^(?P<path>.+?)[-:](?P<line>\d+)[-:](?P<text>.*)$")
# A GitHub Actions secret's name: letters, digits and _, not opening with a digit.
SECRET_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# A workflow is its file name under .github/workflows, or its numeric id.
WORKFLOW_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
# dispatch looks for the run it started at most ten seconds: 5 reads, 2 s apart.
DISPATCH_FIND_TRIES = 5
DISPATCH_FIND_SECONDS = 2


class Refusal(Exception):
    """A refusal with a name and its data."""

    def __init__(self, name, **data):
        self.data = {"refused": name}
        self.data.update(data)
        Exception.__init__(self, name)


# ---------------------------------------------------------------- helpers


def _int(args, key, default, low, high):
    value = args.get(key, default)
    if value is None:
        value = default
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise Refusal("input", field=key, reason="a whole number is necessary")
    if value < low:
        value = low
    if value > high:
        value = high
    return value


def _text(args, key, required=False, default=None):
    value = args.get(key, default)
    if value is None or value == "":
        if required:
            raise Refusal("input", field=key, reason="a value is necessary")
        return default
    if not isinstance(value, str):
        raise Refusal("input", field=key, reason="a text is necessary")
    return value


def _globs(args, key):
    """Gives the list of globs, or None when the argument is not given."""
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, list):
        raise Refusal("input", field=key, reason="a list of globs is necessary")
    for item in value:
        if not isinstance(item, str):
            raise Refusal("input", field=key, reason="each glob is a text")
    return list(value)


def marks_of(args):
    """Gives the seat and the sitting of the call, when they are given."""
    marks = {}
    for key in ("seat", "sitting"):
        value = args.get(key)
        if isinstance(value, str) and value:
            marks[key] = value
    return marks


def log_call(name, args, refused=None):
    """Writes one short line for the call to the standard error."""
    fields = ["tool=%s" % name]
    for key in ("repo", "branch", "seat", "sitting"):
        value = args.get(key)
        if isinstance(value, str) and value:
            fields.append("%s=%s" % (key, value))
    if refused:
        fields.append("refused=%s" % refused)
    print("bench call " + " ".join(fields), file=sys.stderr)


def check_branch(name):
    """Validates a branch name. See R-3."""
    if not name or not isinstance(name, str):
        raise Refusal("branch", branch=name, reason="a branch name is necessary")
    if ".." in name or name.startswith("-") or name.startswith("/") or name.endswith("/"):
        raise Refusal("branch", branch=name, reason="the name has a part that is not permitted")
    if "//" in name or name.endswith(".lock") or "@{" in name or name == "@":
        raise Refusal("branch", branch=name, reason="the name has a part that is not permitted")
    if not BRANCH_CHARS.match(name):
        raise Refusal("branch", branch=name, reason="use only A-Z a-z 0-9 . _ / -")
    return name


def deny_pattern(path, patterns):
    """Gives the deny glob that matches the path, or None."""
    base = os.path.basename(path)
    for pattern in patterns or []:
        if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(base, pattern):
            return pattern
        if pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:]):
            return pattern
    return None


def allow_pattern(path, patterns, directory=False):
    """Gives the allow glob that admits the path, or None.

    A directory is admitted when a glob descends into it.
    """
    pattern = deny_pattern(path, patterns)
    if pattern:
        return pattern
    if directory:
        for item in patterns or []:
            if item.startswith(path + "/"):
                return item
    return None


def clean_path(path):
    """Normalizes a repository path. Gives a path with / separators."""
    if path is None:
        raise Refusal("input", field="path", reason="a path is necessary")
    if not isinstance(path, str) or path == "":
        raise Refusal("input", field="path", reason="a path is necessary")
    text = path.replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    if text.startswith("/") or os.path.isabs(text):
        raise Refusal("denied", path=path, reason="the path is outside the worktree")
    return text.rstrip("/") or "."


def is_protected(path):
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in PROTECTED_PREFIXES)


def cap_text(text, max_bytes):
    """Cuts a text to max_bytes. Gives (text, dropped)."""
    raw = text.encode("utf-8", "replace")
    if len(raw) <= max_bytes:
        return text, 0
    return raw[:max_bytes].decode("utf-8", "ignore"), len(raw) - max_bytes


def cap_items(items, max_bytes, size_of):
    """Keeps items while the total size is below max_bytes."""
    kept = []
    used = 0
    dropped = 0
    over = False
    for item in items:
        size = size_of(item)
        if over or used + size > max_bytes:
            over = True
            dropped += size
            continue
        kept.append(item)
        used += size
    return kept, dropped


# ------------------------------------------------------------------ bench


class Bench:
    """The bench: the clones, the worktrees and the locks."""

    def __init__(self, config):
        self.config = config
        self._locks = {}
        self._guard = threading.Lock()
        # bench.json first, then the repositories that the engine enrolled.
        config.read_repos()
        self.landings = landing_module.Landings(self)
        # The last credential check of each repository, by name.
        self.credentials = {}
        # The cleaned lines of the job logs read in the last hour, by
        # (repository, run, job): (the time they came, the lines).
        self.logs = {}
        # The check steps that run in the background, by check_id.
        self.checks = {}

    def check_credentials(self):
        """Checks the forge credential of every repository. The rig runs it
        at start."""
        for name in self.config.names():
            check_credential(self, self.config.repos[name])
        return dict(self.credentials)

    def no_landing(self, repo, branch):
        """Refuses while a landing of the branch runs."""
        if self.landings.running(repo, branch):
            raise Refusal("landing_running", repo=repo.name, branch=branch,
                          remedy="wait for the landing: call status, or feedback")

    def lock(self, name):
        """Gives the lock of one repository. One git operation at a time."""
        with self._guard:
            if name not in self._locks:
                self._locks[name] = threading.Lock()
            return self._locks[name]

    def repo(self, name):
        if not name or not isinstance(name, str) or not REPO_CHARS.match(name):
            raise Refusal("repo", repo=name, reason="the repository name is not permitted")
        if name not in self.config.repos:
            raise Refusal("repo", repo=name, reason="the repository is not on the bench",
                          known=self.config.names())
        return self.config.repos[name]

    def repo_dir(self, name):
        return os.path.join(self.config.data_dir, name)

    def bare_dir(self, name):
        return os.path.join(self.repo_dir(name), "bare.git")

    def bare_exists(self, name):
        """Tells if the bare clone of one repository is on the disk."""
        return os.path.isdir(os.path.join(self.bare_dir(name), "objects"))

    def wt_dir(self, name, branch):
        return os.path.join(self.repo_dir(name), "wt", *branch.split("/"))

    def meta_path(self, name):
        return os.path.join(self.repo_dir(name), "meta.json")

    def meta_read(self, name):
        try:
            with open(self.meta_path(name), "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    def meta_write(self, name, data):
        os.makedirs(self.repo_dir(name), exist_ok=True)
        tmp = self.meta_path(name) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1, sort_keys=True)
        os.replace(tmp, self.meta_path(name))

    def base_of(self, repo, branch):
        """Gives the base branch of a worktree."""
        meta = self.meta_read(repo.name).get(branch) or {}
        return meta.get("base") or repo.default_branch

    def ensure_bare(self, repo):
        """Makes the bare clone one time. Gives its path."""
        bare = self.bare_dir(repo.name)
        if self.bare_exists(repo.name):
            return bare
        os.makedirs(os.path.dirname(bare), exist_ok=True)
        git.run(["clone", "--bare", repo.clone_url, bare], timeout=600)
        git.run(["config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"], cwd=bare)
        git.run(["fetch", "--prune", "origin"], cwd=bare, timeout=600)
        return bare

    def fetch(self, repo):
        bare = self.ensure_bare(repo)
        git.run(["fetch", "--prune", "origin"], cwd=bare, timeout=600)
        return bare

    def worktree(self, repo, branch, must_exist=True):
        """Gives the path of a worktree."""
        path = self.wt_dir(repo.name, branch)
        if must_exist and not os.path.isdir(path):
            raise Refusal("no_worktree", repo=repo.name, branch=branch,
                          remedy="call prepare first")
        return path

    # ---------------------------------------------------------- path rules

    def resolve(self, repo, worktree, path, for_write=False, allow_protected=False, allow=None):
        """Gives the absolute path inside the worktree, or refuses.

        The deny globs come first. Then allow, when the call gives it:
        a path that no allow glob admits is refused.
        """
        rel = clean_path(path)
        if rel == ".":
            return worktree, "."
        if rel == ".git" or rel.startswith(".git/"):
            raise Refusal("denied", path=rel, reason="the git directory is not served")
        pattern = deny_pattern(rel, repo.deny)
        if pattern:
            raise Refusal("denied", path=rel, pattern=pattern)
        root = os.path.realpath(worktree)
        full = os.path.realpath(os.path.join(root, rel))
        if full != root and not full.startswith(root + os.sep):
            raise Refusal("denied", path=rel, reason="the path is outside the worktree")
        if allow is not None and not allow_pattern(rel, allow, os.path.isdir(full)):
            raise Refusal("denied", path=rel, allow=list(allow))
        if for_write and is_protected(rel) and not allow_protected:
            raise Refusal("protected", path=rel,
                          reason="a write under .github/ or .claude/ needs this seat's scope to name "
                                 "the path in its bench.edit filter")
        return full, rel


# ------------------------------------------------------------ git answers


def status_paths(worktree):
    """Gives the changed paths from git status --porcelain."""
    text = git.out(["status", "--porcelain", "-z"], cwd=worktree)
    parts = [item for item in text.split("\0") if item]
    paths = []
    index = 0
    while index < len(parts):
        entry = parts[index]
        index += 1
        if len(entry) < 4:
            continue
        code = entry[:2]
        name = entry[3:]
        if code[0] in "RC":
            # A rename gives the old name as the next part.
            if index < len(parts):
                index += 1
        paths.append(name)
    return paths


def head_of(worktree):
    code, text, _ = git.run(["rev-parse", "HEAD"], cwd=worktree, check=False)
    if code != 0:
        return None
    return text.strip()


def lag_of(bare, worktree, branch, base_head):
    """Gives (behind, behind_remote): the commits the worktree lacks.

    behind counts against the base head, as status does. behind_remote
    counts against the remote branch, and is None when the branch is
    not on the remote. prepare fetches but does not move a worktree, so
    these two are how a caller learns that it reads old code.
    """
    counts = git.line(["rev-list", "--left-right", "--count", "%s...HEAD" % base_head],
                      cwd=worktree)
    behind = int((counts.split() + ["0"])[0])
    remote = "refs/remotes/origin/" + branch
    if not git.ref_exists(remote, cwd=bare):
        return behind, None
    behind_remote = int(git.line(["rev-list", "--count", "HEAD.." + remote], cwd=worktree))
    return behind, behind_remote


def lag_note(branch, base, behind, behind_remote):
    """Names the pull that brings a lagging worktree forward, or gives ""."""
    def commits(count):
        return "%d commit%s" % (count, "" if count == 1 else "s")
    if behind_remote:
        return "the worktree is %s behind origin/%s: use pull from head" % (
            commits(behind_remote), branch)
    if behind:
        return "the worktree is %s behind the base %s: pull from base merges it in" % (
            commits(behind), base)
    return ""


def base_ref_of(bare, base):
    """Gives the ref that holds the base head."""
    if git.ref_exists("refs/remotes/origin/" + base, cwd=bare):
        return "refs/remotes/origin/" + base
    if git.ref_exists(base, cwd=bare):
        return base
    raise Refusal("no_base", base=base, reason="the base branch is not in the clone")


def unmerged_paths(worktree):
    """Gives the paths a merge left unmerged in the worktree."""
    return [name for name in
            git.out(["diff", "--name-only", "--diff-filter=U"], cwd=worktree).splitlines()
            if name]


def marked_paths(worktree, paths):
    """Gives the paths that still hold a conflict marker."""
    marked = []
    for name in paths:
        try:
            with open(os.path.join(worktree, name), encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        if any(line.startswith(("<<<<<<<", ">>>>>>>")) for line in lines):
            marked.append(name)
    return marked


def conflict_ranges(worktree, paths):
    """Gives each path with the line ranges of its conflict markers, <<<<<<< to >>>>>>>."""
    found = []
    for name in paths:
        try:
            with open(os.path.join(worktree, name), encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        ranges, start = [], None
        for number, line in enumerate(lines, 1):
            if line.startswith("<<<<<<<"):
                start = number
            elif line.startswith(">>>>>>>") and start is not None:
                ranges.append({"start": start, "end": number})
                start = None
        if ranges:
            found.append({"path": name, "ranges": ranges})
    return found


def finish_merge(worktree):
    """Commits a merge in progress whose markers are gone. Gives the marked paths."""
    marked = marked_paths(worktree, unmerged_paths(worktree))
    if marked:
        return marked
    git.run(["add", "-A", "--", "."], cwd=worktree)
    code, text, err = git.run(["commit", "--no-edit"], cwd=worktree, check=False)
    if code != 0:
        raise Refusal("commit_failed", reason=(err or text).strip()[:400])
    return []


def undo_merge(worktree, before):
    """Puts a worktree back as it was before a merge that failed. Gives the paths it reset.

    It aborts the merge, then resets each path the merge left changed. A path
    that was dirty before the merge (in before) is left as it is.
    """
    git.run(["merge", "--abort"], cwd=worktree, check=False)
    wrote = [name for name in status_paths(worktree) if name not in before]
    for name in wrote:
        if git.run(["cat-file", "-e", "HEAD:" + name], cwd=worktree, check=False)[0] == 0:
            git.run(["checkout", "HEAD", "--", name], cwd=worktree, check=False)
            continue
        git.run(["rm", "--cached", "-q", "-r", "--ignore-unmatch", "--", name],
                cwd=worktree, check=False)
        full = os.path.join(worktree, name)
        if os.path.isdir(full) and not os.path.islink(full):
            shutil.rmtree(full, ignore_errors=True)
        elif os.path.lexists(full):
            os.remove(full)
    return wrote


def _ledger_path(worktree):
    """The worktree's file of the paths the edit tools wrote since the last submit."""
    return os.path.join(worktree, git.line(["rev-parse", "--git-path", "bench-written"],
                                           cwd=worktree))


def written_paths(worktree):
    """Gives the paths the edit tools wrote since the last submit, as a set."""
    try:
        with open(_ledger_path(worktree), "r", encoding="utf-8") as handle:
            return set(line for line in handle.read().split("\n") if line)
    except FileNotFoundError:
        return set()


def note_written(worktree, paths):
    """Adds paths to the worktree's ledger of what the edit tools wrote."""
    known = written_paths(worktree)
    fresh = sorted(set(name for name in paths if name and name not in known))
    if fresh:
        with open(_ledger_path(worktree), "a", encoding="utf-8") as handle:
            handle.write("".join(name + "\n" for name in fresh))


def clear_written(worktree):
    """Empties the ledger: a commit holds what the edit tools wrote."""
    try:
        os.remove(_ledger_path(worktree))
    except FileNotFoundError:
        pass


def _is_written(name, written):
    """True when the edit tools wrote name, or a path under it for an untracked directory."""
    if name in written:
        return True
    return name.endswith("/") and any(path.startswith(name) for path in written)


def drop_stray(repo, bare, worktree, branch):
    """Resets the stray paths of a worktree whose head is already pushed. Gives the paths it dropped.

    The head is pushed when it is origin/<branch> or an ancestor of it, so
    an uncommitted path the edit tools did not write is no seat's work: a
    failed pull left it. A path in the ledger of written paths is kept. It
    drops nothing when the branch is not on the remote, when the branch has
    commits the remote does not, or while a merge is in progress. An
    untracked path that matches a deny glob is kept.
    """
    dirty = status_paths(worktree)
    if not dirty or git.ref_exists("MERGE_HEAD", cwd=worktree):
        return []
    remote = "refs/remotes/origin/" + branch
    if not git.ref_exists(remote, cwd=bare):
        return []
    code = git.run(["merge-base", "--is-ancestor", "HEAD", remote], cwd=worktree, check=False)[0]
    if code != 0:
        return []
    written = written_paths(worktree)
    stray = [name for name in dirty if not _is_written(name, written)]
    if not stray:
        return []
    listed = git.out(["ls-tree", "-r", "-z", "--name-only", "HEAD", "--"] + stray, cwd=worktree)
    in_head = set(name for name in listed.split("\0") if name)
    if in_head:
        git.run(["checkout", "HEAD", "--"] + sorted(in_head), cwd=worktree)
    added = [name for name in stray if name not in in_head]
    if added:
        git.run(["rm", "-r", "-q", "--cached", "--ignore-unmatch", "--"] + added,
                cwd=worktree, check=False)
        keep = [arg for pattern in repo.deny for arg in ("-e", pattern)]
        git.run(["clean", "-fd"] + keep + ["--"] + added, cwd=worktree)
    left = set(status_paths(worktree))
    return [name for name in stray if name not in left]


# ------------------------------------------------------------------ tools


def prepare(bench, args):
    """Makes the clone, fetches, and makes the worktree. Idempotent."""
    repo = bench.repo(args.get("repo"))
    branch = check_branch(_text(args, "branch", required=True))
    base = _text(args, "base", default=repo.default_branch)
    check_branch(base)
    with bench.lock(repo.name):
        bare = bench.fetch(repo)
        path = bench.wt_dir(repo.name, branch)
        created = False
        if not os.path.isdir(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if git.ref_exists("refs/heads/" + branch, cwd=bare):
                git.run(["worktree", "add", "--force", path, branch], cwd=bare, timeout=600)
            elif git.ref_exists("refs/remotes/origin/" + branch, cwd=bare):
                git.run(["worktree", "add", "--force", "-b", branch, path,
                         "refs/remotes/origin/" + branch], cwd=bare, timeout=600)
            else:
                start = base_ref_of(bare, base)
                git.run(["worktree", "add", "--force", "-b", branch, path, start],
                        cwd=bare, timeout=600)
            created = True
            meta = bench.meta_read(repo.name)
            meta[branch] = {"base": base}
            bench.meta_write(repo.name, meta)
        else:
            base = bench.base_of(repo, branch)
        dropped = [] if created else drop_stray(repo, bare, path, branch)
        base_head = git.rev_parse(base_ref_of(bare, base), cwd=bare)
        behind, behind_remote = lag_of(bare, path, branch, base_head)
        set_up = (bench.meta_read(repo.name).get(branch) or {}).get("setup") == "ok"
    # outside the lock: npm ci takes a minute, and the repository's other
    # worktrees need not wait for it
    setup = None
    if repo.setup is not None:
        setup = {"ran": False, "ok": True} if set_up else run_setup(repo, branch, path)
        if setup["ran"]:
            with bench.lock(repo.name):
                meta = bench.meta_read(repo.name)
                meta.setdefault(branch, {"base": base})["setup"] = "ok" if setup["ok"] else "failed"
                bench.meta_write(repo.name, meta)
    with bench.lock(repo.name):
        dirty = status_paths(path)
        return {
            "repo": repo.name,
            "branch": branch,
            "base": base,
            "head": head_of(path),
            "base_head": base_head,
            "dirty": len(dirty),
            "dirty_paths": dirty[:100],
            "dropped": dropped[:100],
            "created": created,
            "default_branch": repo.default_branch,
            "behind": behind,
            "behind_remote": behind_remote,
            "note": lag_note(branch, base, behind, behind_remote),
            "setup": setup,
        }


def run_setup(repo, branch, path):
    """Runs the repository's setup step in a worktree, with the land env.
    Gives {ran, ok, exit_code, output}; a failure is reported, never raised."""
    result = run_step(repo, branch, path, repo.setup)
    result["output"] = result["output"][-2000:]
    return result


def run_step(repo, branch, path, step):
    """Runs one configured step in a worktree, with the land env. Gives
    {ran, ok, exit_code, output} with the whole scrubbed output."""
    env = dict(os.environ)
    if repo.land is not None:
        env.update(repo.land.env)
    env["BENCH_REPO"] = repo.name
    env["BENCH_BRANCH"] = branch
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.Popen(step.command, shell=True, cwd=path, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                start_new_session=True)
    except OSError as exc:
        return {"ran": True, "ok": False, "exit_code": 127, "output": str(exc)}
    try:
        output, _ = proc.communicate(timeout=step.timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, 9)
        except OSError:
            proc.kill()
        output, _ = proc.communicate()
        output = (output or "") + "\n(killed after %s seconds)" % step.timeout
        code = -1
    return {"ran": True, "ok": code == 0, "exit_code": code,
            "output": git.scrub(output or "")}


def status(bench, args):
    """Gives the state of a worktree. No fetch."""
    repo = bench.repo(args.get("repo"))
    branch = check_branch(_text(args, "branch", required=True))
    with bench.lock(repo.name):
        path = bench.worktree(repo, branch)
        bare = bench.bare_dir(repo.name)
        base = bench.base_of(repo, branch)
        base_head = git.rev_parse(base_ref_of(bare, base), cwd=path)
        head = head_of(path)
        paths = status_paths(path)
        counts = git.line(["rev-list", "--left-right", "--count", "%s...HEAD" % base_head], cwd=path)
        behind, ahead = (counts.split() + ["0", "0"])[:2]
        merging = git.ref_exists("MERGE_HEAD", cwd=path)
        markers = conflict_ranges(path, unmerged_paths(path)) if merging else None
    item = bench.landings.get(repo, branch)
    max_bytes = _int(args, "max_bytes", DEFAULT_MAX_BYTES, 1024, CEILING_MAX_BYTES)
    answer = {
        "repo": repo.name,
        "branch": branch,
        "head": head,
        "base": base,
        "base_head": base_head,
        "dirty": len(paths),
        "paths": paths[:500],
        "ahead": int(ahead),
        "behind": int(behind),
        "merge_in_progress": merging,
        "landing": item.view(max_bytes) if item else None,
    }
    if markers is not None:
        answer["markers"] = markers
    return answer


def _find_tree(bench, repo, worktree, args, max_bytes, allow=None):
    start_rel = clean_path(_text(args, "path", default=".") or ".")
    start, rel = bench.resolve(repo, worktree, start_rel, allow=allow)
    depth = _int(args, "depth", DEFAULT_DEPTH, 1, CEILING_DEPTH)
    if os.path.isfile(start):
        raise Refusal("is_file", path=rel, reason="this is a file, not a directory",
                      remedy="read it with the read tool: {\"path\": \"%s\"}" % rel)
    if not os.path.isdir(start):
        raise Refusal("not_found", path=rel, reason="the path is not a directory")
    entries = []
    base_depth = start.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(start):
        dirs[:] = sorted(name for name in dirs if name != ".git")
        level = root.rstrip(os.sep).count(os.sep) - base_depth
        if level >= depth:
            dirs[:] = []
        for name in dirs:
            full = os.path.join(root, name)
            path_rel = os.path.relpath(full, worktree).replace(os.sep, "/")
            if allow is not None and not allow_pattern(path_rel, allow, True):
                continue
            entries.append({"path": path_rel, "type": "dir"})
        for name in sorted(files):
            full = os.path.join(root, name)
            path_rel = os.path.relpath(full, worktree).replace(os.sep, "/")
            if deny_pattern(path_rel, repo.deny):
                continue
            if allow is not None and not allow_pattern(path_rel, allow):
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            entries.append({"path": path_rel, "type": "file", "size": size})
    entries.sort(key=lambda item: item["path"])
    kept, dropped = cap_items(entries, max_bytes, lambda item: len(item["path"]) + 24)
    return {"mode": "tree", "path": rel, "depth": depth, "entries": kept, "dropped": dropped}


def _find_glob(bench, repo, worktree, args, max_bytes, allow=None):
    pattern = _text(args, "pattern", required=True)
    text = git.out(["ls-files", "--cached", "--others", "--exclude-standard"], cwd=worktree)
    paths = []
    for name in text.splitlines():
        if not name:
            continue
        if deny_pattern(name, repo.deny):
            continue
        if allow is not None and not allow_pattern(name, allow):
            continue
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(os.path.basename(name), pattern):
            paths.append(name)
    kept, dropped = cap_items(sorted(paths), max_bytes, lambda item: len(item) + 4)
    return {"mode": "glob", "pattern": pattern, "paths": kept, "dropped": dropped}


def _find_grep(bench, repo, worktree, args, max_bytes, allow=None):
    pattern = _text(args, "pattern", required=True)
    context = _int(args, "context", 0, 0, 10)
    max_matches = _int(args, "max_matches", DEFAULT_MAX_MATCHES, 1, 2000)
    scope = []
    if args.get("path"):
        _, rel = bench.resolve(repo, worktree, args.get("path"), allow=allow)
        scope = ["--", rel]
    # Perl syntax, because it is the syntax a caller writes. Without -P
    # git grep reads a basic regex, where `a|b` looks for a literal bar
    # and `(?i)` for a literal paren, and both answer no matches rather
    # than an error. A triage seat read that silence as "pcore has no
    # Allvue fields" and routed a ticket to the wrong desk.
    syntax = ["-P"] + (["-i"] if args.get("ignore_case") else [])
    count_args = ["grep", "-I", "--untracked", "--no-color", "-c"] + syntax + ["-e", pattern] + scope
    code, text, err = git.run(count_args, cwd=worktree, check=False)
    if code not in (0, 1):
        raise Refusal("grep", pattern=pattern, reason=(err or text).strip()[:400])
    files = []
    for row in text.splitlines():
        if ":" not in row:
            continue
        name, _, count = row.rpartition(":")
        if deny_pattern(name, repo.deny):
            continue
        if allow is not None and not allow_pattern(name, allow):
            continue
        try:
            files.append({"path": name, "count": int(count)})
        except ValueError:
            continue
    line_args = ["grep", "-n", "-I", "--untracked", "--no-color"] + syntax
    if context:
        line_args += ["-C", str(context)]
    line_args += ["-e", pattern] + scope
    code, text, err = git.run(line_args, cwd=worktree, check=False)
    lines = []
    for row in text.splitlines():
        if row == "--":
            continue
        match = GREP_LINE.match(row)
        if not match:
            continue
        name = match.group("path")
        if deny_pattern(name, repo.deny):
            continue
        if allow is not None and not allow_pattern(name, allow):
            continue
        lines.append({"path": name, "line": int(match.group("line")),
                      "text": match.group("text")[:400]})
    dropped = 0
    if len(lines) > max_matches:
        for item in lines[max_matches:]:
            dropped += len(item["text"]) + len(item["path"]) + 16
        lines = lines[:max_matches]
    kept, dropped_bytes = cap_items(
        lines, max_bytes, lambda item: len(item["text"]) + len(item["path"]) + 16)
    return {
        "mode": "grep",
        "pattern": pattern,
        "files": files[:500],
        "lines": kept,
        "dropped": dropped + dropped_bytes,
    }


def _find_diff(bench, repo, worktree, args, max_bytes, allow=None):
    base = bench.base_of(repo, check_branch(_text(args, "branch", required=True)))
    base_head = git.rev_parse(base_ref_of(bench.bare_dir(repo.name), base), cwd=worktree)
    # Intent to add: a new file is in the diff, and its content stays out of the index.
    git.run(["add", "-N", "--", "."], cwd=worktree, check=False)
    scope = []
    if args.get("path"):
        _, rel = bench.resolve(repo, worktree, args.get("path"), allow=allow)
        scope = ["--", rel]
    text = git.out(["diff", "--no-color", base_head] + scope, cwd=worktree)
    parts = []
    current = []
    for row in text.splitlines(True):
        if row.startswith("diff --git "):
            if current:
                parts.append("".join(current))
            current = [row]
        else:
            current.append(row)
    if current:
        parts.append("".join(current))
    kept_parts = []
    for part in parts:
        names = re.findall(r"^diff --git a/(.+?) b/(.+)$", part.splitlines()[0])
        denied = False
        for pair in names:
            for name in pair:
                if deny_pattern(name, repo.deny):
                    denied = True
                if allow is not None and not allow_pattern(name, allow):
                    denied = True
        if not denied:
            kept_parts.append(part)
    diff, dropped = cap_text("".join(kept_parts), max_bytes)
    stat = git.out(["diff", "--numstat", base_head] + scope, cwd=worktree)
    added = removed = 0
    files = 0
    for row in stat.splitlines():
        cells = row.split("\t")
        if len(cells) < 3:
            continue
        files += 1
        if cells[0].isdigit():
            added += int(cells[0])
        if cells[1].isdigit():
            removed += int(cells[1])
    return {"mode": "diff", "base_head": base_head, "diff": diff, "dropped": dropped,
            "files": files, "lines_added": added, "lines_removed": removed}


def _find_symbols(bench, repo, worktree, args, max_bytes, allow=None):
    start_rel = clean_path(_text(args, "path", default=".") or ".")
    start, rel = bench.resolve(repo, worktree, start_rel, allow=allow)
    pattern = _text(args, "pattern")
    max_matches = _int(args, "max_matches", DEFAULT_MAX_MATCHES, 1, 2000)
    try:
        wanted = re.compile(pattern) if pattern else None
    except re.error as exc:
        raise Refusal("input", field="pattern", reason=str(exc))
    if os.path.isfile(start):
        names = [rel]
    elif os.path.isdir(start):
        text = git.out(["ls-files", "--cached", "--others", "--exclude-standard", "--", rel],
                       cwd=worktree)
        names = [name for name in text.splitlines()
                 if name and not deny_pattern(name, repo.deny)
                 and (allow is None or allow_pattern(name, allow))]
    else:
        raise Refusal("not_found", path=rel)
    found = []
    for name in sorted(set(names)):
        if not symbols.language_of(name):
            continue
        try:
            with open(os.path.join(worktree, name), "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read()
        except OSError:
            continue
        for item in symbols.definitions(name, content):
            if wanted is None or wanted.search(item["name"]):
                found.append(dict(item, path=name))

    def size_of(item):
        return len(item["name"]) + len(item["path"]) + len(item["kind"]) + 48

    dropped = sum(size_of(item) for item in found[max_matches:])
    kept, dropped_bytes = cap_items(found[:max_matches], max_bytes, size_of)
    return {"path": rel, "pattern": pattern, "symbols": kept,
            "dropped": dropped + dropped_bytes}


def find(bench, args):
    """Looks in a worktree: a tree, a glob, a grep or a diff."""
    repo = bench.repo(args.get("repo"))
    branch = check_branch(_text(args, "branch", required=True))
    # A pattern with no mode is a search: the tree would drop the pattern.
    mode = _text(args, "mode", default="grep" if args.get("pattern") else "tree")
    max_bytes = _int(args, "max_bytes", DEFAULT_MAX_BYTES, 256, CEILING_MAX_BYTES)
    allow = _globs(args, "allow")
    if mode in ("glob", "grep") and not args.get("pattern"):
        raise Refusal("input", field="pattern",
                      reason="mode %s needs pattern: %s" % (
                          mode, "a glob, e.g. *.py" if mode == "glob" else "a Perl regular expression"))
    with bench.lock(repo.name):
        worktree = bench.worktree(repo, branch)
        if mode == "tree":
            answer = _find_tree(bench, repo, worktree, args, max_bytes, allow)
        elif mode == "glob":
            answer = _find_glob(bench, repo, worktree, args, max_bytes, allow)
        elif mode == "grep":
            answer = _find_grep(bench, repo, worktree, args, max_bytes, allow)
        elif mode == "diff":
            answer = _find_diff(bench, repo, worktree, args, max_bytes, allow)
        else:
            raise Refusal("input", field="mode",
                          reason="use tree, glob, grep or diff; the symbols tool gives the definitions")
        answer.update({"repo": repo.name, "branch": branch, "max_bytes": max_bytes})
        return answer


def list_symbols(bench, args):
    """Gives the definitions of the Clojure and Python files under a path."""
    repo = bench.repo(args.get("repo"))
    branch = check_branch(_text(args, "branch", required=True))
    max_bytes = _int(args, "max_bytes", DEFAULT_MAX_BYTES, 256, CEILING_MAX_BYTES)
    allow = _globs(args, "allow")
    with bench.lock(repo.name):
        worktree = bench.worktree(repo, branch)
        answer = _find_symbols(bench, repo, worktree, args, max_bytes, allow)
        answer.update({"repo": repo.name, "branch": branch, "max_bytes": max_bytes})
        return answer


MAX_SYMBOL_MATCHES = 20


def _read_symbol(answer, content, symbol, max_bytes):
    """Gives the lines of each definition of symbol in one file."""
    rel = answer["path"]
    if not symbols.language_of(rel):
        raise Refusal("input", field="symbol",
                      reason="symbol reads a Clojure, ClojureScript, edn or Python file")
    found = symbols.definitions(rel, content)
    matches = [item for item in found
               if symbol in (item["name"], item["name"].rpartition(".")[2])]
    if not matches:
        names = sorted({item["name"] for item in found})
        close = difflib.get_close_matches(symbol, names, 5, 0.5)
        close += [name for name in names if symbol.lower() in name.lower() and name not in close]
        raise Refusal("not_found", path=rel, symbol=symbol, close=close[:10])
    all_lines = content.splitlines()
    budget = max_bytes
    dropped = 0
    definitions = []
    for item in matches[:MAX_SYMBOL_MATCHES]:
        window = [{"line": number, "text": all_lines[number - 1]}
                  for number in range(item["line"], min(item["end_line"], len(all_lines)) + 1)]
        kept, lost = cap_items(window, budget, lambda line: len(line["text"]) + 12)
        budget -= sum(len(line["text"]) + 12 for line in kept)
        dropped += lost
        definitions.append(dict(item, lines=kept))
    answer.update({"symbol": symbol, "matches": len(matches), "definitions": definitions,
                   "total_lines": len(all_lines), "dropped": dropped})
    return answer


_DIRECTORY_REMEDY = ("list it with the find tool: {\"mode\": \"tree\", \"path\": \"%s\"}, "
                     "or its definitions with the symbols tool")


def _load(bench, repo, worktree, branch, args, allow):
    """Gives (rel, ref, hash, content) of one file, from the worktree or from a ref."""
    ref = _text(args, "ref")
    full, rel = bench.resolve(repo, worktree, args.get("path"), allow=allow)
    if ref:
        if ref == "base":
            ref = bench.base_of(repo, branch)
        spec = "%s:%s" % (ref, rel)
        code, blob, err = git.run(["rev-parse", "--verify", "--quiet", spec], cwd=worktree,
                                  check=False)
        if code != 0:
            raise Refusal("not_found", path=rel, ref=ref)
        if git.line(["cat-file", "-t", blob.strip()], cwd=worktree) == "tree":
            raise Refusal("is_directory", path=rel, ref=ref, reason="this is a directory, not a file",
                          remedy=_DIRECTORY_REMEDY % rel)
        return rel, ref, blob.strip(), git.out(["show", spec], cwd=worktree)
    if os.path.isdir(full):
        raise Refusal("is_directory", path=rel, reason="this is a directory, not a file",
                      remedy=_DIRECTORY_REMEDY % rel)
    if not os.path.isfile(full):
        raise Refusal("not_found", path=rel)
    file_hash = git.line(["hash-object", "--", full], cwd=worktree)
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    except OSError as exc:
        raise Refusal("not_found", path=rel, reason=str(exc))
    return rel, ref, file_hash, content


def read_symbol(bench, args):
    """Gives the lines of each definition of one name in one file."""
    repo = bench.repo(args.get("repo"))
    branch = check_branch(_text(args, "branch", required=True))
    symbol = _text(args, "symbol", required=True)
    max_bytes = _int(args, "max_bytes", DEFAULT_MAX_BYTES, 256, CEILING_MAX_BYTES)
    allow = _globs(args, "allow")
    with bench.lock(repo.name):
        worktree = bench.worktree(repo, branch)
        rel, ref, file_hash, content = _load(bench, repo, worktree, branch, args, allow)
        return _read_symbol({"repo": repo.name, "branch": branch, "path": rel, "ref": ref,
                             "hash": file_hash}, content, symbol, max_bytes)


def history(bench, args):
    """Gives the commits that touched a path or added or removed a text: git log."""
    repo = bench.repo(args.get("repo"))
    branch = check_branch(_text(args, "branch", required=True))
    pickaxe = _text(args, "pickaxe")
    every = bool(args.get("all"))
    limit = _int(args, "limit", DEFAULT_HISTORY, 1, CEILING_HISTORY)
    allow = _globs(args, "allow")
    with bench.lock(repo.name):
        worktree = bench.worktree(repo, branch)
        rel = None
        scope = []
        if args.get("path"):
            _, rel = bench.resolve(repo, worktree, _text(args, "path"), allow=allow)
            scope = ["--", rel]
        elif allow is not None:
            # without a path the subjects of every commit would pass the allow list
            raise Refusal("denied", allow=list(allow), reason="with allow, history needs a path")
        log_args = ["log", "--format=%H %ad %s", "--date=short", "-n", str(limit)]
        if pickaxe:
            # one word, so a text that starts with a dash is not read as an option
            log_args.append("-S" + pickaxe)
        if every:
            log_args.append("--all")
        code, text, err = git.run(log_args + scope, cwd=worktree, check=False)
    if code != 0:
        raise Refusal("history", reason=(err or text).strip()[:400])
    commits = []
    for row in text.splitlines():
        sha, _, rest = row.partition(" ")
        date, _, subject = rest.partition(" ")
        commits.append({"sha": sha, "date": date, "subject": subject[:400]})
    return {"repo": repo.name, "branch": branch, "path": rel, "pickaxe": pickaxe,
            "all": every, "limit": limit, "commits": commits}


def read(bench, args):
    """Gives lines with numbers from the worktree or from a ref."""
    if args.get("symbol") is not None:
        raise Refusal("input", field="symbol", reason="read takes no symbol; use read_symbol")
    unknown = sorted(key for key in args if key not in TOOLS["read"]["schema"]["properties"])
    if unknown:
        raise Refusal("input", field=unknown[0], unknown=unknown,
                      reason="read takes no %s; the window is offset, the first line "
                             "from 1, and limit, the count of lines" % " or ".join(unknown))
    repo = bench.repo(args.get("repo"))
    branch = check_branch(_text(args, "branch", required=True))
    offset = _int(args, "offset", 1, 1, 1000000)
    asked = _int(args, "limit", DEFAULT_LIMIT, 1, CEILING_LIMIT)
    limit = min(asked, DEFAULT_LIMIT)
    max_bytes = _int(args, "max_bytes", DEFAULT_MAX_BYTES, 256, CEILING_MAX_BYTES)
    if_hash = _text(args, "if_hash")
    allow = _globs(args, "allow")
    with bench.lock(repo.name):
        worktree = bench.worktree(repo, branch)
        rel, ref, file_hash, content = _load(bench, repo, worktree, branch, args, allow)
        if if_hash and if_hash == file_hash:
            return {"repo": repo.name, "branch": branch, "path": rel,
                    "unchanged": True, "hash": file_hash}
        all_lines = content.splitlines()
        total = len(all_lines)
        window = all_lines[offset - 1: offset - 1 + limit]
        items = [{"line": offset + index, "text": text} for index, text in enumerate(window)]
        kept, dropped = cap_items(items, max_bytes, lambda item: len(item["text"]) + 12)
        last = kept[-1]["line"] if kept else offset - 1
        answer = {
            "repo": repo.name,
            "branch": branch,
            "path": rel,
            "ref": ref,
            "hash": file_hash,
            "offset": offset,
            "lines": kept,
            "total_lines": total,
            "eof": last >= total,
            "dropped": dropped,
        }
        if asked > limit:
            answer["note"] = READ_NOTE
        return answer


CLOJURE_SUFFIXES = (".clj", ".cljs", ".cljc", ".edn")
CLOSERS = {")": "(", "]": "[", "}": "{"}


def clojure_balance(text):
    """Gives the first unbalanced place of a Clojure text, or None.

    A walk at the reader's level: strings, regexes, char literals and
    comments hold no forms. The answer is {line, col, message}.
    """
    stack = []
    string_at = None
    line, col = 1, 0
    index, size = 0, len(text)
    while index < size:
        char = text[index]
        index += 1
        if char == "\n":
            line, col = line + 1, 0
            continue
        col += 1
        if char == "\\":
            # An escape in a string, or a char literal such as \( outside one.
            if index < size:
                if text[index] == "\n":
                    line, col = line + 1, 0
                else:
                    col += 1
                index += 1
            continue
        if string_at:
            if char == '"':
                string_at = None
            continue
        if char == ";":
            while index < size and text[index] != "\n":
                index += 1
            continue
        if char == '"':
            string_at = (line, col)
        elif char in "([{":
            stack.append((char, line, col))
        elif char in CLOSERS:
            if not stack:
                return {"line": line, "col": col, "message": "%s closes no open form" % char}
            opener, open_line, open_col = stack.pop()
            if opener != CLOSERS[char]:
                return {"line": line, "col": col,
                        "message": "%s closes the %s opened at line %d col %d"
                                   % (char, opener, open_line, open_col)}
    if string_at:
        return {"line": string_at[0], "col": string_at[1],
                "message": "the string opened here is never closed"}
    if stack:
        opener, open_line, open_col = stack[0]
        return {"line": open_line, "col": open_col,
                "message": "the %s opened at line %d is never closed" % (opener, open_line)}
    return None


def changed_paths(bench, repo, branch, worktree):
    """Gives the files a branch changed against its base, deleted ones aside. No write."""
    base = bench.base_of(repo, branch)
    base_head = git.rev_parse(base_ref_of(bench.bare_dir(repo.name), base), cwd=worktree)
    changed = git.out(["-c", "core.quotePath=false", "diff", "--name-only", "--diff-filter=d",
                       base_head], cwd=worktree).splitlines()
    untracked = git.out(["-c", "core.quotePath=false", "ls-files", "--others",
                         "--exclude-standard"], cwd=worktree).splitlines()
    names = sorted(set(name for name in changed + untracked if name))
    return [name for name in names if not deny_pattern(name, repo.deny)]


def kondo_errors(program, worktree, paths):
    """Gives the errors clj-kondo finds in paths; its warnings stay out."""
    done = subprocess.run(
        [program, "--lint"] + paths + ["--config", "{:output {:format :json}}"],
        cwd=worktree, capture_output=True, text=True, timeout=120, check=False)
    report = json.loads(done.stdout or "{}")
    return [
        {"path": item.get("filename"), "line": item.get("row"), "col": item.get("col"),
         "message": "clj-kondo: %s" % item.get("message")}
        for item in report.get("findings", []) if item.get("level") == "error"
    ]


def yaml_errors(places, unavailable):
    """Parses each (full, rel) YAML file. Gives a finding for each file that does not parse."""
    try:
        import yaml
    except ImportError:
        unavailable.append("no YAML parser (PyYAML) on the rig: the .github YAML files were not parsed")
        return []
    findings = []
    for full, rel in places:
        with open(full, "rb") as handle:
            source = handle.read()
        try:
            for _ in yaml.safe_load_all(source):
                pass
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
            problem = getattr(exc, "problem", None) or str(exc)
            findings.append({"path": rel, "line": mark.line + 1 if mark else 1,
                             "col": mark.column + 1 if mark else 1, "message": "YAML: %s" % problem})
    return findings


# The labels GitHub gives its own runners. CI runs on the house's runners only, and
# the list is fixed: no repository's policy changes it.
HOSTED_RUNNER = re.compile(r"(?<![\w.-])(?:ubuntu|windows|macos)-[\w.-]+")
HOUSE_RUNNER_LABELS = ("self-hosted", "waymark")
_WORKFLOW_KEY = re.compile(r"^(\s*(?:-\s+)?)([\w-]+)\s*:(.*)$")
_MATRIX_KEY = re.compile(r"matrix\.([\w-]+)")
_HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", re.M)


def is_workflow(rel):
    return rel.startswith(".github/workflows/") and rel.endswith((".yml", ".yaml"))


def hosted_runs_on(text):
    """Gives each line of a workflow that gives runs-on a GitHub-hosted label: in its
    value, in an expression's fallback, in a list under it, or in a matrix value that
    runs-on reads. `with` names the runs-on lines that read that matrix value."""
    lines = [line.split(" #")[0].rstrip() for line in text.splitlines()]
    feeds = {"runs-on": []}
    for number, line in enumerate(lines, 1):
        opened = _WORKFLOW_KEY.match(line)
        if opened and opened.group(2) == "runs-on":
            for key in _MATRIX_KEY.findall(opened.group(3)):
                feeds.setdefault(key, []).append(number)
    found, inside, column = [], None, 0
    for number, line in enumerate(lines, 1):
        bare = line.lstrip()
        if not bare or bare.startswith("#"):
            continue
        indent = len(line) - len(bare)
        opened = _WORKFLOW_KEY.match(line)
        if opened and opened.group(2) in feeds:
            inside, column, value = opened.group(2), len(opened.group(1)), opened.group(3)
        elif inside and (indent > column or (indent == column and bare.startswith("- "))):
            value = line
        else:
            inside = None
            continue
        label = HOSTED_RUNNER.search(value)
        if label:
            found.append({"line": number, "text": bare, "label": label.group(0),
                          "with": feeds[inside]})
    return found


def added_lines(worktree, against, rel, cached=False):
    """Gives the numbers of the lines of rel that a change adds or changes against a
    commit. None is every line: the commit does not have the file."""
    if git.run(["cat-file", "-e", "%s:%s" % (against, rel)], cwd=worktree, check=False)[0] != 0:
        return None
    text = git.out(["diff", "-U0", "--no-renames"] + (["--cached"] if cached else [])
                   + [against, "--", rel], cwd=worktree)
    added = set()
    for start, count in _HUNK.findall(text):
        added.update(range(int(start), int(start) + int(count or 1)))
    return added


def hosted_runner_findings(worktree, against, rels, cached=False, allowed=()):
    """Gives a finding for each GitHub-hosted runs-on line that a change adds or changes
    in a workflow. A line the change does not touch is not judged, and a file in
    allowed, the repository's hosted_workflows, is not judged."""
    findings = []
    for rel in rels:
        full = os.path.join(worktree, rel)
        if not is_workflow(rel) or rel in allowed or not os.path.isfile(full):
            continue
        with open(full, "r", encoding="utf-8", errors="replace") as handle:
            hosted = hosted_runs_on(handle.read())
        added = added_lines(worktree, against, rel, cached) if hosted else set()
        for item in hosted:
            if added is None or item["line"] in added or added.intersection(item["with"]):
                findings.append({
                    "path": rel, "line": item["line"], "col": 1,
                    "message": "runs-on: %s is a GitHub-hosted runner (%s). CI runs on the "
                               "house's runners: use the labels %s"
                               % (item["label"], item["text"], ", ".join(HOUSE_RUNNER_LABELS))})
    return findings


def hosted_runner_lines(worktree, allowed=()):
    """Gives every GitHub-hosted runs-on line of the worktree's workflows, as information.
    `allowed` says if the file is in allowed, the repository's hosted_workflows."""
    folder = os.path.join(worktree, ".github", "workflows")
    lines = []
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        rel = ".github/workflows/" + name
        full = os.path.join(folder, name)
        if is_workflow(rel) and os.path.isfile(full):
            with open(full, "r", encoding="utf-8", errors="replace") as handle:
                lines.extend({"path": rel, "line": item["line"], "text": item["text"],
                              "allowed": rel in allowed}
                             for item in hosted_runs_on(handle.read()))
    return lines


BASH_ERROR = re.compile(r"^.*?: line (\d+): (.*)$")
CHECK_MARK = "✗"
CHECK_KIND = re.compile(r"\[([^\]]+)\]\s*")
CHECK_FIELD = re.compile(r"(\S+?):(?:\s+|$)")


def check_command_findings(result):
    """Turns the answer of the repository's check step into findings: one for
    each line that opens with ✗, as {kind, field, sentence}, where kind is
    its [tag] and field the word before its colon, when it has them. A failed
    step with no ✗ line is one finding that carries the tail of its output."""
    findings = []
    for line in result["output"].splitlines():
        text = line.strip()
        if not text.startswith(CHECK_MARK):
            continue
        text = text[len(CHECK_MARK):].strip()
        kind = field = None
        match = CHECK_KIND.match(text)
        if match:
            kind, text = match.group(1), text[match.end():]
        match = CHECK_FIELD.match(text)
        if match:
            field, text = match.group(1), text[match.end():]
        findings.append({"path": None, "kind": kind, "field": field, "sentence": text.strip(),
                         "message": "check: %s" % line.strip()})
    if not findings and not result["ok"]:
        findings.append({"path": None, "kind": None, "field": None,
                         "sentence": "the check step exited %s" % result["exit_code"],
                         "message": "check: the step exited %s" % result["exit_code"],
                         "output": result["output"][-2000:]})
    return findings


def step_timed_out(result):
    """True when run_step killed the step at its timeout."""
    return result["exit_code"] == -1 and "(killed after " in result["output"][-200:]


def check_step_findings(repo, branch, worktree, step):
    """Runs the repository's check step. Gives {findings, exit_code, timed_out}.
    Its prepare command runs first, when it has one, and the command gets what
    is left of the one timeout. A failed prepare is one finding that carries
    the tail of its output, and the command does not run."""
    budget = step.timeout
    if step.prepare:
        started = time.monotonic()
        result = run_step(repo, branch, worktree,
                          config_module.StageConfig("prepare", step.prepare, timeout=budget))
        if not result["ok"]:
            return {"findings": [{"path": None, "kind": None, "field": None,
                                  "sentence": "the check prepare exited %s" % result["exit_code"],
                                  "message": "check: the prepare exited %s" % result["exit_code"],
                                  "output": result["output"][-2000:]}],
                    "exit_code": result["exit_code"], "timed_out": step_timed_out(result)}
        budget = max(1, budget - int(time.monotonic() - started))
    command = config_module.StageConfig(step.name, step.command, timeout=budget)
    result = run_step(repo, branch, worktree, command)
    return {"findings": check_command_findings(result), "exit_code": result["exit_code"],
            "timed_out": step_timed_out(result)}


CHECK_WAIT = 5
CEILING_CHECK_WAIT = 20
CHECK_KEEP = 3600


def start_check_step(bench, repo, branch, worktree, lint):
    """Starts the repository's check step on a thread. Gives its check_id and
    its entry, which holds the lint answer and, when the step ends, its result.
    A finished entry older than an hour is dropped."""
    check_id = os.urandom(8).hex()
    entry = {"repo": repo.name, "branch": branch, "started": time.time(), "lint": lint,
             "result": None, "done": threading.Event()}

    def work():
        try:
            entry["result"] = check_step_findings(repo, branch, worktree, repo.check)
        except Exception as exc:  # the poll reports it; a thread has no caller
            entry["result"] = {"findings": [{"path": None, "kind": None, "field": None,
                                             "sentence": "the check step did not run: %s" % exc,
                                             "message": "check: the step did not run: %s" % exc}],
                               "exit_code": None, "timed_out": False}
        finally:
            entry["done"].set()

    with bench._guard:
        old = time.time() - CHECK_KEEP
        for key in [key for key, item in bench.checks.items()
                    if item["done"].is_set() and item["started"] < old]:
            del bench.checks[key]
        bench.checks[check_id] = entry
    threading.Thread(target=work, name="check-" + check_id, daemon=True).start()
    return check_id, entry


def check_answer(check_id, entry, wait):
    """Waits up to `wait` seconds for a check step. Gives the lint answer with
    check_id and pending; when the step ended, its state (finished or
    timed_out), its exit code and its findings after the lint's."""
    entry["done"].wait(wait)
    answer = dict(entry["lint"], check_id=check_id)
    if not entry["done"].is_set():
        answer.update(pending=True, state="pending", ok=None,
                      remedy="call check again with this check_id until it is not pending")
        return answer
    result = entry["result"]
    findings = list(answer["findings"]) + result["findings"]
    answer.update(pending=False, state="timed_out" if result["timed_out"] else "finished",
                  exit_code=result["exit_code"], ok=not findings, findings=findings)
    return answer


def shell_errors(program, worktree, rel):
    """Runs `bash -n` on one script. Gives its syntax errors as findings."""
    run = subprocess.run([program, "-n", rel], cwd=worktree, capture_output=True,
                         text=True, timeout=30)
    if run.returncode == 0:
        return []
    lines = [line for line in run.stderr.splitlines() if line.strip()]
    findings = []
    for line in lines:
        match = BASH_ERROR.match(line)
        if match and "syntax error" in match.group(2):
            findings.append({"path": rel, "line": int(match.group(1)), "col": 1,
                             "message": match.group(2)})
    if not findings:
        findings.append({"path": rel, "line": 1, "col": 1,
                         "message": lines[0] if lines else "bash -n failed"})
    return findings


def check(bench, args):
    """Lints the files a change touched: Clojure forms, Python compiles, shell parses,
    .github YAML parses, no workflow line it touched gives runs-on a GitHub-hosted
    runner. Then it starts the repository's check step, when it has one,
    and waits for it a while: a step that has not ended answers pending with a
    check_id, and check with that check_id answers it later. It never writes."""
    repo = bench.repo(args.get("repo"))
    branch = check_branch(_text(args, "branch", required=True))
    wait = _int(args, "wait", CHECK_WAIT, 0, CEILING_CHECK_WAIT)
    check_id = _text(args, "check_id")
    if check_id is not None:
        with bench._guard:
            entry = bench.checks.get(check_id)
        if entry is None or entry["repo"] != repo.name or entry["branch"] != branch:
            raise Refusal("unknown_check", check_id=check_id, repo=repo.name, branch=branch,
                          remedy="call check with no check_id to start the check again")
        return check_answer(check_id, entry, wait)
    given = args.get("paths")
    if given is not None and (not isinstance(given, list)
                              or not all(isinstance(item, str) and item for item in given)):
        raise Refusal("input", field="paths", reason="paths is a list of paths")
    findings, skipped, unavailable, clojure, shell, workflows = [], [], [], [], [], []
    with bench.lock(repo.name):
        worktree = bench.worktree(repo, branch)
        if given:
            places = [bench.resolve(repo, worktree, item, allow=None) for item in given]
        else:
            places = [(os.path.join(worktree, rel), rel)
                      for rel in changed_paths(bench, repo, branch, worktree)]
        for full, rel in places:
            if not os.path.isfile(full):
                skipped.append(rel)
            elif rel.endswith(CLOJURE_SUFFIXES):
                with open(full, "r", encoding="utf-8", errors="replace") as handle:
                    place = clojure_balance(handle.read())
                if place:
                    findings.append(dict(path=rel, **place))
                else:
                    clojure.append(rel)
            elif rel.endswith(".py"):
                with open(full, "rb") as handle:
                    source = handle.read()
                try:
                    compile(source, rel, "exec", dont_inherit=True)
                except SyntaxError as exc:
                    findings.append({"path": rel, "line": exc.lineno or 1, "col": exc.offset or 1,
                                     "message": exc.msg})
                except ValueError as exc:
                    findings.append({"path": rel, "line": 1, "col": 1, "message": str(exc)})
            elif rel.endswith(".sh"):
                shell.append(rel)
            elif rel.startswith(".github/") and rel.endswith((".yml", ".yaml")):
                workflows.append((full, rel))
            else:
                skipped.append(rel)
        if workflows:
            findings.extend(yaml_errors(workflows, unavailable))
        if any(is_workflow(rel) for _, rel in places):
            base_head = git.rev_parse(
                base_ref_of(bench.bare_dir(repo.name), bench.base_of(repo, branch)), cwd=worktree)
            findings.extend(hosted_runner_findings(worktree, base_head,
                                                   [rel for _, rel in places],
                                                   allowed=repo.hosted_workflows))
        hosted = hosted_runner_lines(worktree, repo.hosted_workflows)
        if shell:
            program = shutil.which("bash")
            if not program:
                unavailable.append("bash not installed on the rig: the shell scripts were not checked")
            else:
                for rel in shell:
                    try:
                        findings.extend(shell_errors(program, worktree, rel))
                    except (OSError, subprocess.SubprocessError) as exc:
                        unavailable.append("bash -n did not answer on %s: %s" % (rel, exc))
        if clojure:
            program = shutil.which("clj-kondo")
            if not program:
                unavailable.append("clj-kondo not installed on the rig: the Clojure files had the "
                                   "balance check only")
            else:
                try:
                    findings.extend(kondo_errors(program, worktree, clojure))
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    unavailable.append("clj-kondo did not answer: %s" % exc)
    answer = {
        "repo": repo.name,
        "branch": branch,
        "ok": not findings,
        "findings": findings,
        "skipped": skipped,
        "unavailable": unavailable,
        "hosted_runs_on": hosted,
    }
    if repo.check is None:
        return answer
    check_id, entry = start_check_step(bench, repo, branch, worktree, answer)
    return check_answer(check_id, entry, wait)


EDIT_FIELDS = ("path", "old", "new", "content", "create", "delete", "move_to")
MAX_EDITS = 50
_ON_DISK = "on_disk"


def _operation_of(item):
    """Reads one edit's fields. Gives (operation, old, new)."""
    old = item.get("old")
    new = item.get("new")
    content = item.get("content")
    create = item.get("create")
    if create is not None and not isinstance(create, bool):
        raise Refusal("input", field="create", reason="create is true or false: give the "
                      "content in new, as new with create: true")
    if content is not None:
        # content is a spelling of new for a new file: create true or
        # absent, and no old. Any other use is refused, naming new.
        if new is not None or old is not None or create is False:
            raise Refusal("input", field="content", reason="give the text in new, not content: "
                          "old with new (replace) or new with create: true (a new file)")
        if not isinstance(content, str):
            raise Refusal("input", field="content", reason="a text is necessary")
        new = content
        create = True
    operations = []
    if old is not None:
        operations.append("replace")
    if create:
        operations.append("create")
    if item.get("delete"):
        operations.append("delete")
    if item.get("move_to"):
        operations.append("move")
    if len(operations) != 1:
        raise Refusal("operation", operations=operations,
                      reason="give exactly one of: old with new (replace), new with create: true "
                             "(a new file), delete: true, or move_to")
    return operations[0], old, new


class _Plan:
    """The worktree as the edits so far leave it, before a byte is written.

    A path maps to None (gone), to a text (its new content) or to
    _ON_DISK with the path whose bytes it holds (a file moved as it is).
    """

    def __init__(self):
        self.state = {}
        self.steps = []

    def exists(self, full):
        if full in self.state:
            return self.state[full] is not None
        return os.path.exists(full)

    def is_file(self, full):
        if full in self.state:
            return self.state[full] is not None
        return os.path.isfile(full)

    def text(self, full):
        value = self.state.get(full, (_ON_DISK, full))
        if isinstance(value, tuple):
            with open(value[1], "r", encoding="utf-8") as handle:
                return handle.read()
        return value

    def write(self):
        """Applies the steps in order. The plan has judged each one."""
        for step in self.steps:
            if step[0] == "write":
                os.makedirs(os.path.dirname(step[1]), exist_ok=True)
                with open(step[1], "w", encoding="utf-8") as handle:
                    handle.write(step[2])
            elif step[0] == "delete":
                os.remove(step[1])
            else:
                os.makedirs(os.path.dirname(step[2]), exist_ok=True)
                os.replace(step[1], step[2])


def _nearest(content, old):
    """Gives the line of the file nearest old, for a replace that found nothing.

    The first line of old that is not in the file is the one sought: the
    file's first line with the same words under other spaces, else the
    line most like it by difflib. None when no line is near.
    """
    missing = next((line for line in old.split("\n") if line.strip() and line not in content), None)
    if missing is None:
        return None
    lines = content.split("\n")
    words = " ".join(missing.split())
    for number, line in enumerate(lines, 1):
        if words in " ".join(line.split()):
            return {"line": number, "text": line, "old": missing}
    matcher = difflib.SequenceMatcher(None, "", missing.strip(), autojunk=False)
    best, score = None, 0.6
    for number, line in enumerate(lines, 1):
        matcher.set_seq1(line.strip())
        if matcher.real_quick_ratio() > score and matcher.quick_ratio() > score:
            ratio = matcher.ratio()
            if ratio > score:
                best, score = {"line": number, "text": line, "old": missing}, ratio
    return best


def _nearest_block(content, old, nearest):
    """Gives the file's lines that stand where old would, around the nearest line, verbatim."""
    wanted = old.split("\n")
    first = max(nearest["line"] - wanted.index(nearest["old"]), 1)
    lines = content.split("\n")
    return {"line": first, "text": "\n".join(lines[first - 1: first - 1 + len(wanted)])}


def _lines_of(content, old):
    """Gives the line on which each copy of old starts, at most 20."""
    lines, start = [], content.find(old)
    while start != -1 and len(lines) < 20:
        lines.append(content.count("\n", 0, start) + 1)
        start = content.find(old, start + max(len(old), 1))
    return lines


def _plan_edit(bench, repo, worktree, plan, item, allow_protected, allow):
    """Judges one edit against the plan and adds its steps. Gives its answer."""
    operation, old, new = _operation_of(item)
    full, rel = bench.resolve(repo, worktree, item.get("path"), for_write=True,
                              allow_protected=allow_protected, allow=allow)
    if operation == "replace":
        if new is None or not isinstance(new, str) or not isinstance(old, str):
            raise Refusal("input", field="new", reason="old and new must be texts")
        if old == "" and not plan.exists(full):
            # An empty old on a path that is not there is a new file.
            plan.state[full] = new
            plan.steps.append(("write", full, new))
            return {"path": rel}, full
        if not plan.is_file(full):
            raise Refusal("not_found", path=rel,
                          remedy="the file is not there: new with create: true makes it")
        content = plan.text(full)
        found = content.count(old)
        if found > 1:
            raise Refusal("found", path=rel, found=found, lines=_lines_of(content, old),
                          remedy="old is in the file %d times, starting on lines: give more of the "
                                 "file around the one you mean in old, so it is unique" % found)
        if not found:
            nearest = _nearest(content, old)
            if nearest is None:
                raise Refusal("found", path=rel, found=0, remedy="old is not in the file: copy it from a read")
            raise Refusal("found", path=rel, found=0, nearest=nearest,
                          block=_nearest_block(content, old, nearest),
                          remedy="old is not in the file: nearest is the file's line near its line "
                                 "nearest.old, and block.text is the file's lines where old would "
                                 "stand, from block.line, so copy block.text into old")
        content = content.replace(old, new, 1)
        plan.state[full] = content
        plan.steps.append(("write", full, content))
        return {"path": rel}, full
    if operation == "create":
        if new is None or not isinstance(new, str):
            raise Refusal("input", field="new", reason="give the content in new, as new with create: true")
        if plan.exists(full):
            raise Refusal("exists", path=rel, remedy="use old and new to change the file")
        plan.state[full] = new
        plan.steps.append(("write", full, new))
        return {"path": rel}, full
    if operation == "delete":
        if not plan.is_file(full):
            raise Refusal("not_found", path=rel)
        plan.state[full] = None
        plan.steps.append(("delete", full))
        return {"path": rel, "deleted": True}, None
    target, target_rel = bench.resolve(repo, worktree, item.get("move_to"), for_write=True,
                                       allow_protected=allow_protected, allow=allow)
    if not plan.exists(full):
        raise Refusal("not_found", path=rel)
    if plan.exists(target):
        raise Refusal("exists", path=target_rel)
    plan.state[target] = plan.state.get(full, (_ON_DISK, full))
    plan.state[full] = None
    plan.steps.append(("move", full, target))
    return {"path": target_rel, "from": rel}, target


def edit(bench, args):
    """Changes one path: a replace, a create, a delete or a move."""
    if args.get("edits") is not None:
        raise Refusal("input", field="edits",
                      reason="edit takes the fields of one edit; use edit_many for a list")
    return _edit(bench, args, None)


def edit_many(bench, args):
    """Changes paths with a list of edits.

    The list is judged whole before a byte is written: one refused edit
    writes none of them.
    """
    edits = args.get("edits")
    beside = [key for key in EDIT_FIELDS if args.get(key) is not None]
    if beside:
        raise Refusal("input", field="edits", beside=beside,
                      reason="give edits or the fields of one edit, not both")
    if not isinstance(edits, list) or not edits:
        raise Refusal("input", field="edits", reason="edits is a list of one edit or more")
    if len(edits) > MAX_EDITS:
        raise Refusal("input", field="edits", count=len(edits),
                      reason="at most %d edits in one call" % MAX_EDITS)
    problems = []
    for index, item in enumerate(edits, 1):
        if not isinstance(item, dict):
            problems.append({"refused": "input", "item": index, "field": "edits",
                             "reason": "each edit is an object"})
            continue
        for key in sorted(set(item) - set(EDIT_FIELDS)):
            problems.append({"refused": "input", "item": index, "field": key,
                             "reason": "an edit takes only: " + ", ".join(EDIT_FIELDS)})
    return _edit(bench, args, edits, problems)


def _refused_items(problems):
    """One refusal for a list's bad edits: the first at the top, each in items."""
    problems = sorted(problems, key=lambda problem: problem["item"])
    first = dict(problems[0])
    refusal = Refusal(first.pop("refused"), **first)
    refusal.data["items"] = problems
    return refusal


def _edit(bench, args, edits, problems=None):
    """Plans the edits and writes them together. edits None is the one edit in args.

    Every edit of a list is judged before a refusal, and the refusal names
    each bad one in items: problems holds those edit_many found already.
    """
    repo = bench.repo(args.get("repo"))
    branch = check_branch(_text(args, "branch", required=True))
    allow_protected = bool(args.get("allow_protected"))
    allow = _globs(args, "allow")
    items = [args] if edits is None else edits
    problems = list(problems or [])
    # The fields of each edit are judged before the lock, as one edit's were.
    for index, item in enumerate(items, 1):
        if any(problem["item"] == index for problem in problems):
            continue
        try:
            _operation_of(item)
        except Refusal as exc:
            if edits is None:
                raise
            problems.append(dict(exc.data, item=index))
    with bench.lock(repo.name):
        worktree = bench.worktree(repo, branch)
        bench.no_landing(repo, branch)
        plan = _Plan()
        results = []
        bad = set(problem["item"] for problem in problems)
        for index, item in enumerate(items, 1):
            if index in bad:
                continue
            try:
                results.append(_plan_edit(bench, repo, worktree, plan, item, allow_protected, allow))
            except Refusal as exc:
                if edits is None:
                    raise
                problems.append(dict(exc.data, item=index))
        if problems:
            raise _refused_items(problems)
        plan.write()
        note_written(worktree, [name for result, _ in results
                                for name in (result["path"], result.get("from"))])
        answers = []
        for result, full in results:
            if full is not None and os.path.isfile(full):
                result["hash"] = git.line(["hash-object", "--", full], cwd=worktree)
            answers.append(result)
        if edits is None:
            answer = {"repo": repo.name, "branch": branch}
            answer.update(answers[0])
            return answer
        return {"repo": repo.name, "branch": branch, "edits": answers}


def _wanted(name, patterns):
    """Tells if a path is one the caller named: the path, a directory of it, or a glob."""
    if patterns is None:
        return True
    for pattern in patterns:
        stem = pattern.rstrip("/")
        if name == stem or name.startswith(stem + "/") or fnmatch.fnmatch(name, pattern):
            return True
    return False


def diff(bench, args):
    """Gives the worktree's uncommitted edits against HEAD, one bounded text per path.

    Each path's text is cut at max_bytes and the whole answer's at max_total;
    a path cut short says truncated. A path a deny glob matches is not served.
    """
    repo = bench.repo(args.get("repo"))
    branch = check_branch(_text(args, "branch", required=True))
    wanted = _globs(args, "paths")
    max_bytes = _int(args, "max_bytes", 4096, 256, CEILING_MAX_BYTES)
    max_total = _int(args, "max_total", DEFAULT_MAX_BYTES, 256, CEILING_MAX_BYTES)
    with bench.lock(repo.name):
        worktree = bench.worktree(repo, branch)
        tracked = set(name for name in git.out(["diff", "HEAD", "--name-only", "-z"],
                                                 cwd=worktree).split("\0") if name)
        untracked = set(name for name in git.out(["ls-files", "--others", "--exclude-standard", "-z"],
                                                   cwd=worktree).split("\0") if name)
        files = []
        left = max_total
        for name in sorted(tracked | untracked):
            if deny_pattern(name, repo.deny) or not _wanted(name, wanted):
                continue
            if name in tracked:
                status, argv = "changed", ["diff", "HEAD", "--", name]
            else:
                status, argv = "untracked", ["diff", "--no-index", "--", os.devnull, name]
            data = git.run(argv, cwd=worktree, check=False)[1].encode("utf-8")
            cut = data[:min(max_bytes, left)].decode("utf-8", errors="ignore")
            left -= len(cut.encode("utf-8"))
            files.append({"path": name, "status": status, "bytes": len(data),
                          "truncated": len(cut.encode("utf-8")) < len(data), "diff": cut})
        head = head_of(worktree)
    return {"repo": repo.name, "branch": branch, "head": head, "files": files,
            "truncated": any(item["truncated"] for item in files)}


def stash_ref(branch):
    """Names the ref that holds the edits a pull put aside for a branch."""
    return "refs/bench/stash/" + branch


def stash_dirty(worktree, branch):
    """Puts a dirty worktree's edits aside before a pull. Gives the paths put aside.

    The stash lives under stash_ref, not in the stash list the worktrees
    share, so it outlives a pull that stops on a conflict. A stash a pull
    already holds is kept as it is, and its paths are not named again.
    """
    ref = stash_ref(branch)
    paths = status_paths(worktree)
    if not paths or git.ref_exists(ref, cwd=worktree):
        return []
    code, text, err = git.run(["stash", "push", "-u", "-m", "bench pull " + branch],
                              cwd=worktree, check=False)
    if code != 0:
        raise Refusal("stash_failed", branch=branch, reason=(err or text).strip()[:400])
    git.run(["update-ref", ref, git.rev_parse("refs/stash", cwd=worktree)], cwd=worktree)
    git.run(["stash", "drop", "-q"], cwd=worktree, check=False)
    return paths


def restore_stash(worktree, branch, paths):
    """Puts the edits a pull put aside back. Gives each path with its status.

    A path whose edits meet the pull's changes keeps the conflict markers in
    the file and is conflicted; the index is left plain. When git cannot
    apply the stash at all, the stash stays under its ref and each path is held.
    """
    ref = stash_ref(branch)
    if not git.ref_exists(ref, cwd=worktree):
        return []
    code, _, _ = git.run(["stash", "apply", ref], cwd=worktree, check=False)
    conflicted = unmerged_paths(worktree)
    if code != 0 and not conflicted:
        return [{"path": name, "status": "held"} for name in paths]
    git.run(["reset", "-q"], cwd=worktree, check=False)
    git.run(["update-ref", "-d", ref], cwd=worktree, check=False)
    names = sorted(set(paths) | set(conflicted))
    return [{"path": name, "status": "conflicted" if name in conflicted else "restored"}
            for name in names]


def _merge_in(worktree, branch, base, remote):
    """Merges remote into the worktree: fast-forward only when base is None."""
    before = status_paths(worktree)
    if base is None:
        code, text, err = git.run(["merge", "--ff-only", remote], cwd=worktree, check=False)
        if code != 0:
            reset = undo_merge(worktree, before)
            raise Refusal("not_fast_forward", branch=branch,
                          reason=(err or text).strip()[:400], reset=reset[:100],
                          remedy="use pull from base, or discard")
        return {"head": head_of(worktree), "merged": True, "conflicts": []}
    code, text, err = git.run(["merge", "--no-edit", remote], cwd=worktree, check=False)
    conflicts = []
    if code != 0:
        conflicts = unmerged_paths(worktree)
        if not conflicts:
            # A merge that failed leaves the worktree as it was before it.
            reset = undo_merge(worktree, before)
            raise Refusal("merge_failed", branch=branch, base=base,
                          reason=(err or text).strip()[:400], reset=reset[:100])
    answer = {
        "base": base,
        "head": head_of(worktree),
        "merged": code == 0,
        "conflicts": conflicts,
        "merge_in_progress": bool(conflicts),
        "note": ("the markers stay in the files and the merge stays in progress: "
                 "remove them, then pull or submit") if conflicts else "",
    }
    if conflicts:
        answer["markers"] = conflict_ranges(worktree, conflicts)
    return answer


def pull(bench, args):
    """Brings the worktree to the branch head, or merges the base in.

    A dirty worktree's edits are put aside first and put back after the
    merge (reapplied). A merge that stops on a conflict holds them until a
    later pull finishes it.
    """
    repo = bench.repo(args.get("repo"))
    branch = check_branch(_text(args, "branch", required=True))
    source = _text(args, "from", default="base")
    if source not in ("base", "head"):
        raise Refusal("input", field="from", reason="use base or head")
    with bench.lock(repo.name):
        worktree = bench.worktree(repo, branch)
        bench.no_landing(repo, branch)
        if git.ref_exists("MERGE_HEAD", cwd=worktree):
            # A merge a conflicted pull left: commit it once its markers are gone.
            marked = finish_merge(worktree)
            if marked:
                return {"repo": repo.name, "branch": branch, "head": head_of(worktree),
                        "merged": False, "conflicts": marked, "merge_in_progress": True,
                        "markers": conflict_ranges(worktree, marked),
                        "note": "the merge is in progress: remove the markers, then pull or submit"}
        bare = bench.fetch(repo)
        base = None
        if source == "head":
            remote = "refs/remotes/origin/" + branch
            if not git.ref_exists(remote, cwd=bare):
                return {"repo": repo.name, "branch": branch, "head": head_of(worktree),
                        "merged": False, "conflicts": [],
                        "reason": "the branch is not on the remote"}
        else:
            base = bench.base_of(repo, branch)
            remote = base_ref_of(bare, base)
        stashed = stash_dirty(worktree, branch)
        try:
            merged = _merge_in(worktree, branch, base, remote)
        except Refusal as exc:
            exc.data["reapplied"] = restore_stash(worktree, branch, stashed)
            raise
        answer = {"repo": repo.name, "branch": branch}
        answer.update(merged)
        if merged.get("merge_in_progress"):
            answer["reapplied"] = [{"path": name, "status": "held"} for name in stashed]
        else:
            answer["reapplied"] = restore_stash(worktree, branch, stashed)
        return answer


def conflicts(bench, args):
    """Trial-merges the base into a worktree and gives the unmerged paths.

    The merge is aborted before the answer, so the worktree is left as it
    was. A dirty worktree is refused rather than merged over.
    """
    repo = bench.repo(args.get("repo"))
    branch = check_branch(_text(args, "branch", required=True))
    base = check_branch(_text(args, "base", default=repo.default_branch))
    with bench.lock(repo.name):
        worktree = bench.worktree(repo, branch)
        bench.no_landing(repo, branch)
        dirty = status_paths(worktree)
        if dirty:
            raise Refusal("dirty", branch=branch, paths=dirty[:50],
                          remedy="submit or discard the edits, then ask again")
        bare = bench.fetch(repo)
        remote = base_ref_of(bare, base)
        code, text, err = git.run(["merge", "--no-commit", "--no-ff", remote],
                                  cwd=worktree, check=False)
        try:
            paths = unmerged_paths(worktree) if code != 0 else []
            if code != 0 and not paths:
                raise Refusal("merge_failed", branch=branch, base=base,
                              reason=(err or text).strip()[:400])
        finally:
            # An up-to-date merge leaves no MERGE_HEAD, so the abort may fail.
            git.run(["merge", "--abort"], cwd=worktree, check=False)
        return {"repo": repo.name, "branch": branch, "base": base, "paths": paths}


def submit(bench, args):
    """Commits every change with the trailers, then pushes, or lands."""
    repo = bench.repo(args.get("repo"))
    branch = check_branch(_text(args, "branch", required=True))
    message = _text(args, "message", required=True)
    max_lines = args.get("max_lines")
    trailers = args.get("trailers")
    if trailers is None:
        # Without trailers, the seat and the sitting of the call are the trailers.
        trailers = ["Waymark-%s: %s" % (key.capitalize(), value)
                    for key, value in sorted(marks_of(args).items())]
    if isinstance(trailers, dict):
        trailers = ["%s: %s" % (key, value) for key, value in sorted(trailers.items())]
    if not isinstance(trailers, list):
        raise Refusal("input", field="trailers", reason="give a list of 'Key: value' texts")
    clean_trailers = []
    for item in trailers:
        if not isinstance(item, str) or "\n" in item or ":" not in item:
            raise Refusal("input", field="trailers", reason="each trailer is one 'Key: value' text")
        clean_trailers.append(item.strip())
    if branch == repo.default_branch:
        raise Refusal("default_branch", branch=branch, default_branch=repo.default_branch,
                      remedy="submit on a work branch, not on the default branch")
    land = repo.land
    if land and branch == land.target:
        raise Refusal("default_branch", branch=branch, default_branch=land.target,
                      remedy="submit on a work branch, not on the target branch")
    wait = _int(args, "wait", DEFAULT_WAIT, 0, CEILING_WAIT)
    max_bytes = _int(args, "max_bytes", DEFAULT_MAX_BYTES, 1024, CEILING_MAX_BYTES)
    want_pr = args.get("pull_request", True)
    title = _text(args, "title", default=message.strip().splitlines()[0][:200])
    description = _text(args, "description", default="")
    with bench.lock(repo.name):
        worktree = bench.worktree(repo, branch)
        bench.no_landing(repo, branch)
        paths = status_paths(worktree)
        committed = False
        files = added = removed = 0
        target = land.target if land else bench.base_of(repo, branch)
        against = "HEAD"
        if paths:
            if git.ref_exists("MERGE_HEAD", cwd=worktree):
                marked = marked_paths(worktree, unmerged_paths(worktree))
                if marked:
                    raise Refusal("conflicts", paths=marked, merge_in_progress=True,
                                  remedy="resolve the conflict markers in these paths, "
                                         "then submit again")
            # A refusal puts the index back byte for byte, as _scratch_commit does:
            # a reset keeps MERGE_HEAD but drops the unmerged entries, and a later
            # pull's finish_merge would then find none and commit the markers.
            index = os.path.join(worktree, git.line(["rev-parse", "--git-path", "index"],
                                                    cwd=worktree))
            saved = index + ".bench-submit"
            shutil.copyfile(index, saved)
            git.run(["add", "-A", "--", "."], cwd=worktree)
            try:
                against = _against_of(bench, repo, worktree, target,
                                      fetch=max_lines is not None)
            except BaseException:
                os.replace(saved, index)
                raise
            stat = git.out(["diff", "--cached", "--numstat", against], cwd=worktree)
            workflows = []
            for row in stat.splitlines():
                cells = row.split("\t")
                if len(cells) < 3:
                    continue
                files += 1
                if ".github/workflows/" in cells[2]:
                    workflows.append(cells[2])
                if cells[0].isdigit():
                    added += int(cells[0])
                if cells[1].isdigit():
                    removed += int(cells[1])
            if max_lines is not None:
                ceiling = _int(args, "max_lines", 0, 0, 1000000)
                if added + removed > ceiling:
                    os.replace(saved, index)
                    raise Refusal("over_ceiling", lines=added + removed, max_lines=ceiling,
                                  files=files, against=against, target=target,
                                  remedy="make the change smaller, or raise the ceiling")
            hosted = hosted_runner_findings(worktree, against, git.out(
                ["-c", "core.quotePath=false", "diff", "--cached", "--name-only", "--no-renames",
                 "--diff-filter=d", against, "--", ".github/workflows"],
                cwd=worktree).splitlines(), cached=True, allowed=repo.hosted_workflows)
            if hosted:
                os.replace(saved, index)
                raise Refusal("hosted_runner", findings=hosted,
                              house_labels=list(HOUSE_RUNNER_LABELS),
                              reason="the change adds or changes a runs-on to a GitHub-hosted "
                                     "runner, and CI runs on the house's runners only",
                              remedy="give runs-on the house's labels in these lines, "
                                     "then submit again")
            credential = bench.credentials.get(repo.name) or {}
            if workflows and "workflows" in (credential.get("missing") or []):
                # GitHub rejects the push of a workflow file without it.
                os.replace(saved, index)
                raise Refusal("missing_workflow_permission", repo=repo.name,
                              permission="workflows", classic_scope="workflow", paths=workflows,
                              reason="the rig's GitHub token lacks the workflows permission, "
                                     "so GitHub would reject the push",
                              remedy="give the token Workflows: read and write (classic: the "
                                     "workflow scope), or leave .github/workflows/ out of the change")
            commit_args = ["commit", "-m", message]
            for item in clean_trailers:
                commit_args += ["--trailer", item]
            code, text, err = git.run(commit_args, cwd=worktree, check=False)
            if code != 0:
                os.replace(saved, index)
                raise Refusal("commit_failed", reason=(err or text).strip()[:400])
            os.remove(saved)
            clear_written(worktree)
            committed = True
        elif not land or not _has_work_to_land(bench, repo, branch, worktree):
            raise Refusal("nothing_to_commit", repo=repo.name, branch=branch)
        commit = head_of(worktree)
        if not land:
            code, text, err = git.run(["push", "origin", "HEAD:refs/heads/" + branch],
                                      cwd=worktree, check=False, timeout=600)
            if code != 0:
                raise Refusal("push_rejected", commit=commit,
                              reason=(err or text).strip()[:400],
                              remedy="use pull from head, then submit again")
            return {
                "repo": repo.name,
                "branch": branch,
                "commit": commit,
                "pushed": True,
                "files": files,
                "lines_added": added,
                "lines_removed": removed,
                "against": against,
            }
        item = bench.landings.get(repo, branch, create=True)
        item.start(commit, bool(want_pr), title, description, clean_trailers,
                   marks=marks_of(args), answers=_text(args, "for", default=None))
    item.wait(wait)
    view = item.view(max_bytes)
    answer = {
        "repo": repo.name,
        "branch": branch,
        "commit": commit,
        "committed": committed,
        "files": files,
        "lines_added": added,
        "lines_removed": removed,
        "against": against,
        "pushed": view["pushed"],
        "landing": view,
    }
    if view["state"] == "failed":
        raise Refusal("landing_failed", step=view["failed_step"], reason=view["reason"],
                      remedy="read landing.steps, fix the worktree, then submit again", **answer)
    return answer


def _against_of(bench, repo, worktree, target, fetch=False):
    """Gives the commit the change's size is counted against.

    It is the merge base of the change with its target: what the pull
    request's diff shows. A merge of the base that the change carries is
    on both sides of that diff, so only the change's own lines count.
    While a merge is in progress, MERGE_HEAD is one side of the change.
    Without a target ref or a merge base, the count is against HEAD.
    """
    bare = bench.bare_dir(repo.name)
    if fetch:
        git.run(["fetch", "--prune", "origin"], cwd=bare, check=False, timeout=600)
    try:
        target_ref = base_ref_of(bare, target)
    except Refusal:
        return "HEAD"
    heads = ["HEAD"]
    if git.ref_exists("MERGE_HEAD", cwd=worktree):
        heads.append("MERGE_HEAD")
    # merge-base A B C: the base of A and a merge of B and C.
    code, text, _ = git.run(["merge-base", target_ref] + heads, cwd=worktree, check=False)
    if code != 0 or not text.strip():
        return "HEAD"
    return text.split()[0]


def _has_work_to_land(bench, repo, branch, worktree):
    """Tells if a clean worktree still has a landing to do."""
    remote = "refs/remotes/origin/" + branch
    if not git.ref_exists(remote, cwd=worktree):
        return True
    if git.rev_parse(remote, cwd=worktree) != head_of(worktree):
        return True
    item = bench.landings.get(repo, branch)
    return item is not None and item.state.get("state") != "landed"


def feedback(bench, args):
    """Gathers what the change caused: the landing, the pull request, the pipelines, the reviews."""
    repo = bench.repo(args.get("repo"))
    branch = check_branch(_text(args, "branch", required=True))
    max_bytes = _int(args, "max_bytes", DEFAULT_MAX_BYTES, 1024, CEILING_MAX_BYTES)
    log_bytes = _int(args, "log_bytes", 4096, 256, 32768)
    bench.worktree(repo, branch)
    land = repo.land
    item = bench.landings.get(repo, branch)
    answer = {
        "repo": repo.name,
        "branch": branch,
        "target": land.target if land else repo.default_branch,
        "landing": item.view(max_bytes // 2) if item else None,
        "pull_request": None,
        "pipelines": [],
        "statuses": [],
        "comments": [],
        "findings": [],
        "unavailable": [],
    }
    findings = []
    if item:
        findings.extend(item.findings(max_bytes // 2))
    if not land or land.pull_request is None:
        answer["unavailable"].append("forge: no pull_request block in the land block of bench.json")
        answer["findings"] = findings
        return answer
    try:
        client = forge.client(repo)
    except forge.ForgeError as exc:
        answer["unavailable"].append("forge: %s" % exc)
        answer["findings"] = findings
        return answer

    def attempt(name, function, default):
        try:
            return function()
        except forge.ForgeError as exc:
            answer["unavailable"].append("%s: %s" % (name, exc))
            return default

    target = land.target
    pr = attempt("pull_request", lambda: client.find_pull_request(branch, target), None)
    if pr is None and item and (item.state.get("pull_request") or {}).get("number"):
        number = item.state["pull_request"]["number"]
        pr = attempt("pull_request", lambda: client.pull_request(number), None)
    answer["pull_request"] = pr
    if pr and pr.get("state") in ("declined", "closed", "superseded"):
        findings.append({"source": "pull_request", "severity": "error",
                         "message": "the pull request is %s" % pr["state"], "url": pr.get("url")})
    if pr and pr.get("changes_requested"):
        findings.append({"source": "review", "severity": "error",
                         "message": "changes requested by %s" % ", ".join(pr["changes_requested"]),
                         "url": pr.get("url")})
    auto = (item.state.get("auto_merge") if item else None) or {}
    if auto.get("refused"):
        findings.append({"source": "auto_merge", "severity": "warning",
                         "message": "auto-merge is not on: %s" % auto["refused"],
                         "url": pr.get("url") if pr else None})

    head = (pr or {}).get("head") or (item.state.get("head") if item else None)
    pipelines = attempt("pipelines", lambda: client.pipelines(branch), [])
    chosen = pipelines_of_head(pipelines, head)
    answer["pipelines"] = pipelines[:5] + [p for p in chosen if p not in pipelines[:5]]
    read = []  # (pipeline, step, log) of each failed step; log is None when none came
    dead = []  # (pipeline, step, None) of each job of an interrupted pipeline
    for pipeline in chosen:
        if pipeline.get("result") in ("failed", "error", "failure", "stopped", "cancelled",
                                      "timed_out"):
            steps = attempt("steps", lambda: client.steps(pipeline["id"]), [])
            pipeline["steps"] = steps
            stopped = interrupted_of(steps)
            if stopped:
                dead.extend((pipeline, step, None) for step in stopped)
                findings.append({
                    "source": "pipeline", "severity": "interrupted",
                    "message": INTERRUPTED % ", ".join(step.get("name") or "job" for step in stopped),
                    "jobs": [step.get("name") for step in stopped], "url": pipeline.get("url"),
                })
                continue
            for step in steps:
                if step.get("result") not in ("failed", "error", "failure"):
                    continue
                try:
                    log = client.step_log(pipeline["id"], step["id"]) or ""
                except forge.ForgeError as exc:
                    log = "(no log: %s)" % exc
                if not log.strip() or log.startswith("(no log:"):
                    reason = log[len("(no log: "):-1] if log.strip() else "the forge gave an empty log"
                    answer["unavailable"].append("log: %s %s: %s" % (
                        pipeline.get("kind") or "pipeline", step.get("name"), reason))
                    read.append((pipeline, step, None))
                    marked = []
                else:
                    read.append((pipeline, step, log))
                    cleaned = remember_log(bench, repo, pipeline["id"], step["id"], log)
                    marked = [i + 1 for i, line in enumerate(cleaned) if LOG_MARKERS.search(line)]
                log = log_tail(log, log_bytes)
                findings.append({
                    "source": "pipeline", "step": step.get("name"), "severity": "error",
                    "message": log + LOG_HINT % json.dumps(step.get("name")),
                    "locations": landing_module.locations(log),
                    "url": pipeline.get("url"), "job": step.get("name"), "lines": marked[:50],
                })
        elif pipeline.get("state") in ("in_progress", "pending", "queued", "inprogress"):
            findings.append({"source": "pipeline", "severity": "info",
                             "message": "the pipeline is still running", "url": pipeline.get("url")})

    if head:
        statuses = attempt("statuses", lambda: client.statuses(head), [])
        answer["statuses"] = statuses
        for status in statuses:
            if status.get("state") in ("failed", "failure", "error", "stopped"):
                finding = {
                    "source": "status", "name": status.get("name"), "severity": "error",
                    "message": status.get("description") or "%s failed" % status.get("name"),
                    "url": status.get("url"),
                }
                if step_of_status(status, dead):
                    finding["severity"] = "interrupted"
                    finding["message"] = INTERRUPTED % status.get("name")
                    findings.append(finding)
                    continue
                found = step_of_status(status, read)
                if found and found[2] is not None:
                    pipeline, step, _ = found
                    finding["message"] += ": the log of step %s is in the pipeline finding of %s" % (
                        step.get("name"), pipeline.get("url"))
                    finding["log_in"] = {"source": "pipeline", "step": step.get("name"),
                                         "url": pipeline.get("url")}
                elif not found and status_of_pipeline(status, pipelines):
                    answer["unavailable"].append(
                        "log: %s: the check belongs to a pipeline whose failed step log was not read"
                        % status.get("name"))
                findings.append(finding)

    if pr and pr.get("number") is not None:
        comments = attempt("comments", lambda: client.comments(pr["number"]), [])
        answer["comments"] = comments[:200]
        for comment in comments:
            findings.append({
                "source": "review", "severity": "comment", "author": comment.get("author"),
                "path": comment.get("path"), "line": comment.get("line"),
                "message": comment.get("text"), "created": comment.get("created"),
                "url": comment.get("url"), "reply_to": comment.get("reply_to"),
            })
    answer["findings"] = findings
    return cap_answer(answer, max_bytes)


# The lines of a test report that must survive the cut of a log: clojure.test
# and the like name the failure, the two values and the count.
LOG_MARKERS = re.compile(
    r"FAIL in|ERROR in|expected:|actual:|Ran \d+ tests|\d+ failures?, \d+ errors?"
    r"|\d+ tests?, \d+ assertions?, \d+ errors?, \d+ failures?|Uncaught exception|Exception: |ExceptionInfo")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
LOG_AFTER_MARK = 8
# A thrown error prints `Execution error (Class) at ...`, then its message on the
# lines after it, up to a blank line or a stack frame.
EXECUTION_ERROR = "Execution error ("
EXECUTION_ERROR_LINES = 20
# GitHub marks a failed step ##[error], and opens each step with ##[group]Run and
# its header's ##[endgroup]. A job log's tail is the post-job cleanup, so a
# failure with no test marker takes the failed step's last lines instead.
LOG_ERROR = "##[error]"
LOG_STEP_BOUNDS = ("##[endgroup]", "##[group]Run ")
LOG_STEP_LINES = 12
# GitHub starts each line of a job log with the time it was written.
LOG_STAMP = re.compile(r"^﻿?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ?")
LOG_CACHE_SECONDS = 3600
LOG_HINT = "\nread more with bench__log {job: %s}"
LOG_MODES = ("markers", "grep", "range")


# A job that was cancelled, timed out or stopped in setup ran no test: its
# red is the runner's, and rerun starts it again.
INTERRUPTED = "ci: interrupted: %s stopped before a test ran; call rerun to start it again"


def interrupted_of(steps):
    """Gives the interrupted jobs of a pipeline, or [] when a job is red."""
    if any(step.get("result") in ("failed", "error", "failure") and not step.get("interrupted")
           for step in steps):
        return []
    return [step for step in steps if step.get("interrupted")]


def pipelines_of_head(pipelines, head):
    """Gives the newest pipeline of each workflow on the head commit.

    A forge runs several workflows on one push, and the newest of them is
    not always the one that failed. Without a head, or when no pipeline
    names it, the newest pipeline of each workflow counts.
    """
    on_head = [p for p in pipelines if head and p.get("commit") == head]
    chosen = []
    kinds = set()
    for pipeline in on_head or pipelines:
        kind = pipeline.get("kind")
        if kind in kinds:
            continue
        kinds.add(kind)
        chosen.append(pipeline)
    return chosen


def step_of_status(status, read):
    """Gives the (pipeline, step, log) whose job made a failed status, or None."""
    url = status.get("url") or ""
    for pipeline, step, log in read:
        if step.get("name") and step.get("name") == status.get("name"):
            return pipeline, step, log
        if step.get("id") is not None and url.endswith("/job/%s" % step["id"]):
            return pipeline, step, log
    return None


def status_of_pipeline(status, pipelines):
    """Tells if a status is a job of one of the pipelines."""
    url = status.get("url") or ""
    if "/actions/runs/" in url:
        return True
    return any(p.get("url") and url.startswith(p["url"] + "/") for p in pipelines)


def log_tail(text, size):
    """Gives the end of a log in size bytes, keeping the lines of the test report.

    The colors come off first. The marked lines lead: each marker, then the
    lines around it (a few after, for an exception's first frames), and the
    rest of the room goes to the last lines. A gap is "...". A log with no
    marker gives the failed step's lines up to its ##[error], not the post-job
    cleanup; the plain tail only when neither is found.
    """
    text = ANSI_ESCAPE.sub("", text or "")
    if not text or len(text.encode("utf-8", "replace")) <= size:
        return text
    lines = text.splitlines()
    marks = [i for i, line in enumerate(lines) if LOG_MARKERS.search(line)]
    if not marks:
        step = _failed_step_lines(clean_log(text))
        if step:
            return landing_module.tail("\n".join(step), size)
        return landing_module.tail(text, size)

    def cost(index):
        return len(lines[index].encode("utf-8", "replace")) + 5

    keep = set()
    used = 0
    for mark in marks:
        if used + cost(mark) <= size:
            keep.add(mark)
            used += cost(mark)
    for mark in marks:
        window = [i for i in range(max(0, mark - 2), min(len(lines), mark + 1 + LOG_AFTER_MARK))
                  if i not in keep]
        extra = sum(cost(i) for i in window)
        if used + extra > size:
            continue
        keep.update(window)
        used += extra
    index = len(lines) - 1
    while index >= 0 and used + cost(index) <= size:
        if index not in keep:
            keep.add(index)
            used += cost(index)
        index -= 1
        while index in keep:
            index -= 1
    if not keep:
        return landing_module.tail(text, size)
    out = []
    last = -1
    for i in sorted(keep):
        if i != last + 1:
            out.append("...")
        out.append(lines[i])
        last = i
    return "\n".join(out)


def clean_log(text):
    """Gives the lines of a log, without the colors and without GitHub's timestamps."""
    return [LOG_STAMP.sub("", ANSI_ESCAPE.sub("", line)) for line in (text or "").splitlines()]


def cut_line(text, width):
    """Cuts a line at width characters. A cut line ends in '… (+N)', N the characters dropped."""
    if len(text) <= width:
        return text
    return "%s… (+%d)" % (text[:width], len(text) - width)


def remember_log(bench, repo, run_id, job_id, text):
    """Cleans a job's log, keeps its lines for an hour, and gives them."""
    lines = clean_log(text)
    with bench._guard:
        now = _clock()
        for key in [key for key, (at, _) in bench.logs.items() if now - at > LOG_CACHE_SECONDS]:
            del bench.logs[key]
        bench.logs[(repo.name, run_id, job_id)] = (now, lines)
    return lines


def job_lines(bench, repo, client, run_id, job_id):
    """Gives a job's cleaned lines: from the rig when read within the hour, else from the forge."""
    with bench._guard:
        held = bench.logs.get((repo.name, run_id, job_id))
    if held and _clock() - held[0] <= LOG_CACHE_SECONDS:
        return held[1]
    try:
        text = client.step_log(run_id, job_id) or ""
    except forge.ForgeError as exc:
        text = "(no log: %s)" % exc
    if not text.strip() or text.startswith("(no log:"):
        raise Refusal("log", reason=text.strip() or "the forge gave an empty log")
    return remember_log(bench, repo, run_id, job_id, text)


def log(bench, args):
    """Reads one job's log of the newest run on a branch's head: its jobs, the
    marked lines, a grep, or a range. Every line comes cleaned and cut."""
    repo = bench.repo(args.get("repo"))
    branch = check_branch(_text(args, "branch", required=True))
    max_bytes = _int(args, "max_bytes", DEFAULT_MAX_BYTES, 256, CEILING_MAX_BYTES)
    name = _text(args, "job")
    mode = _text(args, "mode", default="markers")
    if mode not in LOG_MODES:
        raise Refusal("input", field="mode", reason="one of markers, grep, range")
    width = _int(args, "width", 200, 80, 400)
    item = bench.landings.get(repo, branch)
    head = item.state.get("head") if item else None
    run_id = args.get("run_id")
    try:
        client = forge.client(repo)
        jobs = []
        runs = client.pipelines(branch)
        if run_id is None:
            runs = pipelines_of_head(runs, head)
        else:
            known = [p.get("id") for p in runs]
            runs = [p for p in runs if str(p.get("id")) == str(run_id)]
            if not runs:
                raise Refusal("run", repo=repo.name, branch=branch, run_id=run_id, runs=known,
                              reason="no run with this id is among the branch's newest runs")
        for run in runs:
            jobs.extend((run, job) for job in client.steps(run["id"]))
    except forge.ForgeError as exc:
        raise Refusal("forge", reason=str(exc))
    answer = {"repo": repo.name, "branch": branch}
    if not name:
        answer["jobs"] = []
        for run, job in jobs:
            count = None
            if job.get("result") in ("failed", "error", "failure"):
                try:
                    count = len(job_lines(bench, repo, client, run["id"], job["id"]))
                except Refusal:
                    pass
            answer["jobs"].append({"job": job.get("name"), "workflow": run.get("kind"),
                                   "result": job.get("result"), "lines": count})
        return answer
    found = [(run, job) for run, job in jobs if job.get("name") == name]
    if not found:
        raise Refusal("job", job=name, jobs=[job.get("name") for _, job in jobs])
    run, job = found[0]
    lines = job_lines(bench, repo, client, run["id"], job["id"])
    answer.update({"job": name, "workflow": run.get("kind"), "mode": mode, "total": len(lines)})

    def row(index):
        return {"line": index + 1, "text": cut_line(lines[index], width)}

    if mode == "range":
        offset = _int(args, "offset", 1, 1, max(1, len(lines)))
        limit = _int(args, "limit", 60, 1, 200)
        rows = [row(i) for i in range(offset - 1, min(len(lines), offset - 1 + limit))]
        answer.update({"offset": offset, "eof": offset - 1 + limit >= len(lines)})
        key = "lines"
    else:
        pattern = LOG_MARKERS
        if mode == "grep":
            text = _text(args, "pattern", required=True)
            try:
                pattern = re.compile(text)
            except re.error as exc:
                raise Refusal("input", field="pattern", reason=str(exc))
        limit = _int(args, "limit", 20, 1, 100)
        context = _int(args, "context", 2, 0, 10)
        hits = [i for i, line in enumerate(lines) if pattern.search(line)]
        shown = set()
        for i in hits[:limit]:
            shown.update(range(max(0, i - context), min(len(lines), i + context + 1)))
        rows = []
        for i in sorted(shown):
            rows.append(row(i))
            if i in hits[:limit]:
                rows[-1]["hit"] = True
        answer.update({"count": len(hits), "truncated": len(hits) > limit})
        key = "matches"
    answer[key], answer["dropped"] = cap_items(
        rows, max_bytes, lambda item: len(item["text"].encode("utf-8", "replace")) + 32)
    return answer


def cap_answer(answer, max_bytes):
    """Trims the longest texts of an answer until it fits."""
    def size():
        return len(json.dumps(answer))
    if size() <= max_bytes:
        return answer
    for key in ("comments", "statuses", "pipelines"):
        while answer.get(key) and size() > max_bytes:
            answer[key] = answer[key][:-1]
    for finding in answer.get("findings", []):
        if size() <= max_bytes:
            break
        finding["message"] = landing_module.tail(finding.get("message", ""), 1024)
    if size() > max_bytes and answer.get("landing"):
        answer["landing"] = {k: v for k, v in answer["landing"].items() if k != "steps"}
    answer["dropped"] = True
    return answer


def discard(bench, args):
    """Puts the worktree back to the branch head, or removes it."""
    repo = bench.repo(args.get("repo"))
    branch = check_branch(_text(args, "branch", required=True))
    drop = bool(args.get("drop_branch"))
    with bench.lock(repo.name):
        worktree = bench.worktree(repo, branch)
        bench.no_landing(repo, branch)
        bare = bench.bare_dir(repo.name)
        if drop:
            bench.landings.forget(repo, branch)
            git.run(["worktree", "remove", "--force", worktree], cwd=bare, check=False)
            if os.path.isdir(worktree):
                shutil.rmtree(worktree, ignore_errors=True)
            git.run(["worktree", "prune"], cwd=bare, check=False)
            git.run(["branch", "-D", branch], cwd=bare, check=False)
            meta = bench.meta_read(repo.name)
            meta.pop(branch, None)
            bench.meta_write(repo.name, meta)
            return {"repo": repo.name, "branch": branch, "head": None, "dirty": 0,
                    "dropped": True}
        remote = "refs/remotes/origin/" + branch
        target = remote if git.ref_exists(remote, cwd=worktree) else "HEAD"
        git.run(["merge", "--abort"], cwd=worktree, check=False)
        git.run(["reset", "--hard", target], cwd=worktree)
        git.run(["clean", "-fd"], cwd=worktree)
        clear_written(worktree)
        return {"repo": repo.name, "branch": branch, "head": head_of(worktree), "dirty": 0,
                "dropped": False}


MERGE_METHODS = ("merge", "squash", "rebase")


def merge(bench, args):
    """Merges one pull request when its required checks are green on its head."""
    repo = bench.repo(args.get("repo"))
    number = args.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise Refusal("input", field="number", reason="the pull request number is necessary")
    head_sha = _text(args, "head_sha", required=True)
    required = args.get("required_checks")
    if not isinstance(required, list) or not all(
            isinstance(name, str) and name for name in required):
        raise Refusal("input", field="required_checks",
                      reason="a list of check names is necessary")
    if not required:
        raise Refusal("no_required_checks",
                      reason="the rig never merges a change nothing has tested")
    method = _text(args, "method", default="merge")
    if method not in MERGE_METHODS:
        raise Refusal("input", field="method", reason="use merge, squash or rebase")
    try:
        answer = forge.client(repo).merge_when_green(number, head_sha, required, method)
    except forge.ForgeError as exc:
        raise Refusal("forge", reason=git.scrub(str(exc)))
    if answer.get("refused"):
        raise Refusal(answer.pop("refused"), **answer)
    return dict(answer, repo=repo.name, number=number)


def update_branch(bench, args):
    """Merges the base into one pull request's branch, so it is up to date."""
    repo = bench.repo(args.get("repo"))
    number = args.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise Refusal("input", field="number", reason="the pull request number is necessary")
    head_sha = _text(args, "head_sha", required=True)
    try:
        answer = forge.client(repo).update_branch(number, head_sha)
    except forge.ForgeError as exc:
        raise Refusal("forge", reason=git.scrub(str(exc)))
    if answer.get("refused"):
        raise Refusal(answer.pop("refused"), **answer)
    return dict(answer, repo=repo.name, number=number)


def _rerun_failed(client, repo, branch, head, run_id):
    """Asks the forge to start one run's failed jobs again. A 403 is the token's."""
    try:
        client.rerun_failed_jobs(run_id)
    except forge.ForgeError as exc:
        if exc.status != 403:
            raise
        raise Refusal("token_lacks_actions_write", repo=repo.name, branch=branch,
                      head=head, run_id=run_id,
                      reason="GitHub refused the re-run: the rig's token needs "
                             "Actions: read and write (docs/credential.md)")


def rerun_one(bench, repo, branch, head, run_id):
    """Starts the failed jobs of one finished run on the branch's pushed head again,
    one time per run. A red test step counts too: a test can fail in a path the
    change does not touch."""
    if run_id in ((bench.meta_read(repo.name).get(branch) or {}).get("rerun_runs") or []):
        raise Refusal("already_rerun", repo=repo.name, branch=branch, head=head, run_id=run_id,
                      reason="this run was started again once already: a second failure is "
                             "real, so read log, fix it and submit")
    try:
        client = forge.client(repo)
        found = [p for p in client.pipelines(branch)
                 if str(p.get("id")) == run_id and p.get("commit") == head]
        if not found:
            raise Refusal("not_own_head", repo=repo.name, branch=branch, head=head, run_id=run_id,
                          reason="no run with this id is on the branch's pushed head: a seat "
                                 "re-runs the CI of its own change only")
        run = found[0]
        if run.get("state") != "completed":
            raise Refusal("not_finished", repo=repo.name, branch=branch, head=head, run_id=run_id,
                          url=run.get("url"), reason="the run has not ended: wait for it")
        jobs = [s.get("name") for s in client.steps(run["id"])
                if s.get("interrupted") or s.get("result") in ("failure", "cancelled", "timed_out")]
        if not jobs:
            raise Refusal("nothing_failed", repo=repo.name, branch=branch, head=head,
                          run_id=run_id, url=run.get("url"), reason="the run has no failed job")
        _rerun_failed(client, repo, branch, head, run["id"])
    except forge.ForgeError as exc:
        raise Refusal("forge", reason=git.scrub(str(exc)))
    with bench.lock(repo.name):
        meta = bench.meta_read(repo.name)
        meta.setdefault(branch, {}).setdefault("rerun_runs", []).append(run_id)
        bench.meta_write(repo.name, meta)
    stopped = [{"id": run["id"], "kind": run.get("kind"), "url": run.get("url"), "jobs": jobs}]
    return {"repo": repo.name, "branch": branch, "head": head,
            "run_id": run["id"], "runs": stopped}


def rerun(bench, args):
    """Starts the interrupted CI of the branch's pushed head again, one time per head.
    With run_id it starts that run's failed jobs again, one time per run."""
    repo = bench.repo(args.get("repo"))
    branch = check_branch(_text(args, "branch", required=True))
    run_id = args.get("run_id")
    remote = "refs/remotes/origin/" + branch
    with bench.lock(repo.name):
        worktree = bench.worktree(repo, branch)
        bench.fetch(repo)
        if not git.ref_exists(remote, cwd=worktree):
            raise Refusal("not_pushed", repo=repo.name, branch=branch,
                          reason="the branch is not on the remote: submit pushes it")
        head = git.rev_parse(remote, cwd=worktree)
    if run_id is not None:
        return rerun_one(bench, repo, branch, head, str(run_id))
    if (bench.meta_read(repo.name).get(branch) or {}).get("rerun_head") == head:
        raise Refusal("already_rerun", repo=repo.name, branch=branch, head=head,
                      reason="the CI of this head was started again once already")
    try:
        client = forge.client(repo)
        runs = pipelines_of_head([p for p in client.pipelines(branch)
                                  if p.get("commit") == head], head)
        stopped = []
        for run in runs:
            if run.get("state") != "completed":
                continue
            steps = client.steps(run["id"])
            red = [s.get("name") for s in steps
                   if s.get("result") == "failure" and not s.get("interrupted")]
            if red:
                raise Refusal("red", repo=repo.name, branch=branch, head=head, jobs=red,
                              url=run.get("url"),
                              reason="the run has a red test step: read feedback, fix it and submit")
            jobs = [s.get("name") for s in steps if s.get("interrupted")]
            if jobs:
                stopped.append({"id": run["id"], "kind": run.get("kind"),
                                "url": run.get("url"), "jobs": jobs})
        if not stopped:
            raise Refusal("nothing_interrupted", repo=repo.name, branch=branch, head=head,
                          reason="no finished run on the head has a cancelled, timed out or "
                                 "stopped-in-setup job")
        for run in stopped:
            _rerun_failed(client, repo, branch, head, run["id"])
    except forge.ForgeError as exc:
        raise Refusal("forge", reason=git.scrub(str(exc)))
    with bench.lock(repo.name):
        meta = bench.meta_read(repo.name)
        entry = meta.setdefault(branch, {})
        entry["rerun_head"] = head
        entry.setdefault("rerun_runs", []).extend(str(run["id"]) for run in stopped)
        bench.meta_write(repo.name, meta)
    return {"repo": repo.name, "branch": branch, "head": head,
            "run_id": stopped[0]["id"], "runs": stopped}


def _secret_forge(repo):
    """Gives the GitHub client of a repository: Actions secrets are GitHub's.
    The secret tools use their own token, and refuse when it is not set."""
    client = forge.client(repo)
    if client.provider != "github":
        raise Refusal("not_github", repo=repo.name,
                      reason="Actions secrets are GitHub's; this repository's forge is %s"
                             % client.provider)
    try:
        client.secrets_credential()
    except forge.NoSecretsToken:
        raise Refusal("no_secrets_token", repo=repo.name,
                      reason="secret_set and secret_list use their own token: "
                             "set BENCH_SECRETS_TOKEN (docs/credential.md)")
    return client


def secret_set(bench, args):
    """Writes one Actions secret of an enrolled repository. The value is never
    answered, logged, or put in a refusal."""
    repo = bench.repo(args.get("repo"))
    name = _text(args, "name", required=True)
    if not SECRET_NAME.match(name) or name.upper().startswith("GITHUB_"):
        raise Refusal("input", field="name",
                      reason="a secret name is letters, digits and _, opening with neither "
                             "a digit nor GITHUB_")
    value = _text(args, "value", required=True)
    _text(args, "why")  # Optional: a gate in front of the rig may hold the why itself.
    try:
        updated_at = _secret_forge(repo).set_secret(name, value)
    except Refusal:
        raise
    except forge.ForgeError as exc:
        raise Refusal("forge", repo=repo.name, secret=name,
                      reason=git.scrub(str(exc)).replace(value, "[secret]"))
    except Exception as exc:  # Its message may carry the value: name its type only.
        raise Refusal("error", repo=repo.name, secret=name,
                      reason="the secret was not written: %s" % type(exc).__name__)
    return {"repo": repo.name, "name": name, "updated_at": updated_at}


def secret_list(bench, args):
    """Gives the names of a repository's Actions secrets and when each was set."""
    repo = bench.repo(args.get("repo"))
    try:
        return {"repo": repo.name, "secrets": _secret_forge(repo).secrets()}
    except forge.ForgeError as exc:
        raise Refusal("forge", repo=repo.name, reason=git.scrub(str(exc)))


def dispatch(bench, args):
    """Starts one workflow_dispatch workflow of a repository on a ref. Answers
    the run that dispatch started, or run_id null when no run showed within
    about ten seconds."""
    repo = bench.repo(args.get("repo"))
    workflow = _text(args, "workflow", required=True)
    if not WORKFLOW_NAME.match(workflow):
        raise Refusal("input", field="workflow",
                      reason="a workflow is its file name under .github/workflows, or its id")
    ref = check_branch(_text(args, "ref", default=repo.default_branch))
    inputs = args.get("inputs")
    if inputs is None:
        inputs = {}
    if not isinstance(inputs, dict):
        raise Refusal("input", field="inputs", reason="the inputs are an object: name to value")
    _text(args, "why")  # Optional: a gate in front of the rig may hold the why itself.
    try:
        client = forge.client(repo)
        before = {run["id"] for run in client.workflow_runs(workflow, ref)}
        dispatched_at = _utc_stamp(time.time() - TEST_SKEW_SECONDS)
        try:
            client.dispatch_workflow(workflow, ref, inputs)
        except forge.ForgeError as exc:
            if exc.status != 403:
                raise
            raise Refusal("token_lacks_actions_write", repo=repo.name, workflow=workflow, ref=ref,
                          reason="GitHub refused the dispatch: the rig's token needs "
                                 "Actions: read and write (docs/credential.md)")
        answer = {"repo": repo.name, "workflow": workflow, "ref": ref, "run_id": None,
                  "run_url": None, "dispatched_at": dispatched_at}
        for attempt in range(DISPATCH_FIND_TRIES):
            runs = [run for run in client.workflow_runs(workflow, ref)
                    if run["id"] not in before]
            if runs:
                answer.update(run_id=runs[0]["id"], run_url=runs[0].get("url"))
                return answer
            if attempt + 1 < DISPATCH_FIND_TRIES:
                _sleep(DISPATCH_FIND_SECONDS)
        return answer
    except forge.ForgeError as exc:
        raise Refusal("forge", repo=repo.name, workflow=workflow, reason=git.scrub(str(exc)))


def run_status(bench, args):
    """Gives one workflow run's status, its conclusion and the failed steps of
    each failed job."""
    repo = bench.repo(args.get("repo"))
    run_id = args.get("run_id")
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1:
        raise Refusal("input", field="run_id", reason="a run id is a whole number above zero")
    try:
        client = forge.client(repo)
        run = client.pipeline(run_id)
        failed = [{"job": step.get("name"), "steps": step.get("failed_steps") or []}
                  for step in client.steps(run_id) if step.get("result") == "failure"]
    except forge.ForgeError as exc:
        raise Refusal("forge", repo=repo.name, run_id=run_id, reason=git.scrub(str(exc)))
    return {"repo": repo.name, "run_id": run_id, "run_url": run.get("url"),
            "workflow": run.get("kind"), "ref": run.get("branch"),
            "status": run.get("state"), "conclusion": run.get("result") or None,
            "failed_steps": failed}


def test(bench, args):
    """Dispatches one test selection of a branch on the repository's own CI and
    answers at once with the run it started; test_result reads that run."""
    repo = bench.repo(args.get("repo"))
    spec = _test_spec(repo)
    try:
        return _dispatch_test(bench, repo, spec, forge.client(repo), args)
    except forge.ForgeError as exc:
        raise Refusal("forge", reason=git.scrub(str(exc)))


def _test_spec(repo):
    """Gives the repository's test block, or refuses no_test_workflow."""
    if not repo.test:
        raise Refusal("no_test_workflow", repo=repo.name,
                      reason="bench.json gives this repository no test block {workflow, input}")
    return repo.test


def _test_inputs(spec, args):
    """Gives the dispatch inputs: select under the block's input, or the order
    list joined with spaces under its order_input. Refuses input when both or
    neither are given, or when one selection does not have the repo's shape."""
    order = args.get("order")
    if order is None:
        select = _text(args, "select", required=True)
        _check_selection(spec, "select", select)
        return {spec["input"]: select}
    if args.get("select") is not None:
        raise Refusal("input", field="order",
                      reason="give select or order, not both")
    if (not isinstance(order, list) or not order
            or not all(isinstance(name, str) and name.strip() for name in order)):
        raise Refusal("input", field="order",
                      reason="order must be a non-empty list of test namespaces")
    order = [name.strip() for name in order]
    for name in order:
        _check_selection(spec, "order", name)
    return {spec.get("order_input") or "order": " ".join(order)}


def _check_selection(spec, field, select):
    """Refuses input unless select has the shape of this repository's tests."""
    pattern = spec.get("select_pattern")
    if pattern is not None:
        if not re.fullmatch(pattern, select):
            raise Refusal("input", field=field, select=select, select_pattern=pattern,
                          reason="%s must match this repository's select_pattern %s"
                                 % (field, pattern))
    elif not TEST_SELECT.match(select):
        # a job or make target is not a namespace: the workflow would run no suite
        raise Refusal("input", field=field, select=select,
                      reason="%s must be a test namespace (dotted, ending in -test) "
                             "or namespace/test-name, such as factory10.merge-line-test "
                             "or factory10.merge-line-test/merges-a-line" % field)


def _dispatch_test(bench, repo, spec, client, args):
    """Pushes the worktree to the scratch ref and dispatches the test workflow
    on it: a dirty worktree rides a scratch commit on the head, and the work
    branch never moves. Answers pending with the run that dispatch started, or
    with run_id null when no run showed within about fifteen seconds."""
    branch = check_branch(_text(args, "branch", required=True))
    inputs = _test_inputs(spec, args)
    scratch = TEST_PREFIX + branch
    runs = client.workflow_runs(spec["workflow"], scratch)
    before = {run["id"] for run in runs}
    running = [run for run in runs if run.get("state") != "completed"]
    with bench.lock(repo.name):
        worktree = bench.worktree(repo, branch)
        head = head_of(worktree)
        # a new push to the scratch ref would cancel the run still on it
        if running and _scratch_exists(worktree, scratch):
            raise Refusal("test_running", repo=repo.name, branch=branch,
                          run_id=running[0]["id"], run_url=running[0].get("url"),
                          reason="a test run of this branch is not done: "
                                 "read it with test_result, then test again")
        head, paths = _scratch_commit(worktree, head)
        # the scratch ref, never the pull request's branch
        code, out, err = git.run(["push", "--force", "origin",
                                  (head or "HEAD") + ":refs/heads/" + scratch],
                                 cwd=worktree, timeout=600, check=False)
        if code != 0:
            # no run on the branch head stands in for the worktree the push did not carry
            raise Refusal("scratch_push", repo=repo.name, branch=branch, scratch=scratch,
                          head=head, dirty_included=bool(paths), paths=paths,
                          detail=(err.strip() or out.strip())[-400:],
                          reason="the push of the scratch branch was refused, so nothing "
                                 "was tested: no run was dispatched")
        meta = bench.meta_read(repo.name)
        entry = meta.setdefault(branch, {})
        entry["test_head"] = head
        if paths:
            entry["dirty_heads"] = (entry.get("dirty_heads") or [])[-9:] + [head]
        bench.meta_write(repo.name, meta)
    dispatched_at = _utc_stamp(time.time() - TEST_SKEW_SECONDS)
    client.dispatch_workflow(spec["workflow"], scratch, inputs)
    answer = {"repo": repo.name, "branch": branch, "head": head, "conclusion": "pending",
              "run_id": None, "run_url": None, "dispatched_at": dispatched_at,
              "dirty_included": bool(paths), "paths": paths}
    for attempt in range(TEST_FIND_TRIES):
        runs = [run for run in client.workflow_runs(spec["workflow"], scratch)
                if run["id"] not in before and run.get("commit") == head]
        if runs:
            answer.update(run_id=runs[0]["id"], run_url=runs[0].get("url"))
            return answer
        if attempt + 1 < TEST_FIND_TRIES:
            _sleep(TEST_FIND_SECONDS)
    return answer


def test_result(bench, args):
    """Reads one test run for up to wait_seconds and answers its result. It never
    dispatches, so asking again for the same run is always safe."""
    repo = bench.repo(args.get("repo"))
    spec = _test_spec(repo)
    default = max(0, min(settings.load().test_wait, CEILING_TEST_WAIT))
    wait = _int(args, "wait_seconds", default, 0, CEILING_TEST_WAIT)
    deadline = _clock() + wait
    try:
        client = forge.client(repo)
        if args.get("run_id") is not None:
            run_id = _int(args, "run_id", 0, 1, 2 ** 63)
        else:
            run_id = _find_test_run(bench, repo, spec, client, args, deadline)
            if run_id is None:
                since = _stamp_seconds(args.get("dispatched_at"))
                return {"repo": repo.name, "conclusion": "pending", "run_id": None,
                        "run_url": None, "elapsed_s": _age(since)}
        run = _wait_for_run(client, run_id, max(0, deadline - _clock()))
        answer = {"repo": repo.name, "run_id": run["id"], "run_url": run.get("url"),
                  "branch": run.get("branch"), "head": run.get("commit"),
                  "dirty_included": _dirty_included(bench, repo, run.get("branch"),
                                                    run.get("commit"))}
        start = _stamp_seconds(run.get("created"))
        if run.get("state") != "completed":
            answer.update(conclusion="pending", elapsed_s=_age(start))
            return answer
        end = _stamp_seconds(run.get("completed"))
        answer["conclusion"] = run.get("result") or "unknown"
        answer["duration_s"] = None if None in (start, end) else max(0, end - start)
        if answer["conclusion"] not in ("success", "cancelled", "skipped", "neutral"):
            answer["failures"] = _failures(bench, repo, client, run["id"])
    except forge.ForgeError as exc:
        raise Refusal("forge", reason=git.scrub(str(exc)))
    answer["scratch_deleted"] = _drop_scratch(bench, repo, run.get("branch"), run.get("commit"))
    return answer


def _find_test_run(bench, repo, spec, client, args, deadline):
    """Finds the run a test dispatch started, by its workflow, the scratch ref, the
    head and a created time at or after the dispatch. Gives None past the deadline."""
    branch = check_branch(_text(args, "branch", required=True))
    since = _text(args, "dispatched_at", required=True)
    head = args.get("head") if isinstance(args.get("head"), str) else None
    if not head:
        with bench.lock(repo.name):
            # the head the last test pushed, which a dirty worktree made a scratch commit
            head = ((bench.meta_read(repo.name).get(branch) or {}).get("test_head")
                    or head_of(bench.worktree(repo, branch)))
    while True:
        runs = [run for run in client.workflow_runs(spec["workflow"], TEST_PREFIX + branch)
                if run.get("commit") == head and (run.get("created") or "") >= since]
        if runs:
            # newest first: the earliest run after the dispatch is the one it started
            return runs[-1]["id"]
        left = deadline - _clock()
        if left <= 0:
            return None
        _sleep(min(TEST_POLL_SECONDS, left))


def _scratch_commit(worktree, head):
    """Commits the worktree's uncommitted edits on top of head without moving the
    branch. Gives (commit, paths); a clean worktree gives (head, [])."""
    paths = status_paths(worktree)
    if not paths or not head:
        return head, []
    # The index is put back byte for byte, not reset: a reset drops a merge's
    # MERGE_HEAD and its unmerged entries, and no pull could then conclude it.
    index = os.path.join(worktree, git.line(["rev-parse", "--git-path", "index"], cwd=worktree))
    saved = index + ".bench-scratch"
    shutil.copyfile(index, saved)
    try:
        git.run(["add", "-A"], cwd=worktree)
        tree = git.line(["write-tree"], cwd=worktree)
    finally:
        # the edits stay in the worktree, staged or unstaged as they were
        os.replace(saved, index)
    commit = git.line(["commit-tree", tree, "-p", head, "-m",
                       "bench test: the worktree's uncommitted edits"], cwd=worktree)
    return commit, paths


def _dirty_included(bench, repo, scratch, commit):
    """Gives whether a test run's commit carried a worktree's uncommitted edits."""
    if not scratch or not scratch.startswith(TEST_PREFIX) or not commit:
        return False
    with bench.lock(repo.name):
        entry = bench.meta_read(repo.name).get(scratch[len(TEST_PREFIX):]) or {}
    return commit in (entry.get("dirty_heads") or [])


def _scratch_exists(worktree, scratch):
    """Gives whether the scratch ref is still on the remote."""
    code, out, _ = git.run(["ls-remote", "origin", "refs/heads/" + scratch],
                           cwd=worktree, check=False, timeout=120)
    return code == 0 and bool(out.strip())


def _utc_stamp(seconds):
    """Gives epoch seconds as the forge spells a time, 2026-09-28T12:00:00Z."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def _stamp_seconds(stamp):
    """Gives a forge time such as 2026-09-28T12:00:00Z as epoch seconds, or None."""
    try:
        return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def _age(since):
    """Gives the whole seconds since epoch seconds since, or None."""
    return None if since is None else max(0, int(time.time() - since))


def _failures(bench, repo, client, run_id):
    """Gives each failing test of a run's red jobs with its assertion lines, in
    TEST_FAILURE_BYTES at most. The rig keeps each log for bench__log to page."""
    failures = []
    used = 0
    for job in client.steps(run_id):
        if job.get("result") not in ("failed", "error", "failure"):
            continue
        text = client.step_log(run_id, job["id"]) or ""
        lines = remember_log(bench, repo, run_id, job["id"], text)
        for failure in _failing_tests(job.get("name"), lines):
            size = sum(len(line) + 1 for line in failure["lines"]) + len(failure["test"] or "")
            if used + size > TEST_FAILURE_BYTES:
                room = TEST_FAILURE_BYTES - used - len(failure["test"] or "")
                failures.append(_cut_failure(failure, room))
                failures.append({"test": None, "job": job.get("name"),
                                 "lines": [(LOG_HINT % job["id"]).strip()]})
                return failures
            failures.append(failure)
            used += size
    return failures


def _cut_failure(failure, room):
    """Gives a failure cut to room bytes: its first lines, a gap \"...\", then its
    ##[error] lines, which stay whatever the room."""
    errors = [line for line in failure["lines"] if line.startswith(LOG_ERROR)]
    room -= sum(len(line) + 1 for line in errors) + 4
    head = []
    for line in failure["lines"]:
        if line.startswith(LOG_ERROR) or len(line) + 1 > room:
            break
        head.append(line)
        room -= len(line) + 1
    gap = ["..."] if len(head) + len(errors) < len(failure["lines"]) else []
    return dict(failure, lines=head + gap + errors)


def _failing_tests(job, lines):
    """Gives each test a job's clean log names as failed, with its lines up to the
    next failure or a blank line, in TEST_FAILURE_LINES lines and TEST_FAILURE_CHARS
    characters at most. A log that names none gives its marked lines,
    or the failed step's lines up to its ##[error], or its last lines, under test null."""
    found = []
    for index, line in enumerate(lines):
        match = FAILED_TEST.search(line)
        if not match:
            continue
        block = [line[:TEST_LINE_CHARS]]
        used = len(block[0])
        for after in lines[index + 1:index + TEST_FAILURE_LINES]:
            if not after.strip() or FAILED_TEST.search(after):
                break
            after = after[:TEST_LINE_CHARS]
            if used + len(after) > TEST_FAILURE_CHARS:
                break
            block.append(after)
            used += len(after)
        found.append({"test": match.group(1) or match.group(2), "job": job, "lines": block})
    if not found:
        chosen = _marked_lines(lines) or _failed_step_lines(lines) or lines[-LOG_AFTER_MARK:]
        found.append({"test": None, "job": job,
                      "lines": [text[:TEST_LINE_CHARS] for text in chosen]})
    return found


def _marked_lines(lines):
    """Gives a log's last LOG_AFTER_MARK marked lines. An Execution error line keeps
    its message: the lines after it up to a blank line or a stack frame, in
    EXECUTION_ERROR_LINES lines at most."""
    marked = [index for index, line in enumerate(lines)
              if LOG_MARKERS.search(line) or EXECUTION_ERROR in line][-LOG_AFTER_MARK:]
    kept = []
    for index in marked:
        if kept and index <= kept[-1]:
            continue
        kept.append(index)
        if not lines[index].lstrip().startswith(EXECUTION_ERROR):
            continue
        for after in range(index + 1, min(len(lines), index + EXECUTION_ERROR_LINES)):
            if not lines[after].strip() or lines[after].startswith(("at ", "\tat ")):
                break
            kept.append(after)
    return [lines[index] for index in kept]


def _failed_step_lines(lines):
    """Gives the output of the step a clean job log first marks ##[error], through
    its error lines, in LOG_STEP_LINES at most; [] when no line is so marked."""
    errors = [i for i, line in enumerate(lines) if line.startswith(LOG_ERROR)]
    if not errors:
        return []
    end = errors[0]
    while end + 1 < len(lines) and lines[end + 1].startswith(LOG_ERROR):
        end += 1
    start = 0
    for index in range(errors[0] - 1, -1, -1):
        if lines[index].startswith(LOG_STEP_BOUNDS):
            start = index + 1
            break
    return lines[start:end + 1][-LOG_STEP_LINES:]


def _wait_for_run(client, run_id, wait):
    """Reads the run until it completes or the wait runs out; gives the last read."""
    deadline = _clock() + wait
    while True:
        run = client.pipeline(run_id)
        left = deadline - _clock()
        if run.get("state") == "completed" or left <= 0:
            return run
        _sleep(min(TEST_POLL_SECONDS, left))


def _failed_jobs(client, run_id, log_bytes):
    """Gives each failed job of a run with its failed step and its log tail."""
    failed = []
    for job in client.steps(run_id):
        if job.get("result") not in ("failed", "error", "failure"):
            continue
        names = job.get("failed_steps") or []
        log = client.step_log(run_id, job["id"]) or ""
        failed.append({"job": job.get("name"), "step": names[0] if names else None,
                       "log_tail": log_tail(log, log_bytes)})
    return failed


def _drop_scratch(bench, repo, scratch, commit):
    """Deletes a scratch ref while it still points at commit. Gives whether it did."""
    if not scratch or not scratch.startswith(TEST_PREFIX) or not commit:
        return False
    try:
        branch = check_branch(scratch[len(TEST_PREFIX):])
        with bench.lock(repo.name):
            worktree = bench.worktree(repo, branch)
            code, _, _ = git.run(["push", "--force-with-lease=refs/heads/%s:%s" % (scratch, commit),
                                  "origin", ":refs/heads/" + scratch],
                                 cwd=worktree, check=False, timeout=120)
    except Refusal:
        return False
    return code == 0


def _repo_name(args):
    """Gives the repository name of an enrollment call, or refuses."""
    name = _text(args, "repo", required=True)
    if not REPO_CHARS.match(name):
        raise Refusal("repo", repo=name, reason="the repository name is not permitted")
    return name


def enroll(bench, args):
    """Puts one repository on the rig, and makes its clone."""
    name = _repo_name(args)
    clone_url = _text(args, "clone_url", required=True)
    default_branch = check_branch(_text(args, "default_branch", default="main"))
    deny = _globs(args, "deny")
    try:
        land = config_module.land_from_dict(name, args.get("land"), default_branch)
    except config_module.ConfigError as exc:
        raise Refusal("input", field="land", reason=str(exc))
    try:
        test = config_module.test_from_dict(name, args.get("test"))
    except config_module.ConfigError as exc:
        raise Refusal("input", field="test", reason=str(exc))
    try:
        check = config_module.check_from_dict(name, args.get("check"))
    except config_module.ConfigError as exc:
        raise Refusal("input", field="check", reason=str(exc))
    try:
        hosted_workflows = config_module.hosted_workflows_from_dict(
            name, args.get("hosted_workflows"))
    except config_module.ConfigError as exc:
        raise Refusal("input", field="hosted_workflows", reason=str(exc))
    entry = config_module.RepoConfig(
        name=name, clone_url=clone_url, default_branch=default_branch,
        deny=deny, land=land, source="file", test=test, check=check,
        hosted_workflows=hosted_workflows)
    with bench.lock(name):
        cloned = not bench.bare_exists(name)
        try:
            bare = bench.ensure_bare(entry)
        except git.GitError as exc:
            # The entry stays out of the file. An entry from before stays.
            if cloned:
                shutil.rmtree(bench.bare_dir(name), ignore_errors=True)
            raise Refusal("clone_failed", repo=name,
                          reason=str(exc.stderr).strip()[-600:])
        bench.config.add_repo(entry)
        bench.config.write_repos()
    answer = entry.to_dict()
    answer["bare"] = bare
    answer["cloned"] = cloned
    answer["credential"] = check_credential(bench, entry)
    return answer


def check_credential(bench, repo):
    """Checks the token against what the rig does with the repository's
    forge (docs/credential.md), and keeps the answer on the bench. Only a
    GitHub forge is checked."""
    try:
        client = forge.client(repo)
    except forge.ForgeError as exc:
        found = {"checked": False, "ok": None, "missing": [], "reason": git.scrub(str(exc))}
    else:
        if client.provider == "github":
            found = dict(client.check_credential(repo.default_branch), checked=True)
            found.update(client.check_secrets())
        else:
            found = {"checked": False, "ok": None, "missing": [],
                     "reason": "the rig checks a github credential only"}
    bench.credentials[repo.name] = found
    return found


def repos(bench, args):
    """Gives every repository on the rig, where its entry comes from, and
    the check of its credential."""
    items = []
    for name in bench.config.names():
        entry = bench.config.repos[name]
        item = entry.to_dict()
        item["bare_exists"] = bench.bare_exists(name)
        item["source"] = entry.source
        item["credential"] = bench.credentials.get(name) or check_credential(bench, entry)
        items.append(item)
    return {"repos": items}


def unenroll(bench, args):
    """Takes one repository off the rig. It keeps the clone."""
    name = _repo_name(args)
    with bench.lock(name):
        entry = bench.config.repos.get(name)
        if entry is None:
            raise Refusal("repo", repo=name, reason="the repository is not on the bench",
                          known=bench.config.names())
        if entry.source != "file":
            raise Refusal("config_repo", repo=name,
                          reason="the repository comes from bench.json",
                          remedy="remove it from bench.json, then restart the rig")
        bench.config.drop_repo(name)
        bench.config.write_repos()
    return {"repo": name, "kept": bench.bare_dir(name)}


# ------------------------------------------------------------ merge train

TRAIN_PREFIX = "train/"


def _train_branch(args):
    """Gives the train branch of a call, or refuses a branch that is not train/*."""
    branch = check_branch(_text(args, "branch", required=True))
    if not branch.startswith(TRAIN_PREFIX):
        raise Refusal("not_train", branch=branch, reason="a train branch starts with train/")
    return branch


def _train_base(repo, args):
    base = check_branch(_text(args, "base", default=repo.default_branch))
    if base.startswith(TRAIN_PREFIX):
        raise Refusal("input", field="base", reason="the base is never a train branch")
    return base


def train_build(bench, args):
    """Resets a train branch at the base's head, merges each pull request's head
    into it in order, skips the ones that conflict, and pushes it."""
    repo = bench.repo(args.get("repo"))
    base = _train_base(repo, args)
    branch = _train_branch(args)
    prs = args.get("prs")
    if not isinstance(prs, list) or not prs or not all(
            isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in prs):
        raise Refusal("input", field="prs", reason="a list of pull request numbers is necessary")
    try:
        client = forge.client(repo)
        heads = [(number, client.pull_request(number).get("head")) for number in prs]
    except forge.ForgeError as exc:
        raise Refusal("forge", reason=git.scrub(str(exc)))
    merged, conflicted = [], []
    with bench.lock(repo.name):
        bare = bench.fetch(repo)
        base_head = git.rev_parse("refs/remotes/origin/" + base, cwd=bare)
        path = bench.wt_dir(repo.name, branch)
        git.run(["worktree", "remove", "--force", path], cwd=bare, check=False)
        shutil.rmtree(path, ignore_errors=True)
        git.run(["worktree", "prune"], cwd=bare)
        git.run(["worktree", "add", "--force", "-B", branch, path, base_head], cwd=bare)
        try:
            for number, sha in heads:
                if sha and not git.ref_exists(sha, cwd=path):
                    # a head from a fork is only under the pull request's ref
                    git.run(["fetch", "origin", "refs/pull/%d/head" % number], cwd=bare,
                            check=False, timeout=600)
                code, _, _ = git.run(["merge", "--no-ff", "--no-edit", "-m",
                                      "Merge pull request #%d into %s" % (number, branch),
                                      sha or "MISSING"], cwd=path, check=False, timeout=600)
                if code == 0:
                    merged.append(number)
                else:
                    git.run(["merge", "--abort"], cwd=path, check=False)
                    git.run(["reset", "--hard", "HEAD"], cwd=path, check=False)
                    conflicted.append(number)
            head = head_of(path)
            # a train branch is the rig's own: it is reset, so the push forces
            git.run(["push", "--force", "origin", "HEAD:refs/heads/" + branch],
                    cwd=path, timeout=600)
        finally:
            git.run(["worktree", "remove", "--force", path], cwd=bare, check=False)
    return {"repo": repo.name, "branch": branch, "base_head": base_head, "head": head,
            "merged": merged, "conflicted": conflicted}


def _train_workflow(repo, args):
    workflow = _text(args, "workflow") or (repo.test or {}).get("workflow")
    if not workflow:
        raise Refusal("no_test_workflow", repo=repo.name,
                      reason="give workflow, or a test block in bench.json")
    return workflow


def train_checks(bench, args):
    """Dispatches the check workflow on a pushed train branch, without narrowing."""
    repo = bench.repo(args.get("repo"))
    branch = _train_branch(args)
    workflow = _train_workflow(repo, args)
    inputs = args.get("input") or {}
    if not isinstance(inputs, dict):
        raise Refusal("input", field="input", reason="an object of workflow inputs is necessary")
    with bench.lock(repo.name):
        bare = bench.fetch(repo)
        remote = "refs/remotes/origin/" + branch
        if not git.ref_exists(remote, cwd=bare):
            raise Refusal("not_pushed", repo=repo.name, branch=branch,
                          reason="the train branch is not on the remote: call train_build")
        head = git.rev_parse(remote, cwd=bare)
    run_id = None
    try:
        client = forge.client(repo)
        before = {run["id"] for run in client.workflow_runs(workflow, branch)}
        client.dispatch_workflow(workflow, branch, inputs)
        for attempt in range(TEST_FIND_TRIES):
            runs = [run for run in client.workflow_runs(workflow, branch)
                    if run["id"] not in before and run.get("commit") == head]
            if runs:
                run_id = runs[-1]["id"]
                break
            if attempt + 1 < TEST_FIND_TRIES:
                _sleep(TEST_FIND_SECONDS)
    except forge.ForgeError as exc:
        raise Refusal("forge", reason=git.scrub(str(exc)))
    return {"repo": repo.name, "branch": branch, "workflow": workflow, "run_id": run_id,
            "head": head}


def train_status(bench, args):
    """Reads one train check run: pending, success, failure or cancelled."""
    repo = bench.repo(args.get("repo"))
    try:
        client = forge.client(repo)
        if args.get("run_id") is not None:
            run_id = _int(args, "run_id", 0, 1, 2 ** 63)
            run = dict(client.pipeline(run_id))
            if run.get("id") is None:
                run["id"] = run_id
        else:
            branch = _train_branch(args)
            head = _text(args, "head", required=True)
            skip = None
            if args.get("skip_run_id") is not None:
                skip = _int(args, "skip_run_id", 0, 1, 2 ** 63)
            # every event: the train's pull_request run counts as a dispatched one does
            runs = [run for run in client.workflow_runs(_train_workflow(repo, args), branch,
                                                        event=None)
                    if run.get("commit") == head and (skip is None or run.get("id") != skip)]
            if not runs:
                return {"repo": repo.name, "run_id": None, "state": "pending",
                        "head": head, "url": None}
            run = runs[0]
    except forge.ForgeError as exc:
        raise Refusal("forge", reason=git.scrub(str(exc)))
    state = "pending"
    if run.get("state") == "completed":
        state = run.get("result") if run.get("result") in ("success", "cancelled") else "failure"
    return {"repo": repo.name, "run_id": run.get("id"), "state": state,
            "head": run.get("commit"), "url": run.get("url")}


# the subject train_build gives each merge commit; train_land reads the riders from it
TRAIN_MERGE = re.compile(r"^Merge pull request #(\d+) into ")


def _train_riders(bare, since, head):
    """Names the pull requests train_build merged between since and head."""
    subjects = git.run(["log", "--merges", "--reverse", "--format=%s", since + ".." + head],
                       cwd=bare, check=False)[1]
    return [int(found.group(1)) for found in map(TRAIN_MERGE.match, subjects.splitlines())
            if found]


def _train_pull_request(client, branch, base, riders):
    """Finds the train's one pull request, or opens it: a retry finds the one it
    opened. Answers the pull request and whether this call opened it."""
    pr = client.find_pull_request(branch, base)
    if pr is not None:
        return pr, False
    return client.create_pull_request(
        branch, base, "Merge train: " + (" ".join("#%d" % n for n in riders) or branch),
        "\n".join("- #%d" % n for n in riders) or "A merge train of " + branch + "."), True


def train_open(bench, args):
    """Finds or opens the train's one pull request from its branch into the base,
    with the branch at head, and never merges it."""
    repo = bench.repo(args.get("repo"))
    base = _train_base(repo, args)
    branch = _train_branch(args)
    head = _text(args, "head", required=True)
    with bench.lock(repo.name):
        bare = bench.fetch(repo)
        remote = "refs/remotes/origin/" + branch
        if not git.ref_exists(remote, cwd=bare):
            raise Refusal("not_pushed", repo=repo.name, branch=branch,
                          reason="the train branch is not on the remote: call train_build")
        train = git.rev_parse(remote, cwd=bare)
        if train != head:
            raise Refusal("head_moved", repo=repo.name, branch=branch, head=train,
                          reason="the train branch is not at head")
        riders = _train_riders(bare, "refs/remotes/origin/" + base, head)
    try:
        pr, opened = _train_pull_request(forge.client(repo), branch, base, riders)
    except forge.ForgeError as exc:
        raise Refusal("forge", reason=git.scrub(str(exc)))
    return {"repo": repo.name, "base": base, "branch": branch, "head": head,
            "number": pr.get("number"), "opened": opened}


def train_land(bench, args):
    """Lands the train through one pull request from its branch into the base,
    merged with a merge commit at head, while the base is still where the
    train was built on."""
    repo = bench.repo(args.get("repo"))
    base = _train_base(repo, args)
    branch = _train_branch(args)
    expect = _text(args, "expect_base_head", required=True).strip().lower()
    if len(expect) != 40 or not all(c in "0123456789abcdef" for c in expect):
        raise Refusal("input", field="expect_base_head",
                      reason="a whole sha of 40 hex characters is necessary")
    head = _text(args, "head", required=True)
    with bench.lock(repo.name):
        bare = bench.fetch(repo)
        now = git.rev_parse("refs/remotes/origin/" + base, cwd=bare)
        if now != expect:
            raise Refusal("base_moved", repo=repo.name, base=base, base_head=now,
                          reason="the base is %s, not %s: build the train again" % (now, expect))
        remote = "refs/remotes/origin/" + branch
        train = git.rev_parse(remote, cwd=bare) if git.ref_exists(remote, cwd=bare) else None
        if train != head:
            raise Refusal("head_moved", repo=repo.name, branch=branch, head=train,
                          reason="the train branch is not at head")
        code = git.run(["merge-base", "--is-ancestor", expect, head], cwd=bare, check=False)[0]
        if code != 0:
            raise Refusal("not_fast_forward", repo=repo.name, base=base,
                          reason="the head does not hold the base")
        # the riders, from the merge commits train_build made
        riders = _train_riders(bare, expect, head)
    answer = {"repo": repo.name, "base": base, "branch": branch, "head": head}
    client = None
    try:
        client = forge.client(repo)
        # one pull request per train: a retry finds the one it opened
        pr, _ = _train_pull_request(client, branch, base, riders)
        answer["number"] = pr.get("number")
        # a merge commit, never squash or rebase: every rider stays reachable and merged
        landed = client.land_pull_request(pr.get("number"), head)
    except forge.ForgeError as exc:
        said = git.scrub(str(exc))
        if "base branch was modified" in said.lower():
            with bench.lock(repo.name):
                now = git.rev_parse("refs/remotes/origin/" + base, cwd=bench.fetch(repo))
            raise Refusal("base_moved", repo=repo.name, base=base, base_head=now,
                          number=answer.get("number"), reason=said[:400])
        if "required status check" in said.lower() and answer.get("number"):
            return dict(answer, state="waiting", pending=_train_pending(client, head),
                        reason=said[:400])
        raise Refusal("merge_refused", repo=repo.name, base=base,
                      number=answer.get("number"), reason=said[:400])
    if landed.get("state") == "waiting":
        return dict(answer, state="waiting", pending=_train_pending(client, head),
                    reason=landed.get("reason"))
    return dict(answer, landed=True, sha=landed.get("sha"))


def _train_pending(client, head):
    """Names the checks still pending on head, or none when the forge will not say."""
    try:
        states = client.check_states(head)
    except forge.ForgeError:
        return []
    return sorted(name for name, state in states.items() if state == "pending")


def train_delete(bench, args):
    """Deletes one train/* branch on the remote; refuses any other branch."""
    repo = bench.repo(args.get("repo"))
    branch = _train_branch(args)
    with bench.lock(repo.name):
        bare = bench.ensure_bare(repo)
        code, text, err = git.run(["push", "origin", ":refs/heads/" + branch],
                                  cwd=bare, check=False, timeout=120)
        git.run(["branch", "-D", branch], cwd=bare, check=False)
    if code != 0:
        raise Refusal("delete_refused", repo=repo.name, branch=branch,
                      reason=(err or text).strip()[:400])
    return {"repo": repo.name, "branch": branch, "deleted": True}


# ---------------------------------------------------------------- schemas

_REPO = {"type": "string", "description": "The repository name in bench.json, as the forge spells it: owner/name, or a plain name."}
_BRANCH = {"type": "string", "description": "The work branch. Use only A-Z a-z 0-9 . _ / -"}
_MAX_BYTES = {
    "type": "integer",
    "description": "The cap on the answer in bytes. The default is 16384. The ceiling is 65536.",
}
_ALLOW = {
    "type": "array",
    "items": {"type": "string"},
    "description": "The globs that the call may touch. A path that no glob matches is refused.",
}
_REPO_NAME = {"type": "string",
              "description": "The repository name, as the forge spells it: owner/name, or a plain name."}
_SEAT = {"type": "string", "description": "The seat that makes the call. The rig writes it in its log."}
_SITTING = {"type": "string",
            "description": "The sitting that makes the call. The rig writes it in its log."}

TOOL_SPECS = [
    {
        "name": "prepare",
        "function": prepare,
        "description": (
            "Makes the clone and the worktree for one branch. Fetches first. "
            "Call prepare one time before the other tools. It is safe to call it again. "
            "It does not move a worktree that exists: behind and behind_remote give the "
            "commits the worktree lacks, and note names the pull that brings it forward. "
            "When bench.json gives the repository a setup step (npm ci), prepare runs it in "
            "the worktree until it succeeds once, and setup answers {ran, ok, exit_code, "
            "output}; a failed setup is reported, and the next prepare tries it again."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "branch": _BRANCH,
                "base": {"type": "string",
                         "description": "The branch to start from. The default is the repository default branch."},
                "seat": _SEAT,
                "sitting": _SITTING,
            },
            "required": ["repo", "branch"],
            "additionalProperties": False,
        },
    },
    {
        "name": "status",
        "function": status,
        "description": (
            "Gives the state of the worktree: the head, the base, the count of changed "
            "paths, the changed paths, and the commits ahead of and behind the base. "
            "During a merge, markers gives each conflicted path with the line ranges "
            "{start, end} of its conflict markers. It does no fetch."
        ),
        "schema": {
            "type": "object",
            "properties": {"repo": _REPO, "branch": _BRANCH, "seat": _SEAT, "sitting": _SITTING},
            "required": ["repo", "branch"],
            "additionalProperties": False,
        },
    },
    {
        "name": "find",
        "function": find,
        "description": (
            "Looks in the worktree. Mode tree gives the files under a path to a depth "
            "with their sizes. Mode glob gives the paths that match a pattern. Mode grep "
            "gives the count of matches for each file first, then the lines. Mode diff "
            "gives the change of the worktree against the base. The symbols tool gives the "
            "definitions. Modes glob and grep require pattern. Mode tree takes a directory: "
            "a file is refused with the way to read it. Every answer has a cap. "
            "With allow, the answer holds only the paths that a glob of the list matches."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "branch": _BRANCH,
                "mode": {"type": "string", "enum": ["tree", "glob", "grep", "diff"],
                         "default": "tree",
                         "description": ("The kind of look. The default is tree. "
                                         "With a pattern and no mode, the mode is grep.")},
                "path": {"type": "string", "description": "The path to look in."},
                "depth": {"type": "integer",
                          "description": "The depth for mode tree. The default is 2."},
                "pattern": {"type": "string",
                            "description": (
                                "The glob for mode glob, or the pattern for mode grep. A grep "
                                "pattern is a Perl regular expression: a|b, (?i), \\b and .? "
                                "all work. A pattern git cannot read is refused, not empty.")},
                "ignore_case": {"type": "boolean",
                                "description": "For mode grep: match without regard to case."},
                "context": {"type": "integer",
                            "description": "The count of lines around each match for mode grep."},
                "max_matches": {"type": "integer",
                                "description": "The cap on the lines for mode grep. The default is 200."},
                "max_bytes": _MAX_BYTES,
                "allow": _ALLOW,
                "seat": _SEAT,
                "sitting": _SITTING,
            },
            "required": ["repo", "branch"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read",
        "function": read,
        "description": (
            "Gives the lines of one file with their numbers. Use offset and limit for a "
            "range. Use ref to read the file at a git ref, for example base. A ref is read "
            "as the last fetch left it; prepare and pull fetch. Give if_hash "
            "with the hash of your last read: if the file did not change, the answer is "
            "unchanged and the hash, and not the bytes. To read one definition by its name, "
            "use read_symbol. A directory is refused with the way to list it. "
            "With allow, a path that no glob of the list matches is refused."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "branch": _BRANCH,
                "path": {"type": "string", "description": "The path in the repository."},
                "offset": {"type": "integer",
                           "description": (
                               "The number of the first line to give. The lines start at 1, "
                               "and the default is 1. There is no start and no end: the "
                               "window is offset and limit.")},
                "limit": {"type": "integer",
                          "description": (
                              "The count of lines to give from offset, and not the number of "
                              "the last line. The default is 120, and a read gives 120 at most.")},
                "ref": {"type": "string",
                        "description": "A git ref to read instead of the worktree. Use base for the base branch."},
                "if_hash": {"type": "string",
                            "description": "The hash from your last read of this file."},
                "max_bytes": _MAX_BYTES,
                "allow": _ALLOW,
                "seat": _SEAT,
                "sitting": _SITTING,
            },
            "required": ["repo", "branch", "path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "symbols",
        "function": list_symbols,
        "description": (
            "Gives the top-level definitions of the Clojure and Python files under a path, "
            "each with its name, kind, path, line and end_line, e.g. "
            "{\"path\": \"lib/core.clj\", \"pattern\": \"\"} or "
            "{\"path\": \"lib\", \"pattern\": \"^(area|top)$\"}. pattern is required: the empty "
            "pattern gives every definition. Read one with read_symbol. "
            "The answer has a cap. With allow, the answer holds only the paths that a glob of "
            "the list matches."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "branch": _BRANCH,
                "path": {"type": "string",
                         "description": "A file or a directory. The default is the whole worktree."},
                "pattern": {"type": "string",
                            "description": "A Python regular expression over the names; \"\" for all."},
                "max_matches": {"type": "integer",
                                "description": "The cap on the definitions. The default is 200."},
                "max_bytes": _MAX_BYTES,
                "allow": _ALLOW,
                "seat": _SEAT,
                "sitting": _SITTING,
            },
            "required": ["repo", "branch", "pattern"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_symbol",
        "function": read_symbol,
        "description": (
            "Gives one definition of a Clojure or Python file by its name, e.g. "
            "{\"path\": \"lib/core.clj\", \"symbol\": \"greet\"} or {\"path\": \"a.py\", "
            "\"symbol\": \"Thing.method\"}. Each match comes with its range and its numbered "
            "lines, up to 20 under max_bytes; a name the file does not define is refused with "
            "the close names. Use ref to read the file at a git ref, for example base. With "
            "allow, a path that no glob of the list matches is refused."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "branch": _BRANCH,
                "path": {"type": "string", "description": "The path in the repository."},
                "symbol": {"type": "string",
                           "description": "The name of a definition to read, e.g. greet, "
                                          "Thing.method or a defmethod's multi."},
                "ref": {"type": "string",
                        "description": "A git ref to read instead of the worktree. Use base for the base branch."},
                "max_bytes": _MAX_BYTES,
                "allow": _ALLOW,
                "seat": _SEAT,
                "sitting": _SITTING,
            },
            "required": ["repo", "branch", "path", "symbol"],
            "additionalProperties": False,
        },
    },
    {
        "name": "history",
        "function": history,
        "description": (
            "Gives the commits of the branch, newest first, each with its sha, date and subject: "
            "git log. path keeps the commits that touched it, pickaxe the commits that added or "
            "removed that text (git log -S), and all reads every branch instead of this one, e.g. "
            "{\"path\": \"src/app.py\", \"all\": true} or {\"pickaxe\": \"MARKER\"}. For a search "
            "of the content use find mode grep. With allow, a path is necessary, and a path that "
            "no glob of the list matches is refused."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "branch": _BRANCH,
                "path": {"type": "string",
                         "description": "A file or a directory, which may be gone from the worktree."},
                "pickaxe": {"type": "string",
                            "description": "A text: the commits that changed how many times it occurs."},
                "all": {"type": "boolean",
                        "description": "Read every branch and remote branch, not only this one."},
                "limit": {"type": "integer",
                          "description": "The count of commits. The default is 20, and 100 at most."},
                "allow": _ALLOW,
                "seat": _SEAT,
                "sitting": _SITTING,
            },
            "required": ["repo", "branch"],
            "additionalProperties": False,
        },
    },
    {
        "name": "check",
        "function": check,
        "description": (
            "Lints the files a change touched, before submit. With no paths it checks every "
            "file the branch changed against its base. Clojure files (.clj .cljs .cljc .edn) "
            "get a balance check of their forms, and clj-kondo's errors when the rig has it; "
            "Python files get a compile check. Other files are listed as skipped. The answer "
            "is ok, the findings with path, line, col and message, skipped and unavailable. "
            "When the repository has a check step, check starts it and waits up to `wait` "
            "seconds: a step that has not ended answers pending: true, ok: null and a check_id. "
            "Call check again with that check_id until it is not pending; the finished answer "
            "carries state (finished, or timed_out past the step's timeout), exit_code and "
            "the step's findings. It never writes."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "branch": _BRANCH,
                "paths": {"type": "array", "items": {"type": "string"},
                          "description": "The paths to check. The default is every file the "
                                         "branch changed."},
                "check_id": {"type": "string",
                             "description": "The check_id a pending answer gave: answers that "
                                            "check step, pending or ended. It lints nothing again."},
                "wait": {"type": "integer",
                         "description": "The seconds to wait for the check step. The default is 5; "
                                        "the ceiling is 20, under the engine's 30 s limit on a call."},
                "seat": _SEAT,
                "sitting": _SITTING,
            },
            "required": ["repo", "branch"],
            "additionalProperties": False,
        },
    },
    {
        "name": "edit",
        "function": edit,
        "description": (
            "Changes one path with exactly one operation. Replace: "
            "old with new, and old must be in the file one time, e.g. {\"path\": \"a.py\", "
            "\"old\": \"x = 1\", \"new\": \"x = 2\"}. Create: new with create: true, e.g. "
            "{\"path\": \"b.py\", \"new\": \"print(1)\\n\", \"create\": true}; content is "
            "taken as a spelling of new for a new file. Delete: delete: true, e.g. {\"path\": \"c.py\", "
            "\"delete\": true}. Move: move_to, e.g. {\"path\": \"c.py\", \"move_to\": "
            "\"d.py\"}. For many edits in one call, use edit_many. A write under .github/ or .claude/ is "
            "refused when the scope does not name the path. With allow, a path that no glob "
            "of the list matches is refused."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "branch": _BRANCH,
                "path": {"type": "string", "description": "The path in the repository."},
                "old": {"type": "string",
                        "description": "The text to replace. It must be in the file one time. "
                                       "An empty old on a path that is not there makes the file."},
                "new": {"type": "string",
                        "description": "The new text with old, or the content of a new file "
                                       "with create: true."},
                "content": {"type": "string",
                            "description": "A spelling of new for a new file: create true or "
                                           "absent, and no old."},
                "create": {"type": "boolean",
                           "description": "True to make a new file. The file's content goes in "
                                          "new, not here: new with create: true."},
                "delete": {"type": "boolean",
                           "description": "True to remove the file: delete: true, with no other "
                                          "operation."},
                "move_to": {"type": "string", "description": "The new path of the file."},
                "allow_protected": {"type": "boolean",
                                    "description": "True to permit a write under .github/ or .claude/."},
                "allow": _ALLOW,
                "seat": _SEAT,
                "sitting": _SITTING,
            },
            "required": ["repo", "branch"],
            "additionalProperties": False,
        },
    },
    {
        "name": "edit_many",
        "function": edit_many,
        "description": (
            "Changes many paths in one call. edits is a list of up to 50 objects, each shaped "
            "as one edit of the edit tool: path with old and new, new with create: true, "
            "delete: true, or move_to. They apply in order and together or not at all, e.g. "
            "{\"edits\": [{\"path\": \"a.py\", \"old\": \"x = 1\", \"new\": \"x = 2\"}, "
            "{\"path\": \"c.py\", \"delete\": true}]}; a refusal names the edit by its number, "
            "from 1, in item. To create a file give path, new and create: true, and no old. "
            "An old that is not in the file is refused with nearest and block: block.text is "
            "the file's lines where old would stand, verbatim, to copy into old. An old found "
            "more than once is refused with found and the lines each copy starts on. "
            "The answer gives each edit's path and hash. A write under .github/ "
            "or .claude/ is refused when the scope does not name the path. With allow, a path "
            "that no glob of the list matches is refused."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "branch": _BRANCH,
                "edits": {
                    "type": "array", "minItems": 1, "maxItems": 50,
                    "description": "The edits, each shaped as one edit: path with old and new, "
                                   "new with create: true, delete: true, or move_to. They apply "
                                   "in order, and one refused edit writes none.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "old": {"type": "string"},
                            "new": {"type": "string"},
                            "content": {"type": "string"},
                            "create": {"type": "boolean"},
                            "delete": {"type": "boolean"},
                            "move_to": {"type": "string"},
                        },
                        "required": ["path"],
                    },
                },
                "allow_protected": {"type": "boolean",
                                    "description": "True to permit a write under .github/ or .claude/."},
                "allow": _ALLOW,
                "seat": _SEAT,
                "sitting": _SITTING,
            },
            "required": ["repo", "branch", "edits"],
            "additionalProperties": False,
        },
    },
    {
        "name": "diff",
        "function": diff,
        "description": (
            "Gives the uncommitted edits of the worktree against HEAD, one entry per path: "
            "its status (changed or untracked), its diff text, its size in bytes, and "
            "truncated when the text was cut at max_bytes or at the answer's max_total. No fetch."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "branch": _BRANCH,
                "paths": {"type": "array", "items": {"type": "string"},
                          "description": "Only these paths: a path, a directory, or a glob."},
                "max_bytes": {"type": "integer",
                              "description": "The cap of one path's text (default 4096)."},
                "max_total": {"type": "integer",
                              "description": "The cap of all the texts together (default 16384)."},
                "seat": _SEAT,
                "sitting": _SITTING,
            },
            "required": ["repo", "branch"],
            "additionalProperties": False,
        },
    },
    {
        "name": "pull",
        "function": pull,
        "description": (
            "Fetches, then brings the worktree forward. From head moves the branch to the "
            "remote branch. From base merges the base branch in. On a conflict the markers "
            "stay in the files and the answer gives the paths. A dirty worktree's edits are "
            "put aside and put back after; reapplied gives each path restored, conflicted "
            "(the markers stay in the file) or held (kept aside until a later pull). "
            "A conflicted pull gives markers: each conflicted path with the line ranges "
            "{start, end} of its conflict markers."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "branch": _BRANCH,
                "from": {"type": "string", "enum": ["base", "head"],
                         "description": "base merges the base branch in. head moves to the remote branch."},
                "seat": _SEAT,
                "sitting": _SITTING,
            },
            "required": ["repo", "branch", "from"],
            "additionalProperties": False,
        },
    },
    {
        "name": "conflicts",
        "function": conflicts,
        "description": (
            "Fetches, trial-merges the base branch into the worktree, gives the unmerged "
            "paths ([] when it merges clean), and aborts the merge: the worktree is left "
            "as it was. A dirty worktree is refused (dirty). The engine calls this tool; "
            "put it in no powers entry."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "branch": _BRANCH,
                "base": {"type": "string",
                         "description": "The branch to merge in. Default the repo's default branch."},
                "seat": _SEAT,
                "sitting": _SITTING,
            },
            "required": ["repo", "branch"],
            "additionalProperties": False,
        },
    },
    {
        "name": "submit",
        "function": submit,
        "description": (
            "Commits every change of the worktree with the message and the trailers. Then, "
            "for a repository without a land block, it pushes the branch. For a repository "
            "with a land block, it lands: it rebases onto the target, runs the configured "
            "steps (setup, format, test...), pushes, and opens the pull request. The landing "
            "runs in the background, and submit answers at once with the landing running: "
            "follow it with status or feedback until landing.running is false. Give wait to "
            "have submit wait up to that many seconds instead. landing.steps gives the output "
            "of each step, and a failed step is a refusal landing_failed with the output: fix "
            "the worktree and submit again. A clean worktree is refused, unless a "
            "landing is still owed. The default branch is refused. While a landing runs, edit, "
            "pull and discard are refused; use status or feedback to follow it. Without "
            "trailers, the rig writes the seat and the sitting as the trailers Waymark-Seat "
            "and Waymark-Sitting."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "branch": _BRANCH,
                "message": {"type": "string", "description": "The commit message."},
                "trailers": {"type": "array", "items": {"type": "string"},
                             "description": "The git trailers, each one as 'Key: value'."},
                "max_lines": {"type": "integer",
                              "description": "The ceiling on the added lines plus the removed lines, "
                                             "counted against the merge base with the target: "
                                             "a merge of the base the change carries does not count."},
                "wait": {"type": "integer",
                         "description": "The seconds to wait for the landing. The default is 0: it answers at once, and status gives the landing's state. "
                                        "The ceiling is 28, under the engine's 30 s limit on a call: a larger wait is cut to it. "
                                        "Zero gives the answer at once."},
                "pull_request": {"type": "boolean",
                                 "description": "False to land without opening the pull request. "
                                                "The default is true."},
                "title": {"type": "string",
                          "description": "The title of the pull request. The default is the first "
                                         "line of the message."},
                "description": {"type": "string", "description": "The body of the pull request."},
                "for": {"type": "string",
                        "description": "The id of the row this landing answers, for example the "
                                       "routing verdict you walked. The landing record keeps it "
                                       "with the seat, so the seat woken by its outcome can find "
                                       "the work."},
                "max_bytes": _MAX_BYTES,
                "seat": _SEAT,
                "sitting": _SITTING,
            },
            "required": ["repo", "branch", "message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "feedback",
        "function": feedback,
        "description": (
            "Gathers what a change caused, after submit: the landing with its steps, the pull "
            "request and its state, the newest pipelines with the log of each failed step, the "
            "commit statuses (a quality gate is one), and the review comments. Every item also "
            "comes as one finding in findings, with a source (landing, pipeline, status, review, "
            "pull_request), a severity, a message, and the path:line locations it names. Read "
            "findings, fix the worktree, submit again. A run whose jobs were cancelled, timed "
            "out or stopped in setup, with no red test step, is severity interrupted, not "
            "error, and its message starts ci: interrupted: call rerun for it. Sources the "
            "rig cannot reach are named in unavailable."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "branch": _BRANCH,
                "max_bytes": _MAX_BYTES,
                "log_bytes": {"type": "integer",
                              "description": "The tail of each failed pipeline log to give. "
                                             "The default is 4096. The ceiling is 32768."},
                "seat": _SEAT,
                "sitting": _SITTING,
            },
            "required": ["repo", "branch"],
            "additionalProperties": False,
        },
    },
    {
        "name": "log",
        "function": log,
        "description": (
            "Reads the log of one job of the newest run on a branch's head, a small answer at a "
            "time, for when feedback's excerpt does not show why a check failed. Without job it "
            "lists the run's jobs with their result and, for a failed job, its line count. With "
            "job (the name feedback reports): mode markers (the default) gives the lines of the "
            "test report (FAIL in, ERROR in, the exception, the summary) with context around "
            "them; mode grep gives the lines a regex pattern finds, with context, the found ones "
            "marked hit; mode range gives limit lines from offset, counting from 1. Every line "
            "comes without colors and without GitHub's timestamp, cut at width characters; a "
            "cut line ends in '… (+N)'. The rig keeps a log an hour, so paging does not fetch "
            "it again. With run_id the jobs are those of that run of the branch, not of the "
            "newest run of each workflow; an id the branch's newest runs do not hold is "
            "refused run."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "branch": _BRANCH,
                "job": {"type": "string", "description": "The job's name, as feedback reports it."},
                "run_id": {"type": "integer",
                           "description": "The run to read, as feedback and rerun name it. "
                                          "Without it: the newest run of each workflow."},
                "mode": {"type": "string", "enum": list(LOG_MODES),
                         "description": "markers (the default), grep or range."},
                "pattern": {"type": "string", "description": "The regex of mode grep."},
                "offset": {"type": "integer", "description": "The first line of mode range, from 1."},
                "limit": {"type": "integer",
                          "description": "Lines of a range (default 60, ceiling 200), or "
                                         "matches of grep and markers (default 20, ceiling 100)."},
                "context": {"type": "integer",
                            "description": "Lines before and after each match. The default is 2. "
                                           "The ceiling is 10."},
                "width": {"type": "integer",
                          "description": "Characters of a line. The default is 200, from 80 to 400."},
                "max_bytes": _MAX_BYTES,
                "seat": _SEAT,
                "sitting": _SITTING,
            },
            "required": ["repo", "branch"],
            "additionalProperties": False,
        },
    },
    {
        "name": "discard",
        "function": discard,
        "description": (
            "Puts the worktree back to the branch head and removes every change. With "
            "drop_branch true it also removes the worktree and the branch."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "branch": _BRANCH,
                "drop_branch": {"type": "boolean",
                                "description": "True to remove the worktree and the branch."},
                "seat": _SEAT,
                "sitting": _SITTING,
            },
            "required": ["repo", "branch"],
            "additionalProperties": False,
        },
    },
    {
        "name": "merge",
        "function": merge,
        "description": (
            "Merges one pull request when every required check is green on its head. For a "
            "GitHub repository where the forge's auto-merge cannot run (a private repository "
            "on a free plan). The rig reads the pull request, then the check runs AND the "
            "commit statuses on head_sha, and merges only when each name in required_checks "
            "is success; the merge names head_sha, so GitHub refuses it if the head moved. "
            "Call it again until the state is merged, red or closed. The answers: "
            "merged - the pull request is merged, now or before, e.g. "
            "{\"state\": \"merged\", \"sha\": \"9f1c...\"}; "
            "closed - it was closed without a merge, e.g. {\"state\": \"closed\"}; "
            "waiting - a required check is missing or still running (or GitHub has not yet "
            "computed mergeability), e.g. {\"state\": \"waiting\", \"pending\": [\"tests\"]}; "
            "red - a required check failed, e.g. {\"state\": \"red\", \"failed\": [\"tests\"]}; "
            "a refusal - nothing was merged, e.g. {\"refused\": \"head_moved\", \"reason\": "
            "\"the head is b2..., not a1...: something was pushed since\"}. The refusals are "
            "head_moved, draft, not_mergeable (a conflict), no_required_checks (the list is "
            "empty: the rig never merges a change nothing has tested), merge_refused (GitHub "
            "refused the merge call) and forge (no forge, no credential, or not GitHub). "
            "behind - the checks are green but the branch is behind its base and GitHub "
            "wants it up to date: call update_branch, e.g. {\"state\": \"behind\"}."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "branch": {"type": "string",
                           "description": "The work branch of the pull request. Optional: "
                                          "the rig writes it in its log."},
                "number": {"type": "integer", "minimum": 1,
                           "description": "The pull request number, e.g. 7."},
                "head_sha": {"type": "string",
                             "description": "The head commit the checks were judged on. A "
                                            "pull request whose head is another commit is "
                                            "refused with head_moved."},
                "required_checks": {"type": "array", "items": {"type": "string"},
                                    "minItems": 1,
                                    "description": "The names of the check runs or commit "
                                                   "status contexts that must be success, "
                                                   "e.g. [\"tests\"]. An empty list is "
                                                   "refused."},
                "method": {"type": "string", "enum": list(MERGE_METHODS),
                           "description": "The merge method. The default is merge."},
                "seat": _SEAT,
                "sitting": _SITTING,
            },
            "required": ["repo", "number", "head_sha", "required_checks"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_branch",
        "function": update_branch,
        "description": (
            "Brings one pull request's branch up to date with its base, so branch protection "
            "lets it merge: GitHub merges the base into the branch (never a rebase, never a "
            "force). The call names head_sha, so GitHub refuses it if the head moved. The "
            "answers: updated - GitHub is merging the base in; the head will move and the "
            "checks run again, e.g. {\"state\": \"updated\"}; current - the branch is already "
            "up to date, e.g. {\"state\": \"current\"}; a refusal - nothing changed. The "
            "refusals are head_moved, not_mergeable (a conflict), update_refused (GitHub "
            "refused for another reason), unsupported (not GitHub) and forge (no forge or no "
            "credential)."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "number": {"type": "integer", "minimum": 1,
                           "description": "The pull request number, e.g. 7."},
                "head_sha": {"type": "string",
                             "description": "The head commit the engine saw. A pull request "
                                            "whose head is another commit is refused with "
                                            "head_moved."},
                "seat": _SEAT,
                "sitting": _SITTING,
            },
            "required": ["repo", "number", "head_sha"],
            "additionalProperties": False,
        },
    },
    {
        "name": "rerun",
        "function": rerun,
        "description": (
            "Starts the interrupted CI of the branch's pushed head again. The rig reads the "
            "newest finished run of each workflow on that head: a job that was cancelled, "
            "timed out, or failed with no step past setup (checkout, set up, containers, "
            "network, services) is interrupted, and the rig asks GitHub to re-run the "
            "failed jobs of its run, e.g. {\"run_id\": 11, \"runs\": [...]}. Use it when "
            "feedback gives a finding whose message starts ci: interrupted. One time per "
            "head. The refusals are not_pushed, red (a job failed in a test step: fix it), "
            "nothing_interrupted, already_rerun (this head was re-run once) and forge. With "
            "run_id it re-runs the failed jobs of that one finished run, a red test step "
            "too, for a failure in a path the change does not touch. One time per run: "
            "already_rerun after, because a second failure is real. The run must be on the "
            "branch's pushed head (not_own_head), ended (not_finished) and hold a failed "
            "job (nothing_failed)."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO, "branch": _BRANCH,
                "run_id": {"type": "integer",
                           "description": "The workflow run whose failed jobs to start again. "
                                          "It must be on the branch's pushed head."},
                "seat": _SEAT, "sitting": _SITTING,
            },
            "required": ["repo", "branch"],
            "additionalProperties": False,
        },
    },
    {
        "name": "secret_set",
        "function": secret_set,
        "description": (
            "Writes one GitHub Actions secret of an enrolled repository: the rig seals the "
            "value with the repository's Actions public key and PUTs it, and answers "
            "{repo, name, updated_at}, never the value. The value is an engine secret "
            "reference: the engine hands the rig the real value and keeps only the "
            "reference. The refusals are repo (not enrolled), input, not_github and forge."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "name": {"type": "string", "description": "The secret's name, e.g. TS_OAUTH_SECRET."},
                "value": {"type": "string", "x-secret-ref": True,
                          "description": "The engine secret that holds the value."},
                "why": {"type": "string", "description": "One sentence: why this secret is set. "
                        "Optional: a gate in front of this server may hold the why itself."},
                "seat": _SEAT, "sitting": _SITTING,
            },
            "required": ["repo", "name", "value"],
            "additionalProperties": False,
        },
    },
    {
        "name": "secret_list",
        "function": secret_list,
        "description": (
            "Lists a repository's GitHub Actions secrets as {repo, secrets: [{name, "
            "updated_at}]}: names only, as GitHub never answers a value. The refusals are "
            "repo, not_github and forge."
        ),
        "schema": {
            "type": "object",
            "properties": {"repo": _REPO, "seat": _SEAT, "sitting": _SITTING},
            "required": ["repo"],
            "additionalProperties": False,
        },
    },
    {
        "name": "dispatch",
        "function": dispatch,
        "description": (
            "Starts one workflow_dispatch workflow of an enrolled repository on a ref "
            "(the default branch when none is given) with these inputs, and answers "
            "{repo, workflow, ref, run_id, run_url, dispatched_at}. The rig looks for the "
            "run about ten seconds; run_id is null when it did not show. Read the run "
            "with run_status. The refusals are repo (not enrolled), input, "
            "token_lacks_actions_write (GitHub answered 403) and forge."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "workflow": {"type": "string", "description": "The workflow file under "
                             ".github/workflows, e.g. runner-image.yml, or its id."},
                "ref": {"type": "string", "description": "The branch or tag the workflow "
                        "runs on. The default branch when it is not given."},
                "inputs": {"type": "object", "description": "The workflow's inputs: name to value."},
                "why": {"type": "string", "description": "One sentence: why this workflow is started. "
                        "Optional: a gate in front of this server may hold the why itself."},
                "seat": _SEAT, "sitting": _SITTING,
            },
            "required": ["repo", "workflow"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_status",
        "function": run_status,
        "description": (
            "Reads one workflow run of an enrolled repository: {repo, run_id, run_url, "
            "workflow, ref, status, conclusion, failed_steps: [{job, steps}]}. conclusion "
            "is null while the run is not done. The refusals are repo, input and forge."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "run_id": {"type": "integer", "description": "The run, as dispatch answers it."},
                "seat": _SEAT, "sitting": _SITTING,
            },
            "required": ["repo", "run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "test",
        "function": test,
        "description": (
            "Dispatches one test selection of the branch on the repository's own CI and "
            "answers at once. The rig pushes the worktree's head to the scratch ref "
            "bench-test/<branch> (never the pull request's branch), dispatches the test "
            "workflow bench.json names with select as its input, and answers {run_id, run_url, "
            "conclusion: pending, head, dispatched_at}. When no run shows within about 15 s, "
            "run_id is null: give test_result the branch, head and dispatched_at instead. "
            "Read the result with test_result. While a run of the branch is not completed and "
            "its scratch ref is still there, test refuses test_running with that run's run_id "
            "and run_url and pushes nothing: read it with test_result, then test again. "
            "The refusals are no_test_workflow, test_running, git and forge."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "branch": _BRANCH,
                "select": {"type": "string",
                           "description": "The test selection. It must match the test block's "
                                          "select_pattern when bench.json gives one (a Python "
                                          "module such as tests.test_bench_test); without it, "
                                          "a test namespace, dotted and ending in -test "
                                          "(factory10.merge-line-test), or namespace/test-name. "
                                          "Anything else is refused before dispatch."},
                "order": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                          "description": "Instead of select: namespaces to run in this order "
                                         "in one run. Each must pass the select check; they "
                                         "are joined with spaces and dispatched as the test "
                                         "block's order_input (default order). Giving both "
                                         "select and order is refused."},
                "seat": _SEAT, "sitting": _SITTING,
            },
            "required": ["repo"],
            "additionalProperties": False,
        },
    },
    {
        "name": "test_result",
        "function": test_result,
        "description": (
            "Reads one run that test started, every 5 s for up to wait_seconds (default 25, "
            "at most 28), and answers {conclusion: success or cancelled, run_url, duration_s}, "
            "{conclusion: failure, run_url, duration_s, failures: [{test, job, lines}]} or "
            "{conclusion: pending, run_url, elapsed_s}. It never dispatches: ask again while "
            "it answers pending. It deletes the scratch ref when the run is done. The "
            "refusals are no_test_workflow and forge."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "run_id": {"type": "integer", "description": "The run test answered."},
                "wait_seconds": {"type": "integer", "minimum": 0, "maximum": CEILING_TEST_WAIT,
                                 "description": "Seconds to wait for the run. Default 25."},
                "branch": _BRANCH,
                "head": {"type": "string",
                         "description": "With no run_id: the head test answered."},
                "dispatched_at": {"type": "string",
                                  "description": "With no run_id: the dispatched_at test answered."},
                "seat": _SEAT, "sitting": _SITTING,
            },
            "required": ["repo"],
            "additionalProperties": False,
        },
    },
    {
        "name": "enroll",
        "function": enroll,
        "description": (
            "Puts one repository on the rig. Give the clone URL. The rig writes the entry "
            "in repos.json under the data directory, and it makes the bare clone one time. "
            "An enroll for a name the rig holds replaces the entry and keeps the clone: "
            "the answer gives cloned false. A clone that fails is a refusal clone_failed, "
            "and the rig writes no entry. The engine calls this tool; put it in no powers "
            "entry."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO_NAME,
                "clone_url": {"type": "string",
                              "description": "The URL that git clones: https or ssh."},
                "default_branch": {"type": "string",
                                   "description": "The default branch. The default is main."},
                "deny": {"type": "array", "items": {"type": "string"},
                         "description": "The globs that the rig never serves. The default is "
                                        "*.pem, *.key, .env* and **/secrets/**."},
                "land": {"type": "object",
                         "description": "The landing block, as bench.json spells it: target, "
                                        "rebase, stages, env and pull_request."},
                "test": {"type": "object",
                         "description": "The test block, as bench.json spells it: workflow, "
                                        "the CI workflow the test tool dispatches, and input, "
                                        "the name of its input that takes the selection, and "
                                        "select_pattern (optional), the regex a selection must "
                                        "match, and order_input (optional, default order), the "
                                        "input that takes an ordered namespace list. Without it "
                                        "the test tool refuses no_test_workflow."},
                "check": {"type": "object",
                          "description": "The check step, as bench.json spells it: command, "
                                         "the command the check tool runs in the worktree "
                                         "after its lint, prepare (optional), a command run "
                                         "before it, and timeout (optional), in seconds, "
                                         "the budget both share. Without it the check tool "
                                         "runs lint only."},
                "hosted_workflows": {"type": ["array", "null"], "items": {"type": "string"},
                                     "description": "The workflow paths, relative to the "
                                                    "repository, that may use a GitHub-hosted "
                                                    "runs-on. Without it no workflow may."},
                "seat": _SEAT,
                "sitting": _SITTING,
            },
            "required": ["repo", "clone_url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "repos",
        "function": repos,
        "description": (
            "Gives every repository on the rig, by name. Each one gives its entry, "
            "bare_exists for the clone on the disk, and source: file for a repository from "
            "enroll, config for a repository from bench.json, and credential: the check of "
            "the GitHub token against what the rig needs ({ok, missing, ...}; "
            "docs/credential.md). The engine calls this tool; "
            "put it in no powers entry."
        ),
        "schema": {
            "type": "object",
            "properties": {"seat": _SEAT, "sitting": _SITTING},
            "additionalProperties": False,
        },
    },
    {
        "name": "unenroll",
        "function": unenroll,
        "description": (
            "Takes one repository off the rig. The rig removes the entry from repos.json, "
            "and it keeps the clone and the worktrees on the disk. A repository from "
            "bench.json is a refusal config_repo: remove it from bench.json. The engine "
            "calls this tool; put it in no powers entry."
        ),
        "schema": {
            "type": "object",
            "properties": {"repo": _REPO_NAME, "seat": _SEAT, "sitting": _SITTING},
            "required": ["repo"],
            "additionalProperties": False,
        },
    },
    {
        "name": "train_build",
        "function": train_build,
        "description": (
            "Builds a merge train. The rig resets branch (it must start with train/) at the "
            "base's current head, merges each pull request's head into it in the order of "
            "prs with a merge commit, and force-pushes the train branch (never the base). A "
            "pull request that does not merge cleanly is skipped and its merge aborted. "
            "Answers {branch, base_head, head, merged: [n...], conflicted: [n...]}. The "
            "refusals are not_train, input, forge and git."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO, "branch": _BRANCH,
                "base": {"type": "string", "description": "The base branch. Default the repo's."},
                "prs": {"type": "array", "items": {"type": "integer", "minimum": 1},
                        "minItems": 1, "description": "The pull request numbers, in order."},
                "seat": _SEAT, "sitting": _SITTING,
            },
            "required": ["repo", "branch", "prs"],
            "additionalProperties": False,
        },
    },
    {
        "name": "train_checks",
        "function": train_checks,
        "description": (
            "Dispatches the check workflow on a pushed train branch, as test does but with "
            "no narrowing, and answers {workflow, run_id, head}: workflow is the one it "
            "dispatched, the given one or the test block's. run_id is null when no run "
            "showed within about 15 s: give train_status the branch, head and workflow "
            "instead. The "
            "refusals are not_train, not_pushed, no_test_workflow and forge."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO, "branch": _BRANCH,
                "workflow": {"type": "string", "description": "The workflow file, e.g. "
                             "tests.yml. Default the test block's."},
                "input": {"type": "object", "description": "The workflow inputs, if any."},
                "seat": _SEAT, "sitting": _SITTING,
            },
            "required": ["repo", "branch"],
            "additionalProperties": False,
        },
    },
    {
        "name": "train_open",
        "function": train_open,
        "description": (
            "Finds the train's one open pull request from branch into the base, or opens "
            "it, with the pushed train branch at head, and never merges it: its "
            "pull_request run is the train's check, which train_status reads. Answers "
            "{number, opened, head}; opened is false when the pull request was there. The "
            "refusals are not_train, input, not_pushed, head_moved and forge."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO, "branch": _BRANCH,
                "base": {"type": "string", "description": "The base branch. Default the repo's."},
                "head": {"type": "string", "description": "The head train_build answered."},
                "seat": _SEAT, "sitting": _SITTING,
            },
            "required": ["repo", "branch", "head"],
            "additionalProperties": False,
        },
    },
    {
        "name": "train_status",
        "function": train_status,
        "description": (
            "Reads one train check run, once, and answers {run_id, state: pending, success, "
            "failure or cancelled, head, url}. Give run_id, or with a null run_id the "
            "branch and head (and workflow): the rig then reads the newest run of the "
            "workflow on the branch at head, of any event, so the pull_request run of "
            "train_open counts as a train_checks run does; by branch, skip_run_id passes "
            "over that run (a cancelled run a retry left behind). The answer always names "
            "run_id, null only while no run has the head. The refusals are "
            "not_train and forge."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "run_id": {"type": "integer", "description": "The run train_checks answered."},
                "branch": _BRANCH,
                "head": {"type": "string", "description": "With no run_id: the head."},
                "workflow": {"type": "string", "description": "With no run_id: the workflow."},
                "skip_run_id": {"type": "integer", "description": (
                    "With no run_id: a run to pass over, the stale run a retry left behind. "
                    "Ignored when absent.")},
                "seat": _SEAT, "sitting": _SITTING,
            },
            "required": ["repo"],
            "additionalProperties": False,
        },
    },
    {
        "name": "train_land",
        "function": train_land,
        "description": (
            "Lands a merge train through one pull request: when the base is still at "
            "expect_base_head and the train branch at head, the rig opens (or, on a retry, "
            "reuses) a pull request from the train branch into the base, titled 'Merge train: "
            "#a #b', and merges it at sha head with a merge commit (never squash or rebase), "
            "so branch protection stays on and GitHub shows each rider merged. Answers "
            "{landed: true, number, sha}, or {state: waiting, number, pending} while GitHub "
            "has not computed mergeability or a required check is pending: ask again later. "
            "The refusals are base_moved (the base is not at expect_base_head, or GitHub says "
            "the base was modified, with base_head: build the train again), merge_refused "
            "(any other GitHub refusal, with its words in reason), head_moved, "
            "not_fast_forward, not_train, and input when expect_base_head is not a sha of 40 "
            "hex characters."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO, "branch": _BRANCH,
                "base": {"type": "string", "description": "The base branch. Default the repo's."},
                "expect_base_head": {"type": "string",
                                     "description": "The base_head train_build answered."},
                "head": {"type": "string", "description": "The head train_build answered."},
                "seat": _SEAT, "sitting": _SITTING,
            },
            "required": ["repo", "branch", "expect_base_head", "head"],
            "additionalProperties": False,
        },
    },
    {
        "name": "train_delete",
        "function": train_delete,
        "description": (
            "Deletes one train/* branch on the remote and answers {deleted: true}. Any other "
            "branch is refused not_train. The other refusal is delete_refused."
        ),
        "schema": {
            "type": "object",
            "properties": {"repo": _REPO, "branch": _BRANCH, "seat": _SEAT, "sitting": _SITTING},
            "required": ["repo", "branch"],
            "additionalProperties": False,
        },
    },
]

TOOLS = {spec["name"]: spec for spec in TOOL_SPECS}


def call(bench, name, args):
    """Calls one tool by name. Gives (answer, refused)."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return {"refused": "input", "reason": "the arguments must be an object"}, True
    spec = TOOLS.get(name)
    if spec is None:
        answer = {"refused": "unknown_tool", "tool": name, "known": sorted(TOOLS)}
    else:
        try:
            answer = spec["function"](bench, args)
            log_call(name, args)
            return answer, False
        except Refusal as exc:
            answer = exc.data
        except git.GitError as exc:
            answer = {"refused": "git", "command": " ".join(exc.argv[:3]),
                      "reason": git.scrub(str(exc.stderr))[:600]}
        except Exception as exc:  # A fault is an answer, and never a stack trace.
            answer = {"refused": "error",
                      "reason": git.scrub("%s: %s" % (type(exc).__name__, exc))[:600]}
    # A refusal gives the seat and the sitting of the call back.
    answer.update(marks_of(args))
    log_call(name, args, answer["refused"])
    return answer, True
