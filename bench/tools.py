"""The twenty-two tools of the bench.

Each tool is a function over a Bench object. Each function validates its
input, applies the caps, and gives a dictionary. A refusal is a Refusal
exception with a name and its data. No tool gives a stack trace.
"""

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

from . import config as config_module, forge, git, landing as landing_module, symbols


DEFAULT_MAX_BYTES = 16384
CEILING_MAX_BYTES = 65536
DEFAULT_MAX_MATCHES = 200
DEFAULT_LIMIT = 200
CEILING_LIMIT = 2000
DEFAULT_DEPTH = 2
CEILING_DEPTH = 12
# submit starts the landing and answers at once unless it is asked to
# wait. A landing runs a repo's whole test suite, and the engine that
# brokers a seat's calls gives up on any call after 30 seconds and marks
# the server dark - so a default that waited cut every seat off the
# rig. The way to follow a landing is status or feedback.
DEFAULT_WAIT = 0
CEILING_WAIT = 3600
# test waits for its run the same bounded way: a short default the engine
# stands, up to fifteen minutes for a caller that can hold the line; past
# the wait it answers running, and test {run_id} asks again.
DEFAULT_TEST_WAIT = 15
CEILING_TEST_WAIT = 900
TEST_POLL_SECONDS = 20
TEST_FIND_TRIES = 5
TEST_FIND_SECONDS = 2
TEST_PREFIX = "bench-test/"
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
        base_head = git.rev_parse(base_ref_of(bare, base), cwd=bare)
        behind, behind_remote = lag_of(bare, path, branch, base_head)
        return {
            "repo": repo.name,
            "branch": branch,
            "base": base,
            "head": head_of(path),
            "base_head": base_head,
            "dirty": len(status_paths(path)),
            "created": created,
            "default_branch": repo.default_branch,
            "behind": behind,
            "behind_remote": behind_remote,
            "note": lag_note(branch, base, behind, behind_remote),
        }


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
    item = bench.landings.get(repo, branch)
    max_bytes = _int(args, "max_bytes", DEFAULT_MAX_BYTES, 1024, CEILING_MAX_BYTES)
    return {
        "repo": repo.name,
        "branch": branch,
        "head": head,
        "base": base,
        "base_head": base_head,
        "dirty": len(paths),
        "paths": paths[:500],
        "ahead": int(ahead),
        "behind": int(behind),
        "landing": item.view(max_bytes) if item else None,
    }


def _find_tree(bench, repo, worktree, args, max_bytes, allow=None):
    start_rel = clean_path(_text(args, "path", default=".") or ".")
    start, rel = bench.resolve(repo, worktree, start_rel, allow=allow)
    depth = _int(args, "depth", DEFAULT_DEPTH, 1, CEILING_DEPTH)
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
    mode = _text(args, "mode", default="tree")
    max_bytes = _int(args, "max_bytes", DEFAULT_MAX_BYTES, 256, CEILING_MAX_BYTES)
    allow = _globs(args, "allow")
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
        return rel, ref, blob.strip(), git.out(["show", spec], cwd=worktree)
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


