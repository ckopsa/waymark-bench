"""The forge: the pull requests, pipelines, statuses and comments of a repository.

Two forges are known: Bitbucket Cloud and GitHub. The forge is read from
the clone URL, or named in the land block of bench.json. The credential
comes from the settings (settings.py): BENCH_BITBUCKET_USER and
BENCH_BITBUCKET_TOKEN for Bitbucket (an app password), BENCH_GITHUB_TOKEN
or BENCH_GIT_TOKEN for GitHub. On macOS, Bitbucket also reads the keychain
item for bitbucket.org when the settings have nothing. The rig never
writes a credential to disk, and it removes the credentials from every
message that it gives.

Every answer of a forge is a plain dictionary in one shape, so that the
feedback tool can turn Bitbucket and GitHub into the same findings.
"""

import base64
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from . import settings


TIMEOUT = 60
BITBUCKET = re.compile(r"bitbucket\.org[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")
GITHUB = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")


class ForgeError(Exception):
    """The forge did not answer, or refused."""


def scrub(text):
    """Removes every secret from a text."""
    return settings.scrub(text)


def detect(clone_url):
    """Gives (provider, owner, name) from a clone URL, or None."""
    match = BITBUCKET.search(clone_url or "")
    if match:
        return "bitbucket", match.group(1), match.group(2)
    match = GITHUB.search(clone_url or "")
    if match:
        return "github", match.group(1), match.group(2)
    return None


# The one HTTP call. The tests replace it.
def http(method, url, headers, body=None):
    """Gives (status, text). Raises ForgeError when nothing answers."""
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers = dict(headers, **{"Content-Type": "application/json"})
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
            return answer.status, answer.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        raise ForgeError("no answer from %s: %s" % (urllib.parse.urlsplit(url).netloc, exc))


