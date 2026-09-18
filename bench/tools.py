"""The eight tools of the bench.

Each tool is a function over a Bench object. Each function validates its
input, applies the caps, and gives a dictionary. A refusal is a Refusal
exception with a name and its data. No tool gives a stack trace.
"""

import fnmatch
import json
import os
import re
import shutil
import threading

from . import forge, git, landing as landing_module


DEFAULT_MAX_BYTES = 16384
CEILING_MAX_BYTES = 65536
DEFAULT_MAX_MATCHES = 200
DEFAULT_LIMIT = 200
CEILING_LIMIT = 2000
DEFAULT_DEPTH = 2
CEILING_DEPTH = 12
DEFAULT_WAIT = 600
CEILING_WAIT = 3600
PROTECTED_PREFIXES = (".github/", ".claude/")
BRANCH_CHARS = re.compile(r"^[A-Za-z0-9._/-]+$")
REPO_CHARS = re.compile(r"^[A-Za-z0-9._-]+$")
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
        self.landings = landing_module.Landings(self)

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
        if os.path.isdir(os.path.join(bare, "objects")):
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

    def resolve(self, repo, worktree, path, for_write=False, allow_protected=False):
        """Gives the absolute path inside the worktree, or refuses."""
        rel = clean_path(path)
        if rel == ".":
            return worktree, "."
        if rel == ".git" or rel.startswith(".git/"):
            raise Refusal("denied", path=rel, reason="the git directory is not served")
        pattern = deny_pattern(rel, repo.deny)
        if pattern:
            raise Refusal("denied", path=rel, pattern=pattern)
        if for_write and is_protected(rel) and not allow_protected:
            raise Refusal("protected", path=rel,
                          reason="a write under .github/ or .claude/ needs the scope to name the path")
        root = os.path.realpath(worktree)
        full = os.path.realpath(os.path.join(root, rel))
        if full != root and not full.startswith(root + os.sep):
            raise Refusal("denied", path=rel, reason="the path is outside the worktree")
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