def read(bench, args):
    """Gives lines with numbers from the worktree or from a ref."""
    if args.get("symbol") is not None:
        raise Refusal("input", field="symbol", reason="read takes no symbol; use read_symbol")
    repo = bench.repo(args.get("repo"))
    branch = check_branch(_text(args, "branch", required=True))
    offset = _int(args, "offset", 1, 1, 1000000)
    limit = _int(args, "limit", DEFAULT_LIMIT, 1, CEILING_LIMIT)
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
        return {
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


def check(bench, args):
    """Lints the files a change touched: Clojure forms, Python compiles. It never writes."""
    repo = bench.repo(args.get("repo"))
    branch = check_branch(_text(args, "branch", required=True))
    given = args.get("paths")
    if given is not None and (not isinstance(given, list)
                              or not all(isinstance(item, str) and item for item in given)):
        raise Refusal("input", field="paths", reason="paths is a list of paths")
    findings, skipped, unavailable, clojure = [], [], [], []
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
            else:
                skipped.append(rel)
        if clojure:
            program = shutil.which("clj-kondo")
            if not program:
                unavailable.append("clj-kondo is not on the rig's PATH: the Clojure files had the "
                                   "balance check only")
            else:
                try:
                    findings.extend(kondo_errors(program, worktree, clojure))
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    unavailable.append("clj-kondo did not answer: %s" % exc)
    return {
        "repo": repo.name,
        "branch": branch,
        "ok": not findings,
        "findings": findings,
        "skipped": skipped,
        "unavailable": unavailable,
    }


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


def _plan_edit(bench, repo, worktree, plan, item, allow_protected, allow):
    """Judges one edit against the plan and adds its steps. Gives its answer."""
    operation, old, new = _operation_of(item)
    full, rel = bench.resolve(repo, worktree, item.get("path"), for_write=True,
                              allow_protected=allow_protected, allow=allow)
    if operation == "replace":
        if new is None or not isinstance(new, str) or not isinstance(old, str):
            raise Refusal("input", field="new", reason="old and new must be texts")
        if not plan.is_file(full):
            raise Refusal("not_found", path=rel)
        content = plan.text(full)
        found = content.count(old)
        if found != 1:
            raise Refusal("found", path=rel, found=found,
                          remedy="give more of the file in old, so it is unique")
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
    for index, item in enumerate(edits, 1):
        if not isinstance(item, dict):
            raise Refusal("input", field="edits", item=index, reason="each edit is an object")
        extra = sorted(set(item) - set(EDIT_FIELDS))
        if extra:
            raise Refusal("input", field="edits", item=index, fields=extra,
                          reason="an edit takes only: " + ", ".join(EDIT_FIELDS))
    return _edit(bench, args, edits)


def _edit(bench, args, edits):
    """Plans the edits and writes them together. edits None is the one edit in args."""
    repo = bench.repo(args.get("repo"))
    branch = check_branch(_text(args, "branch", required=True))
    allow_protected = bool(args.get("allow_protected"))
    allow = _globs(args, "allow")
    items = [args] if edits is None else edits
    # The fields of each edit are judged before the lock, as one edit's were.
    for index, item in enumerate(items, 1):
        try:
            _operation_of(item)
        except Refusal as exc:
            if edits is not None:
                exc.data["item"] = index
            raise
    with bench.lock(repo.name):
        worktree = bench.worktree(repo, branch)
        bench.no_landing(repo, branch)
        plan = _Plan()
        results = []
        for index, item in enumerate(items, 1):
            try:
                results.append(_plan_edit(bench, repo, worktree, plan, item, allow_protected, allow))
            except Refusal as exc:
                if edits is not None:
                    exc.data["item"] = index
                raise
        plan.write()
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


def pull(bench, args):
    """Brings the worktree to the branch head, or merges the base in."""
    repo = bench.repo(args.get("repo"))
    branch = check_branch(_text(args, "branch", required=True))
    source = _text(args, "from", default="base")
    if source not in ("base", "head"):
        raise Refusal("input", field="from", reason="use base or head")
    with bench.lock(repo.name):
        worktree = bench.worktree(repo, branch)
        bench.no_landing(repo, branch)
        bare = bench.fetch(repo)
        if source == "head":
            remote = "refs/remotes/origin/" + branch
            if not git.ref_exists(remote, cwd=bare):
                return {"repo": repo.name, "branch": branch, "head": head_of(worktree),
                        "merged": False, "conflicts": [],
                        "reason": "the branch is not on the remote"}
            code, text, err = git.run(["merge", "--ff-only", remote], cwd=worktree, check=False)
            if code != 0:
                raise Refusal("not_fast_forward", branch=branch,
                              reason=(err or text).strip()[:400],
                              remedy="use pull from base, or discard")
            return {"repo": repo.name, "branch": branch, "head": head_of(worktree),
                    "merged": True, "conflicts": []}
        base = bench.base_of(repo, branch)
        remote = base_ref_of(bare, base)
        code, text, err = git.run(["merge", "--no-edit", remote], cwd=worktree, check=False)
        conflicts = []
        if code != 0:
            conflicts = unmerged_paths(worktree)
            if not conflicts:
                raise Refusal("merge_failed", branch=branch, base=base,
                              reason=(err or text).strip()[:400])
        return {
            "repo": repo.name,
            "branch": branch,
            "base": base,
            "head": head_of(worktree),
            "merged": code == 0,
            "conflicts": conflicts,
            "note": "the markers stay in the files" if conflicts else "",
        }


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
            git.run(["add", "-A", "--", "."], cwd=worktree)
            against = _against_of(bench, repo, worktree, target, fetch=max_lines is not None)
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
                    # A pathspec unstages without git reset's other work: a
                    # path-less reset also drops MERGE_HEAD, and a merge from
                    # pull would then commit with one parent.
                    git.run(["reset", "-q", "--", "."], cwd=worktree, check=False)
                    raise Refusal("over_ceiling", lines=added + removed, max_lines=ceiling,
                                  files=files, against=against, target=target,
                                  remedy="make the change smaller, or raise the ceiling")
            credential = bench.credentials.get(repo.name) or {}
            if workflows and "workflows" in (credential.get("missing") or []):
                # GitHub rejects the push of a workflow file without it.
                git.run(["reset", "-q", "--", "."], cwd=worktree, check=False)
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
                raise Refusal("commit_failed", reason=(err or text).strip()[:400])
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
    r"|\d+ tests?, \d+ assertions?, \d+ errors?, \d+ failures?|Uncaught exception|Exception: ")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
