"""The landing: what submit does after the commit, per repository.

A repository with a `land` block in bench.json does not just push. It
rebases the branch onto the target, runs the configured steps in order
(setup, format, test, or whatever the repository names), pushes, and
opens the pull request. Every step gives the same shape: a name, a state,
the seconds it took, the exit code, and the tail of its output. That
shape is what an agent reads to fix what it broke.

A landing runs in its own thread, because a test step can take minutes.
The submit tool waits a while for it, and status and feedback give its
state afterwards. The state is written to disk after every step, so it
survives a restart of the rig.
"""

import datetime
import json
import os
import re
import subprocess
import threading
import time

from . import forge, git


OUTPUT_KEEP = 65536
PASSED_TAIL = 1024
DEFAULT_STEP_TIMEOUT = 1800
CEILING_STEP_TIMEOUT = 7200
LOCATION = re.compile(r"(?m)(?:^|[\s\"'(])((?:[\w./-]+/)?[\w.-]+\.\w{1,6}):(\d+)(?::\d+)?")


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tail(text, size):
    """Gives the last size bytes of a text, cut at a line."""
    if not text or size <= 0:
        return ""
    data = text.encode("utf-8", "replace")
    if len(data) <= size:
        return text
    cut = data[-size:]
    newline = cut.find(b"\n")
    if 0 <= newline < len(cut) - 1:
        cut = cut[newline + 1:]
    return "..." + cut.decode("utf-8", "replace")


def locations(text, limit=20):
    """Gives the path:line references in an output, for a finding."""
    found = []
    seen = set()
    for match in LOCATION.finditer(text or ""):
        path, line = match.group(1), int(match.group(2))
        if path.startswith("/") or path in seen:
            continue
        seen.add(path)
        found.append({"path": path, "line": line})
        if len(found) >= limit:
            break
    return found