def base_ref_of(bare, base):
    """Gives the ref that holds the base head."""
    if git.ref_exists("refs/remotes/origin/" + base, cwd=bare):
        return "refs/remotes/origin/" + base
    if git.ref_exists(base, cwd=bare):
        return base
    raise Refusal("no_base", base=base, reason="the base branch is not in the clone")


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
        return {
            "repo": repo.name,
            "branch": branch,
            "base": base,
            "head": head_of(path),
            "base_head": base_head,
            "dirty": len(status_paths(path)),
            "created": created,
            "default_branch": repo.default_branch,
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


def _find_tree(bench, repo, worktree, args, max_bytes):
    start_rel = clean_path(_text(args, "path", default=".") or ".")
    start, rel = bench.resolve(repo, worktree, start_rel)
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
            entries.append({"path": os.path.relpath(full, worktree).replace(os.sep, "/"),
                            "type": "dir"})
        for name in sorted(files):
            full = os.path.join(root, name)
            path_rel = os.path.relpath(full, worktree).replace(os.sep, "/")
            if deny_pattern(path_rel, repo.deny):
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            entries.append({"path": path_rel, "type": "file", "size": size})
    entries.sort(key=lambda item: item["path"])
    kept, dropped = cap_items(entries, max_bytes, lambda item: len(item["path"]) + 24)
    return {"mode": "tree", "path": rel, "depth": depth, "entries": kept, "dropped": dropped}


def _find_glob(bench, repo, worktree, args, max_bytes):
    pattern = _text(args, "pattern", required=True)
    text = git.out(["ls-files", "--cached", "--others", "--exclude-standard"], cwd=worktree)
    paths = []
    for name in text.splitlines():
        if not name:
            continue
        if deny_pattern(name, repo.deny):
            continue
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(os.path.basename(name), pattern):
            paths.append(name)
    kept, dropped = cap_items(sorted(paths), max_bytes, lambda item: len(item) + 4)
    return {"mode": "glob", "pattern": pattern, "paths": kept, "dropped": dropped}


def _find_grep(bench, repo, worktree, args, max_bytes):
    pattern = _text(args, "pattern", required=True)
    context = _int(args, "context", 0, 0, 10)
    max_matches = _int(args, "max_matches", DEFAULT_MAX_MATCHES, 1, 2000)
    scope = []
    if args.get("path"):
        _, rel = bench.resolve(repo, worktree, args.get("path"))
        scope = ["--", rel]
    count_args = ["grep", "-I", "--untracked", "--no-color", "-c", "-e", pattern] + scope
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
        try:
            files.append({"path": name, "count": int(count)})
        except ValueError:
            continue
    line_args = ["grep", "-n", "-I", "--untracked", "--no-color"]
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


def _find_diff(bench, repo, worktree, args, max_bytes):
    base = bench.base_of(repo, check_branch(_text(args, "branch", required=True)))
    base_head = git.rev_parse(base_ref_of(bench.bare_dir(repo.name), base), cwd=worktree)
    # Intent to add: a new file is in the diff, and its content stays out of the index.
    git.run(["add", "-N", "--", "."], cwd=worktree, check=False)
    scope = []
    if args.get("path"):
        _, rel = bench.resolve(repo, worktree, args.get("path"))
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


def find(bench, args):
    """Looks in a worktree: a tree, a glob, a grep or a diff."""
    repo = bench.repo(args.get("repo"))
    branch = check_branch(_text(args, "branch", required=True))
    mode = _text(args, "mode", default="tree")
    max_bytes = _int(args, "max_bytes", DEFAULT_MAX_BYTES, 256, CEILING_MAX_BYTES)
    with bench.lock(repo.name):
        worktree = bench.worktree(repo, branch)
        if mode == "tree":
            answer = _find_tree(bench, repo, worktree, args, max_bytes)
        elif mode == "glob":
            answer = _find_glob(bench, repo, worktree, args, max_bytes)
        elif mode == "grep":
            answer = _find_grep(bench, repo, worktree, args, max_bytes)
        elif mode == "diff":
            answer = _find_diff(bench, repo, worktree, args, max_bytes)
        else:
            raise Refusal("input", field="mode", reason="use tree, glob, grep or diff")
        answer.update({"repo": repo.name, "branch": branch, "max_bytes": max_bytes})
        return answer


def read(bench, args):
    """Gives lines with numbers from the worktree or from a ref."""
    repo = bench.repo(args.get("repo"))
    branch = check_branch(_text(args, "branch", required=True))
    offset = _int(args, "offset", 1, 1, 1000000)
    limit = _int(args, "limit", DEFAULT_LIMIT, 1, CEILING_LIMIT)
    max_bytes = _int(args, "max_bytes", DEFAULT_MAX_BYTES, 256, CEILING_MAX_BYTES)
    ref = _text(args, "ref")
    if_hash = _text(args, "if_hash")
    with bench.lock(repo.name):
        worktree = bench.worktree(repo, branch)
        full, rel = bench.resolve(repo, worktree, args.get("path"))
        if ref:
            if ref == "base":
                ref = bench.base_of(repo, branch)
            spec = "%s:%s" % (ref, rel)
            code, blob, err = git.run(["rev-parse", "--verify", "--quiet", spec], cwd=worktree,
                                      check=False)
            if code != 0:
                raise Refusal("not_found", path=rel, ref=ref)
            file_hash = blob.strip()
            content = git.out(["show", spec], cwd=worktree)
        else:
            if not os.path.isfile(full):
                raise Refusal("not_found", path=rel)
            file_hash = git.line(["hash-object", "--", full], cwd=worktree)
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as handle:
                    content = handle.read()
            except OSError as exc:
                raise Refusal("not_found", path=rel, reason=str(exc))
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


def edit(bench, args):
    """Changes one path: a replace, a create, a delete or a move."""
    repo = bench.repo(args.get("repo"))
    branch = check_branch(_text(args, "branch", required=True))
    allow_protected = bool(args.get("allow_protected"))
    old = args.get("old")
    new = args.get("new")
    create = bool(args.get("create"))
    delete = bool(args.get("delete"))
    move_to = args.get("move_to")
    operations = []
    if old is not None:
        operations.append("replace")
    if create:
        operations.append("create")
    if delete:
        operations.append("delete")
    if move_to:
        operations.append("move")
    if len(operations) != 1:
        raise Refusal("operation", operations=operations,
                      reason="give exactly one of: old with new, create, delete, move_to")
    operation = operations[0]
    with bench.lock(repo.name):
        worktree = bench.worktree(repo, branch)
        bench.no_landing(repo, branch)
        full, rel = bench.resolve(repo, worktree, args.get("path"), for_write=True,
                                  allow_protected=allow_protected)
        if operation == "replace":
            if new is None or not isinstance(new, str) or not isinstance(old, str):
                raise Refusal("input", field="new", reason="old and new must be texts")
            if not os.path.isfile(full):
                raise Refusal("not_found", path=rel)
            with open(full, "r", encoding="utf-8") as handle:
                content = handle.read()
            found = content.count(old)
            if found != 1:
                raise Refusal("found", path=rel, found=found,
                              remedy="give more of the file in old, so it is unique")
            content = content.replace(old, new, 1)
            with open(full, "w", encoding="utf-8") as handle:
                handle.write(content)
        elif operation == "create":
            if new is None or not isinstance(new, str):
                raise Refusal("input", field="new", reason="give the content in new")
            if os.path.exists(full):
                raise Refusal("exists", path=rel, remedy="use old and new to change the file")
            os.makedirs(os.path.dirname(full) or worktree, exist_ok=True)
            with open(full, "w", encoding="utf-8") as handle:
                handle.write(new)
        elif operation == "delete":
            if not os.path.isfile(full):
                raise Refusal("not_found", path=rel)
            os.remove(full)
            return {"repo": repo.name, "branch": branch, "path": rel, "deleted": True}
        else:
            target, target_rel = bench.resolve(repo, worktree, move_to, for_write=True,
                                               allow_protected=allow_protected)
            if not os.path.exists(full):
                raise Refusal("not_found", path=rel)
            if os.path.exists(target):
                raise Refusal("exists", path=target_rel)
            os.makedirs(os.path.dirname(target) or worktree, exist_ok=True)
            os.replace(full, target)
            return {"repo": repo.name, "branch": branch, "path": target_rel, "from": rel,
                    "hash": git.line(["hash-object", "--", target], cwd=worktree)}
        return {"repo": repo.name, "branch": branch, "path": rel,
                "hash": git.line(["hash-object", "--", full], cwd=worktree)}


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
            conflicts = [name for name in
                         git.out(["diff", "--name-only", "--diff-filter=U"],
                                 cwd=worktree).splitlines() if name]
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


def submit(bench, args):
    """Commits every change with the trailers, then pushes, or lands."""
    repo = bench.repo(args.get("repo"))
    branch = check_branch(_text(args, "branch", required=True))
    message = _text(args, "message", required=True)
    max_lines = args.get("max_lines")
    trailers = args.get("trailers") or []
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
        if paths:
            git.run(["add", "-A", "--", "."], cwd=worktree)
            stat = git.out(["diff", "--cached", "--numstat"], cwd=worktree)
            for row in stat.splitlines():
                cells = row.split("\t")
                if len(cells) < 3:
                    continue
                files += 1
                if cells[0].isdigit():
                    added += int(cells[0])
                if cells[1].isdigit():
                    removed += int(cells[1])
            if max_lines is not None:
                ceiling = _int(args, "max_lines", 0, 0, 1000000)
                if added + removed > ceiling:
                    git.run(["reset", "-q"], cwd=worktree, check=False)
                    raise Refusal("over_ceiling", lines=added + removed, max_lines=ceiling,
                                  files=files, remedy="make the change smaller, or raise the ceiling")
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
            }
        item = bench.landings.get(repo, branch, create=True)
        item.start(commit, bool(want_pr), title, description, clean_trailers)
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
        "pushed": view["pushed"],
        "landing": view,
    }
    if view["state"] == "failed":
        raise Refusal("landing_failed", step=view["failed_step"], reason=view["reason"],
                      remedy="read landing.steps, fix the worktree, then submit again", **answer)
    return answer


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

    pipelines = attempt("pipelines", lambda: client.pipelines(branch), [])
    answer["pipelines"] = pipelines[:5]
    if pipelines:
        newest = pipelines[0]
        if newest.get("result") in ("failed", "error", "failure", "stopped", "cancelled"):
            steps = attempt("steps", lambda: client.steps(newest["id"]), [])
            newest["steps"] = steps
            for step in steps:
                if step.get("result") not in ("failed", "error", "failure"):
                    continue
                log = attempt("log", lambda: client.step_log(newest["id"], step["id"]), "")
                log = landing_module.tail(log, log_bytes)
                findings.append({
                    "source": "pipeline", "step": step.get("name"), "severity": "error",
                    "message": log, "locations": landing_module.locations(log),
                    "url": newest.get("url"),
                })
        elif newest.get("state") in ("in_progress", "pending", "queued", "inprogress"):
            findings.append({"source": "pipeline", "severity": "info",
                             "message": "the pipeline is still running", "url": newest.get("url")})

    head = (pr or {}).get("head") or (item.state.get("head") if item else None)
    if head:
        statuses = attempt("statuses", lambda: client.statuses(head), [])
        answer["statuses"] = statuses
        for status in statuses:
            if status.get("state") in ("failed", "failure", "error", "stopped"):
                findings.append({
                    "source": "status", "name": status.get("name"), "severity": "error",
                    "message": status.get("description") or "%s failed" % status.get("name"),
                    "url": status.get("url"),
                })

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