def _keychain_bitbucket():
    """Gives (user, password) from the macOS keychain, or None."""
    if sys.platform != "darwin":
        return None
    try:
        shown = subprocess.run(["security", "find-internet-password", "-s", "bitbucket.org", "-g"],
                               capture_output=True, text=True, timeout=10)
        secret = subprocess.run(["security", "find-internet-password", "-s", "bitbucket.org", "-w"],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if shown.returncode != 0 or secret.returncode != 0:
        return None
    match = re.search(r'"acct"<blob>="([^"]*)"', shown.stdout + shown.stderr)
    if not match:
        return None
    return match.group(1), secret.stdout.strip()


class Client:
    """The common part: the request, the pages and the errors."""

    provider = ""

    def __init__(self, owner, name):
        self.owner = owner
        self.name = name

    def headers(self):  # Each forge gives its own.
        return {}

    def request(self, method, url, body=None, accept_text=False):
        status, text = http(method, url, self.headers(), body)
        text = scrub(text)
        if status == 401 or status == 403:
            raise ForgeError("%s refused the credential (%s)" % (self.provider, status))
        if status == 404:
            raise ForgeError("%s has no %s" % (self.provider, urllib.parse.urlsplit(url).path))
        if status >= 400:
            raise ForgeError("%s answered %s: %s" % (self.provider, status, text[:300]))
        if accept_text:
            return text
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except ValueError:
            raise ForgeError("%s did not answer JSON" % self.provider)


# ---------------------------------------------------------------- bitbucket


class Bitbucket(Client):
    provider = "bitbucket"
    api = "https://api.bitbucket.org/2.0/repositories"

    def credential(self):
        current = settings.load()
        user = current.bitbucket_user
        token = current.secret("bitbucket_token")
        if user and token:
            return user, token
        found = _keychain_bitbucket()
        if found:
            return found
        raise ForgeError("no Bitbucket credential: set BENCH_BITBUCKET_USER and "
                         "BENCH_BITBUCKET_TOKEN (an app password)")

    def headers(self):
        user, token = self.credential()
        raw = base64.b64encode(("%s:%s" % (user, token)).encode("utf-8")).decode("ascii")
        return {"Authorization": "Basic " + raw, "Accept": "application/json",
                "User-Agent": "waymark-bench"}

    def url(self, path, **query):
        base = "%s/%s/%s%s" % (self.api, self.owner, self.name, path)
        if query:
            base += "?" + urllib.parse.urlencode(query)
        return base

    def values(self, path, limit=100, **query):
        """Gives the values of one page. The forge pages at 50 or 100."""
        query.setdefault("pagelen", min(limit, 100))
        data = self.request("GET", self.url(path, **query))
        return data.get("values") or []

    def find_pull_request(self, branch, target):
        query = 'source.branch.name="%s" AND destination.branch.name="%s" AND state="OPEN"' % (
            branch, target)
        values = self.values("/pullrequests", limit=1, q=query)
        if not values:
            return None
        return self._pr(values[0])

    def create_pull_request(self, branch, target, title, description, close_source=True):
        data = self.request("POST", self.url("/pullrequests"), {
            "title": title,
            "description": description,
            "source": {"branch": {"name": branch}},
            "destination": {"branch": {"name": target}},
            "close_source_branch": bool(close_source),
        })
        return self._pr(data)

    def pull_request(self, number):
        return self._pr(self.request("GET", self.url("/pullrequests/%s" % number)))

    def enable_auto_merge(self, pr):
        raise ForgeError("bitbucket does not enable auto-merge on a pull request")

    def _pr(self, data):
        approvals = [p.get("user", {}).get("display_name") for p in data.get("participants") or []
                     if p.get("approved")]
        changes = [p.get("user", {}).get("display_name") for p in data.get("participants") or []
                   if p.get("state") == "changes_requested"]
        return {
            "provider": self.provider,
            "number": data.get("id"),
            "url": (data.get("links") or {}).get("html", {}).get("href"),
            "state": (data.get("state") or "").lower(),
            "title": data.get("title"),
            "source": ((data.get("source") or {}).get("branch") or {}).get("name"),
            "target": ((data.get("destination") or {}).get("branch") or {}).get("name"),
            "head": ((data.get("source") or {}).get("commit") or {}).get("hash"),
            "node_id": None,
            "approvals": [name for name in approvals if name],
            "changes_requested": [name for name in changes if name],
            "open_tasks": data.get("task_count"),
        }

    def comments(self, number, limit=100):
        found = []
        for item in self.values("/pullrequests/%s/comments" % number, limit=limit):
            if item.get("deleted"):
                continue
            inline = item.get("inline") or {}
            found.append({
                "id": item.get("id"),
                "author": (item.get("user") or {}).get("display_name"),
                "created": item.get("created_on"),
                "path": inline.get("path"),
                "line": inline.get("to") or inline.get("from"),
                "text": (item.get("content") or {}).get("raw") or "",
                "reply_to": (item.get("parent") or {}).get("id"),
                "url": (item.get("links") or {}).get("html", {}).get("href"),
            })
        return found

    def pipelines(self, branch, limit=30):
        """Gives the pipelines of a branch, the newest first."""
        found = []
        for item in self.values("/pipelines/", limit=limit, sort="-created_on"):
            target = item.get("target") or {}
            names = {target.get("ref_name"), target.get("source"),
                     ((target.get("source") or {}) if isinstance(target.get("source"), dict)
                      else {}).get("name")}
            if branch not in names:
                continue
            state = item.get("state") or {}
            found.append({
                "id": item.get("uuid"),
                "number": item.get("build_number"),
                "state": (state.get("name") or "").lower(),
                "result": ((state.get("result") or state.get("stage") or {}).get("name") or "").lower(),
                "created": item.get("created_on"),
                "completed": item.get("completed_on"),
                "commit": (target.get("commit") or {}).get("hash"),
                "url": "https://bitbucket.org/%s/%s/pipelines/results/%s" % (
                    self.owner, self.name, item.get("build_number")),
                "kind": target.get("type"),
            })
        return found

    def steps(self, pipeline_id):
        found = []
        for item in self.values("/pipelines/%s/steps/" % pipeline_id, limit=50):
            state = item.get("state") or {}
            found.append({
                "id": item.get("uuid"),
                "name": item.get("name") or "step",
                "state": (state.get("name") or "").lower(),
                "result": ((state.get("result") or {}).get("name") or "").lower(),
                "seconds": item.get("duration_in_seconds"),
            })
        return found

    def step_log(self, pipeline_id, step_id):
        try:
            return self.request("GET", self.url("/pipelines/%s/steps/%s/log" % (pipeline_id, step_id)),
                                accept_text=True)
        except ForgeError as exc:
            return "(no log: %s)" % exc

    def statuses(self, sha):
        found = []
        for item in self.values("/commit/%s/statuses" % sha, limit=50):
            found.append({
                "name": item.get("name") or item.get("key"),
                "key": item.get("key"),
                "state": (item.get("state") or "").lower(),
                "description": item.get("description") or "",
                "url": item.get("url"),
                "updated": item.get("updated_on"),
            })
        return found


# ------------------------------------------------------------------ github


class GitHub(Client):
    provider = "github"
    api = "https://api.github.com/repos"
    graphql_api = "https://api.github.com/graphql"

    def credential(self):
        current = settings.load()
        token = current.secret("github_token") or current.secret("git_token")
        if not token:
            raise ForgeError("no GitHub credential: set BENCH_GITHUB_TOKEN or BENCH_GIT_TOKEN")
        return token

    def headers(self):
        return {"Authorization": "Bearer " + self.credential(),
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "waymark-bench"}

    def url(self, path, **query):
        base = "%s/%s/%s%s" % (self.api, self.owner, self.name, path)
        if query:
            base += "?" + urllib.parse.urlencode(query)
        return base

    def find_pull_request(self, branch, target):
        values = self.request("GET", self.url("/pulls", head="%s:%s" % (self.owner, branch),
                                              base=target, state="open", per_page=1))
        if not values:
            return None
        return self._pr(values[0])

    def create_pull_request(self, branch, target, title, description, close_source=True):
        data = self.request("POST", self.url("/pulls"), {
            "title": title, "body": description, "head": branch, "base": target})
        return self._pr(data)

    def pull_request(self, number):
        return self._pr(self.request("GET", self.url("/pulls/%s" % number)))

    def graphql(self, query, variables):
        """Asks the GraphQL API. GitHub answers 200 with `errors`, so the
        errors are a refusal here too."""
        data = self.request("POST", self.graphql_api,
                            {"query": query, "variables": variables})
        errors = data.get("errors") or []
        if errors:
            raise ForgeError("github refused: %s" % "; ".join(
                str(item.get("message") or item) for item in errors))
        return data.get("data") or {}

    def enable_auto_merge(self, pr):
        """Turns auto-merge on for one pull request: the forge merges it when
        the checks are green. The merge method is the repository's default. A
        repository that does not allow auto-merge refuses."""
        node = pr.get("node_id")
        if not node:
            node = self.pull_request(pr.get("number")).get("node_id")
        if not node:
            raise ForgeError("github gave no node id for pull request %s" % pr.get("number"))
        self.graphql("mutation($id:ID!){enablePullRequestAutoMerge(input:{pullRequestId:$id})"
                     "{pullRequest{number}}}", {"id": node})
        return True

    def _pr(self, data):
        state = data.get("state") or ""
        if data.get("merged") or data.get("merged_at"):
            state = "merged"
        return {
            "provider": self.provider,
            "number": data.get("number"),
            "url": data.get("html_url"),
            "state": state.lower(),
            "title": data.get("title"),
            "source": (data.get("head") or {}).get("ref"),
            "target": (data.get("base") or {}).get("ref"),
            "head": (data.get("head") or {}).get("sha"),
            "node_id": data.get("node_id"),
            "approvals": [],
            "changes_requested": [],
            "open_tasks": None,
        }

    def comments(self, number, limit=100):
        found = []
        for item in self.request("GET", self.url("/issues/%s/comments" % number, per_page=limit)):
            found.append({
                "id": item.get("id"), "author": (item.get("user") or {}).get("login"),
                "created": item.get("created_at"), "path": None, "line": None,
                "text": item.get("body") or "", "reply_to": None, "url": item.get("html_url"),
            })
        for item in self.request("GET", self.url("/pulls/%s/comments" % number, per_page=limit)):
            found.append({
                "id": item.get("id"), "author": (item.get("user") or {}).get("login"),
                "created": item.get("created_at"), "path": item.get("path"),
                "line": item.get("line") or item.get("original_line"),
                "text": item.get("body") or "", "reply_to": item.get("in_reply_to_id"),
                "url": item.get("html_url"),
            })
        return found

    def pipelines(self, branch, limit=10):
        data = self.request("GET", self.url("/actions/runs", branch=branch, per_page=limit))
        found = []
        for item in data.get("workflow_runs") or []:
            found.append({
                "id": item.get("id"), "number": item.get("run_number"),
                "state": (item.get("status") or "").lower(),
                "result": (item.get("conclusion") or "").lower(),
                "created": item.get("created_at"), "completed": item.get("updated_at"),
                "commit": item.get("head_sha"), "url": item.get("html_url"),
                "kind": item.get("name"),
            })
        return found

    def steps(self, pipeline_id):
        data = self.request("GET", self.url("/actions/runs/%s/jobs" % pipeline_id, per_page=50))
        found = []
        for job in data.get("jobs") or []:
            failed = [s.get("name") for s in job.get("steps") or [] if s.get("conclusion") == "failure"]
            found.append({
                "id": job.get("id"), "name": job.get("name") or "job",
                "state": (job.get("status") or "").lower(),
                "result": (job.get("conclusion") or "").lower(),
                "seconds": None, "failed_steps": failed,
            })
        return found

    def step_log(self, pipeline_id, step_id):
        # The log of a job comes as a redirect to a zip; the tail is what matters.
        try:
            return self.request("GET", self.url("/actions/jobs/%s/logs" % step_id), accept_text=True)
        except ForgeError as exc:
            return "(no log: %s)" % exc

    def statuses(self, sha):
        found = []
        data = self.request("GET", self.url("/commits/%s/check-runs" % sha, per_page=50))
        for item in data.get("check_runs") or []:
            state = item.get("conclusion") or item.get("status") or ""
            found.append({
                "name": item.get("name"), "key": item.get("name"),
                "state": {"success": "successful", "failure": "failed",
                          "in_progress": "inprogress", "queued": "inprogress"}.get(state, state),
                "description": ((item.get("output") or {}).get("title") or ""),
                "url": item.get("html_url"), "updated": item.get("completed_at"),
            })
        return found


# ------------------------------------------------------------------ factory


def client(repo):
    """Gives the forge client of a repository, or raises ForgeError."""
    spec = repo.land.pull_request if repo.land else None
    provider = owner = name = None
    if spec:
        provider = spec.get("provider")
        owner = spec.get("owner") or spec.get("workspace")
        name = spec.get("repo") or spec.get("name")
    if not (provider and owner and name):
        found = detect(repo.clone_url)
        if not found:
            raise ForgeError("no forge known for %s: name provider, owner and repo in the "
                             "pull_request block" % repo.clone_url)
        provider = provider or found[0]
        owner = owner or found[1]
        name = name or found[2]
    if provider == "bitbucket":
        return Bitbucket(owner, name)
    if provider == "github":
        return GitHub(owner, name)
    raise ForgeError("unknown forge: %s" % provider)