LOG_AFTER_MARK = 8
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
    rest of the room goes to the last lines. A gap is "...".
    """
    text = ANSI_ESCAPE.sub("", text or "")
    if not text or len(text.encode("utf-8", "replace")) <= size:
        return text
    lines = text.splitlines()
    marks = [i for i, line in enumerate(lines) if LOG_MARKERS.search(line)]
    if not marks:
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
    try:
        client = forge.client(repo)
        jobs = []
        for run in pipelines_of_head(client.pipelines(branch), head):
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


def rerun(bench, args):
    """Starts the interrupted CI of the branch's pushed head again, one time per head."""
    repo = bench.repo(args.get("repo"))
    branch = check_branch(_text(args, "branch", required=True))
    remote = "refs/remotes/origin/" + branch
    with bench.lock(repo.name):
        worktree = bench.worktree(repo, branch)
        bench.fetch(repo)
        if not git.ref_exists(remote, cwd=worktree):
            raise Refusal("not_pushed", repo=repo.name, branch=branch,
                          reason="the branch is not on the remote: submit pushes it")
        head = git.rev_parse(remote, cwd=worktree)
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
            try:
                client.rerun_failed_jobs(run["id"])
            except forge.ForgeError as exc:
                if exc.status != 403:
                    raise
                raise Refusal("token_lacks_actions_write", repo=repo.name, branch=branch,
                              head=head, run_id=run["id"],
                              reason="GitHub refused the re-run: the rig's token needs "
                                     "Actions: read and write (docs/credential.md)")
    except forge.ForgeError as exc:
        raise Refusal("forge", reason=git.scrub(str(exc)))
    with bench.lock(repo.name):
        meta = bench.meta_read(repo.name)
        meta.setdefault(branch, {})["rerun_head"] = head
        bench.meta_write(repo.name, meta)
    return {"repo": repo.name, "branch": branch, "head": head,
            "run_id": stopped[0]["id"], "runs": stopped}


def test(bench, args):
    """Runs one test selection of a branch on the repository's own CI and answers the result."""
    repo = bench.repo(args.get("repo"))
    spec = repo.test
    if not spec:
        raise Refusal("no_test_workflow", repo=repo.name,
                      reason="bench.json gives this repository no test block {workflow, input}")
    wait = _int(args, "wait", DEFAULT_TEST_WAIT, 0, CEILING_TEST_WAIT)
    log_bytes = _int(args, "log_bytes", 4096, 256, 32768)
    try:
        client = forge.client(repo)
        if args.get("run_id") is not None:
            run_id = _int(args, "run_id", 0, 1, 2 ** 63)
        else:
            run_id = _dispatch_test(bench, repo, spec, client, args)
        run = _wait_for_run(client, run_id, wait)
        answer = {"repo": repo.name, "run_id": run["id"], "url": run.get("url"),
                  "branch": run.get("branch"), "head": run.get("commit")}
        if run.get("state") != "completed":
            answer["conclusion"] = "running"
            return answer
        answer["conclusion"] = run.get("result") or "unknown"
        answer["failed"] = _failed_jobs(client, run["id"], log_bytes)
    except forge.ForgeError as exc:
        raise Refusal("forge", reason=git.scrub(str(exc)))
    answer["scratch_deleted"] = _drop_scratch(bench, repo, run.get("branch"), run.get("commit"))
    return answer