# ---------------------------------------------------------------- schemas

_REPO = {"type": "string", "description": "The repository name in bench.json."}
_BRANCH = {"type": "string", "description": "The work branch. Use only A-Z a-z 0-9 . _ / -"}
_MAX_BYTES = {
    "type": "integer",
    "description": "The cap on the answer in bytes. The default is 16384. The ceiling is 65536.",
}

TOOL_SPECS = [
    {
        "name": "prepare",
        "function": prepare,
        "description": (
            "Makes the clone and the worktree for one branch. Fetches first. "
            "Call prepare one time before the other tools. It is safe to call it again."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "repo": _REPO,
                "branch": _BRANCH,
                "base": {"type": "string",
                         "description": "The branch to start from. The default is the repository default branch."},
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
            "properties": {"repo": _REPO, "branch": _BRANCH},
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
            "gives the change of the worktree against the base. Every answer has a cap."
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
                            "description": "The glob for mode glob, or the pattern for mode grep."},
                "context": {"type": "integer",
                            "description": "The count of lines around each match for mode grep."},
                "max_matches": {"type": "integer",
                                "description": "The cap on the lines for mode grep. The default is 200."},
                "max_bytes": _MAX_BYTES,
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
            "range. Use ref to read the file at a git ref, for example base. Give if_hash "
            "with the hash of your last read: if the file did not change, the answer is "
            "unchanged and the hash, and not the bytes."
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
            },
            "required": ["repo", "branch", "path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "edit",
        "function": edit,
        "description": (
            "Changes one path. Give old and new to replace a text: old must be in the file "
            "one time. Give create true with new for a new file. Give delete true to remove "
            "a file. Give move_to to move a file. A write under .github/ or .claude/ is "
            "refused when the scope does not name the path."
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
                        "description": "The new text, or the content of a new file."},
                "create": {"type": "boolean", "description": "True to make a new file from new."},
                "delete": {"type": "boolean", "description": "True to remove the file."},
                "move_to": {"type": "string", "description": "The new path of the file."},
                "allow_protected": {"type": "boolean",
                                    "description": "True to permit a write under .github/ or .claude/."},
            },
            "required": ["repo", "branch", "path"],
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
            },
            "required": ["repo", "branch", "from"],
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
            "runs in the background; submit waits up to wait seconds and gives landing.steps "
            "with the output of each step. A failed step is a refusal landing_failed with the "
            "output: fix the worktree and submit again. A clean worktree is refused, unless a "
            "landing is still owed. The default branch is refused. While a landing runs, edit, "
            "pull and discard are refused; use status or feedback to follow it."
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
                              "description": "The ceiling on the added lines plus the removed lines."},
                "wait": {"type": "integer",
                         "description": "The seconds to wait for the landing. The default is 600. "
                                        "The ceiling is 3600. Zero gives the answer at once."},
                "pull_request": {"type": "boolean",
                                 "description": "False to land without opening the pull request. "
                                                "The default is true."},
                "title": {"type": "string",
                          "description": "The title of the pull request. The default is the first "
                                         "line of the message."},
                "description": {"type": "string", "description": "The body of the pull request."},
                "max_bytes": _MAX_BYTES,
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
            "findings, fix the worktree, submit again. Sources the rig cannot reach are named in "
            "unavailable."
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
            },
            "required": ["repo", "branch"],
            "additionalProperties": False,
        },
    },
]

TOOLS = {spec["name"]: spec for spec in TOOL_SPECS}


def call(bench, name, args):
    """Calls one tool by name. Gives (answer, refused)."""
    spec = TOOLS.get(name)
    if spec is None:
        return {"refused": "unknown_tool", "tool": name, "known": sorted(TOOLS)}, True
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return {"refused": "input", "reason": "the arguments must be an object"}, True
    try:
        return spec["function"](bench, args), False
    except Refusal as exc:
        return exc.data, True
    except git.GitError as exc:
        return {"refused": "git", "command": " ".join(exc.argv[:3]),
                "reason": git.scrub(str(exc.stderr))[:600]}, True
    except Exception as exc:  # A fault is an answer, and never a stack trace.
        return {"refused": "error", "reason": git.scrub("%s: %s" % (type(exc).__name__, exc))[:600]}, True