class Landing:
    """One landing of one branch. The state is a plain dictionary."""

    def __init__(self, bench, repo, branch, state=None):
        self.bench = bench
        self.repo = repo
        self.branch = branch
        self.lock = threading.Lock()
        self.thread = None
        self.state = state or {
            "repo": repo.name,
            "branch": branch,
            "target": repo.land.target if repo.land else repo.default_branch,
            "state": "pending",
            "started": None,
            "finished": None,
            "commit": None,
            "head": None,
            "rebased": False,
            "pushed": False,
            "pull_request": None,
            "auto_merge": None,
            "failed_step": None,
            "reason": None,
            "attempt": None,
            "seat": None,
            "sitting": None,
            "for": None,
            "steps": [],
        }

    # ---------------------------------------------------------- the state

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def path(self):
        return os.path.join(self.bench.repo_dir(self.repo.name), "landings",
                            *self.branch.split("/")) + ".json"

    def attempt_path(self):
        """Where this attempt is kept for good, or None before it starts.

        The branch's own file is overwritten by the next submit. The
        engine mirrors landings to wake the next seat on an outcome, and
        a record that changes after it finished would read as a second
        outcome - so each attempt also lands in a file of its own, named
        by when it started, that nothing writes once the attempt ends.
        """
        attempt = self.state.get("attempt")
        if not attempt:
            return None
        return os.path.join(self.bench.repo_dir(self.repo.name), "attempts",
                            *self.branch.split("/")) + os.sep + attempt + ".json"

    def save(self):
        with self.lock:
            data = json.dumps(self.state, indent=1, sort_keys=True)
        for path in (self.path(), self.attempt_path()):
            if path is None:
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(data)
            os.replace(tmp, path)

    def step(self, name):
        for item in self.state["steps"]:
            if item["name"] == name:
                return item
        item = {"name": name, "state": "pending", "seconds": None, "exit_code": None,
                "output": "", "commit": None}
        self.state["steps"].append(item)
        return item

    def begin(self, name):
        with self.lock:
            item = self.step(name)
            item["state"] = "running"
            item["started_at"] = time.time()
        self.save()
        return item

    def finish(self, item, ok, exit_code=None, output="", reason=None, commit=None):
        with self.lock:
            item["state"] = "passed" if ok else "failed"
            item["seconds"] = round(time.time() - item.pop("started_at", time.time()), 1)
            item["exit_code"] = exit_code
            item["output"] = tail(output, OUTPUT_KEEP)
            if commit:
                item["commit"] = commit
            if not ok:
                self.state["state"] = "failed"
                self.state["failed_step"] = item["name"]
                self.state["reason"] = reason or ("%s failed" % item["name"])
                self.state["finished"] = now()
        self.save()
        return ok

    def skip(self, name, reason):
        with self.lock:
            item = self.step(name)
            item["state"] = "skipped"
            item["output"] = reason
        self.save()

    def view(self, max_bytes=16384):
        """Gives the state with the outputs capped. The failed step keeps the most."""
        with self.lock:
            data = json.loads(json.dumps(self.state))
        steps = data["steps"]
        failed = data.get("failed_step")
        budget = max(max_bytes - 2048, 1024)
        for item in steps:
            item.pop("started_at", None)
            if item["name"] != failed:
                item["output"] = tail(item.get("output", ""), PASSED_TAIL)
                budget -= len(item["output"].encode("utf-8", "replace"))
        for item in steps:
            if item["name"] == failed:
                item["output"] = tail(item.get("output", ""), max(budget, 1024))
        data["running"] = self.running
        return data

    def findings(self, max_bytes=8192):
        """Gives the failed step as one finding, with its locations."""
        with self.lock:
            failed = self.state.get("failed_step")
            steps = list(self.state["steps"])
        found = []
        for item in steps:
            if item["name"] != failed:
                continue
            output = item.get("output", "")
            found.append({
                "source": "landing",
                "step": item["name"],
                "severity": "error",
                "message": tail(output, max_bytes),
                "exit_code": item.get("exit_code"),
                "locations": locations(output),
                "ref": self.path(),
            })
        return found

    # ------------------------------------------------------------ running

    def start(self, commit, want_pr, title, description, trailers, marks=None, answers=None):
        # Who landed it and what it answers ride the record, because a
        # landing outlives the session that submitted it: the engine
        # mirrors these files and wakes the next seat on the outcome,
        # and that seat has only this record to find its work from.
        marks = marks or {}
        with self.lock:
            self.state.update({
                # finer than `started`: two submits inside one second are
                # two attempts, and each needs a name of its own
                "attempt": datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"),
                "seat": marks.get("seat"),
                "sitting": marks.get("sitting"),
                "for": answers,
                "state": "running",
                "started": now(),
                "finished": None,
                "commit": commit,
                "head": commit,
                "rebased": False,
                "pushed": False,
                "pull_request": None,
                "auto_merge": None,
                "failed_step": None,
                "reason": None,
                "steps": [],
            })
        self.save()
        self.thread = threading.Thread(
            target=self._run, args=(want_pr, title, description, trailers),
            name="landing-%s-%s" % (self.repo.name, self.branch), daemon=True)
        self.thread.start()

    def wait(self, seconds):
        if self.thread is not None:
            self.thread.join(max(seconds, 0))
        return not self.running

    def _run(self, want_pr, title, description, trailers):
        try:
            self._land(want_pr, title, description, trailers)
        except Exception as exc:  # The thread never dies without a sentence.
            with self.lock:
                self.state["state"] = "failed"
                self.state["reason"] = "%s: %s" % (type(exc).__name__, git.scrub(str(exc)))
                self.state["finished"] = now()
                if self.state["steps"] and self.state["steps"][-1]["state"] == "running":
                    self.state["steps"][-1]["state"] = "failed"
            self.save()

    def _land(self, want_pr, title, description, trailers):
        land = self.repo.land
        worktree = self.bench.wt_dir(self.repo.name, self.branch)
        bare = self.bench.bare_dir(self.repo.name)
        remote = "refs/remotes/origin/" + self.branch
        lease = None

        # The rebase, under the repository lock for the fetch.
        if land.rebase:
            item = self.begin("rebase")
            with self.bench.lock(self.repo.name):
                self.bench.fetch(self.repo)
                if git.ref_exists(remote, cwd=bare):
                    lease = git.rev_parse(remote, cwd=bare)
                onto = "refs/remotes/origin/" + land.target
                if not git.ref_exists(onto, cwd=bare):
                    self.finish(item, False, reason="the target branch %s is not on the remote"
                                % land.target)
                    return
                before = git.rev_parse("HEAD", cwd=worktree)
                # A branch with a merge on it (a seat merged the target in to
                # resolve conflicts) takes the target by a merge: a rebase
                # would drop that merge and replay the conflicts it resolved.
                if git.out(["rev-list", "--merges", onto + "..HEAD"], cwd=worktree).strip():
                    verb, argv = "merge of", ["merge", "--no-edit", onto]
                else:
                    verb, argv = "rebase onto", ["rebase", onto]
                code, text, err = git.run(argv, cwd=worktree, check=False, timeout=600)
                if code != 0:
                    conflicts = [name for name in git.out(
                        ["diff", "--name-only", "--diff-filter=U"], cwd=worktree).splitlines() if name]
                    git.run([argv[0], "--abort"], cwd=worktree, check=False)
                    self.finish(item, False, exit_code=code, output=(err or text),
                                reason="the %s %s has conflicts in: %s"
                                % (verb, land.target, ", ".join(conflicts) or "unknown files"))
                    return
                after = git.rev_parse("HEAD", cwd=worktree)
            with self.lock:
                self.state["rebased"] = after != before
                self.state["head"] = after
            self.finish(item, True, exit_code=0, output=text or err,
                        commit=after if after != before else None)
        else:
            with self.bench.lock(self.repo.name):
                self.bench.fetch(self.repo)
                if git.ref_exists(remote, cwd=bare):
                    lease = git.rev_parse(remote, cwd=bare)

        # The steps, one after the other, without the lock.
        for stage in land.stages:
            item = self.begin(stage.name)
            code, output = self._shell(stage, worktree)
            if code != 0:
                reason = ("%s timed out after %s seconds" % (stage.name, stage.timeout)
                          if code == -1 else "%s failed with exit code %s" % (stage.name, code))
                self.finish(item, False, exit_code=code, output=output, reason=reason)
                return
            commit = None
            if stage.commit:
                dirty = git.out(["status", "--porcelain"], cwd=worktree).strip()
                if dirty:
                    git.run(["add", "-A", "--", "."], cwd=worktree)
                    args = ["commit", "-m", stage.commit]
                    for trailer in trailers or []:
                        args += ["--trailer", trailer]
                    git.run(args, cwd=worktree)
                    commit = git.rev_parse("HEAD", cwd=worktree)
                    with self.lock:
                        self.state["head"] = commit
            self.finish(item, True, exit_code=code, output=output, commit=commit)

        # The push, with a lease when the branch was already on the remote.
        item = self.begin("push")
        with self.bench.lock(self.repo.name):
            args = ["push", "origin", "HEAD:refs/heads/" + self.branch]
            if lease:
                args.insert(1, "--force-with-lease=refs/heads/%s:%s" % (self.branch, lease))
            code, text, err = git.run(args, cwd=worktree, check=False, timeout=600)
        if code != 0:
            self.finish(item, False, exit_code=code, output=(err or text),
                        reason="the push was rejected: %s" % (err or text).strip()[:300])
            return
        with self.lock:
            self.state["pushed"] = True
            self.state["head"] = git.rev_parse("HEAD", cwd=worktree)
        self.finish(item, True, exit_code=0, output=(err or text))

        # The pull request.
        if not want_pr:
            self.skip("pull_request", "not asked for")
        elif land.pull_request is None:
            self.skip("pull_request", "no pull_request in the land block of bench.json")
        else:
            item = self.begin("pull_request")
            try:
                client = forge.client(self.repo)
                existing = client.find_pull_request(self.branch, land.target)
                if existing:
                    result = dict(existing, created=False)
                else:
                    result = dict(client.create_pull_request(
                        self.branch, land.target, title or self.branch, description or "",
                        close_source=land.pull_request.get("close_source_branch", True)),
                        created=True)
            except forge.ForgeError as exc:
                self.finish(item, False, output=str(exc), reason="the pull request failed: %s" % exc)
                return
            with self.lock:
                self.state["pull_request"] = result
            if land.pull_request.get("auto_merge"):
                # The forge merges the pull request when the checks are green.
                # A forge that refuses is a finding in feedback, not a failure:
                # the change is pushed and the pull request is open.
                try:
                    client.enable_auto_merge(result)
                    auto = {"enabled": True, "refused": None}
                except forge.ForgeError as exc:
                    auto = {"enabled": False, "refused": git.scrub(str(exc))}
                with self.lock:
                    self.state["auto_merge"] = auto
            self.finish(item, True, exit_code=0, output=json.dumps(result))

        with self.lock:
            self.state["state"] = "landed"
            self.state["finished"] = now()
        self.save()

    def _shell(self, stage, worktree):
        """Runs one step. Gives (exit_code, output). A timeout gives -1."""
        env = dict(os.environ)
        env.update(self.repo.land.env)
        env["BENCH_REPO"] = self.repo.name
        env["BENCH_BRANCH"] = self.branch
        env["BENCH_TARGET"] = self.repo.land.target
        env["GIT_TERMINAL_PROMPT"] = "0"
        try:
            proc = subprocess.Popen(
                stage.command, shell=True, cwd=worktree, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", start_new_session=True)
        except OSError as exc:
            return 127, str(exc)
        try:
            output, _ = proc.communicate(timeout=stage.timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, 9)
            except OSError:
                proc.kill()
            output, _ = proc.communicate()
            return -1, git.scrub(output or "") + "\n(killed after %s seconds)" % stage.timeout
        return proc.returncode, git.scrub(output or "")


# ------------------------------------------------------------ the registry


class Landings:
    """The landings of the bench, one for each worktree, on disk and in memory."""

    def __init__(self, bench):
        self.bench = bench
        self._items = {}
        self._guard = threading.Lock()

    def get(self, repo, branch, create=False):
        key = (repo.name, branch)
        with self._guard:
            item = self._items.get(key)
            if item is None:
                item = self._load(repo, branch)
                if item is None and create:
                    item = Landing(self.bench, repo, branch)
                if item is not None:
                    self._items[key] = item
            return item

    def forget(self, repo, branch):
        with self._guard:
            item = self._items.pop((repo.name, branch), None)
        path = (item or Landing(self.bench, repo, branch)).path()
        try:
            os.remove(path)
        except OSError:
            pass

    def running(self, repo, branch):
        item = self.get(repo, branch)
        return item is not None and item.running

    def _load(self, repo, branch):
        path = Landing(self.bench, repo, branch).path()
        try:
            with open(path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError):
            return None
        if state.get("state") in ("running", "pending"):
            # The rig restarted under the landing.
            state["state"] = "failed"
            state["reason"] = "the bench restarted while the landing ran"
            state["finished"] = state.get("finished") or now()
            for item in state.get("steps", []):
                if item.get("state") == "running":
                    item["state"] = "failed"
                    item.pop("started_at", None)
                    state["failed_step"] = item["name"]
            item = Landing(self.bench, repo, branch, state=state)
            # written back, so the attempt's own file ends too rather
            # than reading "running" to the engine forever
            item.save()
            return item
        item = Landing(self.bench, repo, branch, state=state)
        return item