def _dispatch_test(bench, repo, spec, client, args):
    """Pushes the worktree head to the scratch ref and dispatches the test
    workflow on it. Gives the id of the run that dispatch started."""
    branch = check_branch(_text(args, "branch", required=True))
    select = _text(args, "select", required=True)
    scratch = TEST_PREFIX + branch
    before = {run["id"] for run in client.workflow_runs(spec["workflow"], scratch)}
    with bench.lock(repo.name):
        worktree = bench.worktree(repo, branch)
        head = head_of(worktree)
        # the scratch ref, never the pull request's branch
        git.run(["push", "--force", "origin", "HEAD:refs/heads/" + scratch],
                cwd=worktree, timeout=600)
    client.dispatch_workflow(spec["workflow"], scratch, {spec["input"]: select})
    for attempt in range(TEST_FIND_TRIES):
        runs = [run for run in client.workflow_runs(spec["workflow"], scratch)
                if run["id"] not in before and run.get("commit") == head]
        if runs:
            return runs[0]["id"]
        if attempt + 1 < TEST_FIND_TRIES:
            _sleep(TEST_FIND_SECONDS)
    _drop_scratch(bench, repo, scratch, head)
    raise Refusal("run_not_found", repo=repo.name, branch=branch, head=head,
                  reason="the workflow was dispatched but no run of it showed on the scratch ref")


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
    entry = config_module.RepoConfig(
        name=name, clone_url=clone_url, default_branch=default_branch,
        deny=deny, land=land, source="file", test=test)
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
            "commits the worktree lacks, and note names the pull that brings it forward."
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
            "It does no fetch."
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
            "definitions. Every answer has a cap. "
            "With allow, the answer holds only the paths that a glob of the list matches."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "branch": _BRANCH,
                "mode": {"type": "string", "enum": ["tree", "glob", "grep", "diff"],
                         "description": "The kind of look. The default is tree."},
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
            "required": ["repo", "branch", "mode"],
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
            "use read_symbol. With allow, a path that no glob of the list matches is refused."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "branch": _BRANCH,
                "path": {"type": "string", "description": "The path in the repository."},
                "offset": {"type": "integer",
                           "description": "The first line. The lines start at 1."},
                "limit": {"type": "integer",
                          "description": "The count of lines. The default is 200. The ceiling is 2000."},
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
            "each with its name, kind, path, line and end_line, e.g. {\"path\": \"lib/core.clj\"} "
            "or {\"path\": \"lib\", \"pattern\": \"^(area|top)$\"}. Read one with read_symbol. "
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
                            "description": "A Python regular expression over the names."},
                "max_matches": {"type": "integer",
                                "description": "The cap on the definitions. The default is 200."},
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
        "name": "check",
        "function": check,
        "description": (
            "Lints the files a change touched, before submit. With no paths it checks every "
            "file the branch changed against its base. Clojure files (.clj .cljs .cljc .edn) "
            "get a balance check of their forms, and clj-kondo's errors when the rig has it; "
            "Python files get a compile check. Other files are listed as skipped. The answer "
            "is ok, the findings with path, line, col and message, skipped and unavailable. "
            "It never writes."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "branch": _BRANCH,
                "paths": {"type": "array", "items": {"type": "string"},
                          "description": "The paths to check. The default is every file the "
                                         "branch changed."},
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
                        "description": "The text to replace. It must be in the file one time."},
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
            "from 1, in item. The answer gives each edit's path and hash. A write under .github/ "
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
                        "additionalProperties": False,
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
        "name": "pull",
        "function": pull,
        "description": (
            "Fetches, then brings the worktree forward. From head moves the branch to the "
            "remote branch. From base merges the base branch in. On a conflict the markers "
            "stay in the files and the answer gives the paths."
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
                                        "The ceiling is 3600. Zero gives the answer at once."},
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
            "it again."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "branch": _BRANCH,
                "job": {"type": "string", "description": "The job's name, as feedback reports it."},
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
            "nothing_interrupted, already_rerun (this head was re-run once) and forge."
        ),
        "schema": {
            "type": "object",
            "properties": {"repo": _REPO, "branch": _BRANCH, "seat": _SEAT, "sitting": _SITTING},
            "required": ["repo", "branch"],
            "additionalProperties": False,
        },
    },
    {
        "name": "test",
        "function": test,
        "description": (
            "Runs one test selection of the branch on the repository's own CI. The rig pushes "
            "the worktree's head to the scratch ref bench-test/<branch> (never the pull "
            "request's branch), dispatches the test workflow bench.json names with select as "
            "its input, waits up to wait seconds, and answers {run_id, url, conclusion, "
            "failed: [{job, step, log_tail}]}. It deletes the scratch ref when the run is "
            "done. Past the wait it answers conclusion running: ask again with {run_id}. "
            "The refusals are no_test_workflow, run_not_found, git and forge."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "branch": _BRANCH,
                "select": {"type": "string",
                           "description": "The test selection: a namespace or a test id."},
                "run_id": {"type": "integer",
                           "description": "A run an earlier test answered running: read it again."},
                "wait": {"type": "integer", "minimum": 0, "maximum": CEILING_TEST_WAIT,
                         "description": "Seconds to wait for the run. Default 15."},
                "log_bytes": {"type": "integer", "minimum": 256, "maximum": 32768},
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
                                        "the name of its input that takes the selection. "
                                        "Without it the test tool refuses no_test_workflow."},
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
