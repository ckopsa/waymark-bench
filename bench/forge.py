"""The forge: the pull requests, pipelines, statuses and comments of a repository.

Two forges are known: Bitbucket Cloud and GitHub. The forge is read from
the clone URL, or named in the land block of bench.json. The credential
comes from the settings (settings.py): BENCH_BITBUCKET_USER and
BENCH_BITBUCKET_TOKEN for Bitbucket (an app password), BENCH_GITHUB_TOKEN
or BENCH_GIT_TOKEN for GitHub (docs/credential.md names what that token
needs, and check_credential checks it). On macOS, Bitbucket also reads the keychain
item for bitbucket.org when the settings have nothing. The rig never
writes a credential to disk, and it removes the credentials from every
message that it gives.

Every answer of a forge is a plain dictionary in one shape, so that the
feedback tool can turn Bitbucket and GitHub into the same findings.
"""

import base64
import datetime
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import settings


TIMEOUT = 60
BITBUCKET = re.compile(r"bitbucket\.org[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")
GITHUB = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")
RATE_LIMIT = re.compile(r"rate limit", re.IGNORECASE)
# A throttle that names no time: GitHub asks for a minute at least.
THROTTLE_WAIT = 60


class ForgeError(Exception):
    """The forge did not answer, or refused. `status` is the HTTP status of a
    refusal, or None when nothing answered. `reset` is the instant a spent
    rate limit resets, and None when the refusal is not a throttle."""

    def __init__(self, message, status=None, reset=None):
        Exception.__init__(self, message)
        self.status = status
        self.reset = reset


class NoSecretsToken(ForgeError):
    """BENCH_SECRETS_TOKEN is not set: secret_set and secret_list have no token."""


class Text(str):
    """The text of an answer. `headers` has the answer's headers, the names
    in lower case."""

    headers = {}


def throttle_reset(status, headers, text):
    """Gives the instant a spent rate limit resets, or None when the answer
    is not a throttle. GitHub answers a spent primary or secondary limit
    with 403 or 429 and `x-ratelimit-remaining: 0`, a `retry-after`, or a
    body that names the rate limit."""
    if status not in (403, 429):
        return None
    headers = {str(name).lower(): str(value).strip() for name, value in (headers or {}).items()}
    after = headers.get("retry-after", "")
    spent = headers.get("x-ratelimit-remaining") == "0"
    if not (status == 429 or after or spent or RATE_LIMIT.search(text or "")):
        return None
    if after.isdigit():
        moment = time.time() + int(after)
    elif spent and headers.get("x-ratelimit-reset", "").isdigit():
        moment = int(headers["x-ratelimit-reset"])
    else:
        moment = time.time() + THROTTLE_WAIT
    return datetime.datetime.fromtimestamp(moment, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def scrub(text):
    """Removes every secret from a text."""
    return settings.scrub(text)


def seal(public_key, value):
    """Seals value for a GitHub Actions secret: a libsodium sealed box to the
    repository's base64 public key, given back in base64."""
    from nacl import encoding, public
    key = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder())
    return base64.b64encode(public.SealedBox(key).encrypt(value.encode("utf-8"))).decode("ascii")


def detect(clone_url):
    """Gives (provider, owner, name) from a clone URL, or None."""
    match = BITBUCKET.search(clone_url or "")
    if match:
        return "bitbucket", match.group(1), match.group(2)
    match = GITHUB.search(clone_url or "")
    if match:
        return "github", match.group(1), match.group(2)
    return None


class _KeepCredentialOnHost(urllib.request.HTTPRedirectHandler):
    """Follows a redirect, but sends the credential only to the host it was
    meant for. GitHub answers the log of a job with a 302 to a signed URL
    on a blob store; that store refuses a request that carries both its
    signature and our Authorization, and the token must never leave the
    forge's own host (api.github.com, api.bitbucket.org) in any case."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and (urllib.parse.urlsplit(newurl).netloc.lower()
                                != urllib.parse.urlsplit(req.full_url).netloc.lower()):
            for name in list(new.headers):
                if name.lower() in ("authorization", "cookie"):
                    del new.headers[name]
        return new


# The one HTTP call. The tests replace it.
def http(method, url, headers, body=None):
    """Gives (status, text); the text is a Text, so it carries the answer's
    headers. Raises ForgeError when nothing answers."""
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers = dict(headers, **{"Content-Type": "application/json"})
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    opener = urllib.request.build_opener(_KeepCredentialOnHost)
    try:
        with opener.open(request, timeout=TIMEOUT) as answer:
            return answer.status, _text(answer.read(), answer.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, _text(exc.read(), exc.headers)
    except (urllib.error.URLError, OSError) as exc:
        raise ForgeError("no answer from %s: %s" % (urllib.parse.urlsplit(url).netloc, exc))


def _text(raw, headers):
    text = Text(raw.decode("utf-8", "replace"))
    text.headers = {name.lower(): value for name, value in (headers.items() if headers else ())}
    return text


# The scopes of a classic token ride a header of any answer. The tests replace it.
def token_scopes(url, headers):
    """Gives the scopes that X-OAuth-Scopes names, as a list, or None when
    the answer has no such header: a fine-grained token names none. Raises
    ForgeError when nothing answers."""
    request = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
            found = answer.headers.get("X-OAuth-Scopes")
    except urllib.error.HTTPError as exc:
        found = exc.headers.get("X-OAuth-Scopes") if exc.headers else None
    except (urllib.error.URLError, OSError) as exc:
        raise ForgeError("no answer from %s: %s" % (urllib.parse.urlsplit(url).netloc, exc))
    if found is None:
        return None
    return [scope.strip() for scope in found.split(",") if scope.strip()]


# What the rig does with GitHub needs these repository permissions, by their
# fine-grained names. docs/credential.md names the tool behind each one.
PERMISSIONS = ("metadata", "contents", "pull_requests", "workflows", "actions",
               "checks", "statuses")
# The permissions each classic scope gives.
CLASSIC_SCOPES = {
    "repo": ("metadata", "contents", "pull_requests", "actions", "checks", "statuses"),
    "workflow": ("workflows",),
}
# A fine-grained token is probed by reads; a write alone proves these.
WRITE_ONLY = ("contents", "pull_requests", "workflows", "actions")


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
        return self.judge(status, scrub(text), url, accept_text, getattr(text, "headers", None))

    def judge(self, status, text, url, accept_text=False, headers=None):
        """Gives the answer of a scrubbed response, or raises ForgeError. A
        spent rate limit is a ForgeError with `reset`, not a refused credential."""
        reset = throttle_reset(status, headers, text)
        if reset:
            raise ForgeError("%s's rate limit is spent until %s" % (self.provider, reset),
                             status, reset=reset)
        if status == 401 or status == 403:
            raise ForgeError("%s refused the credential (%s)" % (self.provider, status), status)
        if status == 404:
            raise ForgeError("%s has no %s" % (self.provider, urllib.parse.urlsplit(url).path),
                             status)
        if status >= 400:
            raise ForgeError("%s answered %s: %s" % (self.provider, status, text[:300]), status)
        if accept_text:
            return text
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except ValueError:
            raise ForgeError("%s did not answer JSON" % self.provider)


# ---------------------------------------------------------------- bitbucket


# The merge strategy Bitbucket names for each method of the merge tool.
BITBUCKET_STRATEGIES = {"merge": "merge_commit", "squash": "squash", "rebase": "fast_forward"}
# The most pages of statuses the rig reads on one commit.
BITBUCKET_PAGES = 20


class Bitbucket(Client):
    provider = "bitbucket"
    api = "https://api.bitbucket.org/2.0/repositories"
    # close_source_branch of the pull_request block; the factory sets it.
    close_source = True

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

    def all_values(self, path, **query):
        """Gives the values of every page, following each page's `next`."""
        query.setdefault("pagelen", 100)
        url, found = self.url(path, **query), []
        for _ in range(BITBUCKET_PAGES):
            data = self.request("GET", url)
            found.extend(data.get("values") or [])
            url = data.get("next")
            if not url:
                break
        return found

    def post_status(self, sha, key, state, name, url):
        """Posts one build status on a commit: state is SUCCESSFUL or FAILED."""
        return self.request("POST", self.url("/commit/%s/statuses/build" % sha), {
            "key": key, "state": state, "name": name, "url": url})

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

    def merge_when_green(self, number, head_sha, required_checks, method="merge"):
        """Merges one pull request when every required status is green on its
        head, in the answers of the GitHub path. A required name is the `key`
        of a commit status; Bitbucket's source hash is short, so the head
        matches head_sha by a prefix."""
        if not required_checks:
            return {"refused": "no_required_checks",
                    "reason": "the rig never merges a change nothing has tested"}
        data = self.request("GET", self.url("/pullrequests/%s" % number))
        state = (data.get("state") or "").upper()
        if state == "MERGED":
            return {"state": "merged", "sha": (data.get("merge_commit") or {}).get("hash")}
        if state in ("DECLINED", "SUPERSEDED"):
            return {"state": "closed"}
        head = ((data.get("source") or {}).get("commit") or {}).get("hash") or ""
        if not head or not (head_sha.startswith(head) or head.startswith(head_sha)):
            return {"refused": "head_moved", "head": head,
                    "reason": "the head is %s, not %s: something was pushed since" % (
                        head, head_sha)}
        states = {}
        for item in self.all_values("/commit/%s/statuses" % head_sha):
            states[item.get("key")] = (item.get("state") or "").upper()
        failed = [name for name in required_checks if states.get(name) in ("FAILED", "STOPPED")]
        if failed:
            return {"state": "red", "failed": failed}
        pending = [name for name in required_checks if states.get(name) != "SUCCESSFUL"]
        if pending:
            return {"state": "waiting", "pending": pending}
        url = self.url("/pullrequests/%s/merge" % number)
        status, text = http("POST", url, self.headers(), {
            "merge_strategy": BITBUCKET_STRATEGIES.get(method, "merge_commit"),
            "close_source_branch": bool(self.close_source),
        })
        text = scrub(text)
        try:
            answer = json.loads(text) if text.strip() else {}
        except ValueError:
            answer = {}
        if not isinstance(answer, dict):
            answer = {}
        if 200 <= status < 300:
            return {"state": "merged", "sha": (answer.get("merge_commit") or {}).get("hash")}
        message = ((answer.get("error") or {}).get("message") or answer.get("message")
                   or text)
        return {"refused": "merge_refused",
                "reason": "bitbucket answered %s: %s" % (status, str(message)[:300])}

    def land_pull_request(self, number, sha):
        raise ForgeError("the rig lands a merge train only on github")

    def update_branch(self, number, head_sha):
        return {"refused": "unsupported",
                "reason": "the rig updates a pull request's branch only on github"}

    def rerun_failed_jobs(self, run_id):
        raise ForgeError("the rig re-runs a pipeline only on github")

    def dispatch_workflow(self, workflow, ref, inputs):
        raise ForgeError("the rig dispatches a test workflow only on github")

    def workflow_runs(self, workflow, branch, limit=10, event="workflow_dispatch"):
        raise ForgeError("the rig dispatches a test workflow only on github")

    def pipeline(self, run_id):
        raise ForgeError("the rig dispatches a test workflow only on github")

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


# The steps a runner takes before a job's own work. A job that failed with
# no step past these never ran its tests: the runner died, not the code.
SETUP_STEP = re.compile(r"(?i)^(set ?up\b|run actions/checkout\b|checkout\b|initiali[sz]e containers?\b"
                        r"|start(ing)? .*\b(services?|containers?)\b|.*\bnetwork\b|post |complete job\b"
                        r"|stop containers?\b)")


def job_interrupted(job):
    """Tells if a GitHub job stopped before its own work: cancelled, timed
    out, or failed with no step past setup."""
    conclusion = job.get("conclusion")
    if conclusion in ("cancelled", "timed_out"):
        return True
    if conclusion != "failure":
        return False
    return not any(step.get("conclusion") not in (None, "skipped")
                   and not SETUP_STEP.match(step.get("name") or "")
                   for step in job.get("steps") or [])


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

    def secrets_credential(self):
        """Gives the token of secret_set and secret_list. The main token never
        stands in for it, and no other call uses it."""
        token = settings.load().secret("secrets_token")
        if not token:
            raise NoSecretsToken("no secrets token: set BENCH_SECRETS_TOKEN")
        return token

    def headers(self, token=None):
        return {"Authorization": "Bearer " + (token or self.credential()),
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "waymark-bench"}

    def url(self, path, **query):
        base = "%s/%s/%s%s" % (self.api, self.owner, self.name, path)
        if query:
            base += "?" + urllib.parse.urlencode(query)
        return base

    def check_credential(self, branch):
        """Checks the token against PERMISSIONS on this repository. Gives
        {ok, token, missing, unverified}. A classic token names its scopes,
        so each permission is known. A fine-grained token is probed by reads
        on the branch: a refusal is a missing permission, and what only a
        write could prove is unverified."""
        try:
            scopes = token_scopes(self.url(""), self.headers())
            self.request("GET", self.url(""))
        except ForgeError as exc:
            return {"ok": False, "token": None, "missing": list(PERMISSIONS),
                    "unverified": [], "reason": scrub(str(exc))}
        if scopes is not None:
            held = set()
            for scope in scopes:
                held.update(CLASSIC_SCOPES.get(scope, ()))
            missing = [name for name in PERMISSIONS if name not in held]
            return {"ok": not missing, "token": "classic", "missing": missing,
                    "unverified": []}
        ref = urllib.parse.quote(branch, safe="")
        probes = {"actions": self.url("/actions/runs", per_page=1),
                  "checks": self.url("/commits/%s/check-runs" % ref, per_page=1),
                  "statuses": self.url("/commits/%s/status" % ref)}
        missing = []
        for name, url in probes.items():
            try:
                status, _ = http("GET", url, self.headers())
            except ForgeError as exc:
                return {"ok": False, "token": "fine_grained", "missing": [],
                        "unverified": list(PERMISSIONS), "reason": scrub(str(exc))}
            if status in (401, 403):
                missing.append(name)
        return {"ok": not missing, "token": "fine_grained", "missing": missing,
                "unverified": list(WRITE_ONLY)}

    def check_secrets(self):
        """Checks the two tokens against the Secrets API of this repository,
        each by one read of the secret names. Gives {secrets_token: {set, ok
        [, reason]}, warnings}. The main token should be refused there: one
        that reads the names is the warning main_token_reads_secrets."""
        url = self.url("/actions/secrets", per_page=1)

        def reads(token):
            try:
                status, _ = http("GET", url, self.headers(token))
            except ForgeError as exc:
                return {"ok": None, "reason": scrub(str(exc))}
            if status == 200:
                return {"ok": True}
            return {"ok": False, "reason": "github answered %s" % status}

        current = settings.load()
        found = {"secrets_token": {"set": False, "ok": None}, "warnings": []}
        token = current.secret("secrets_token")
        if token:
            found["secrets_token"] = dict(reads(token), set=True)
        main = current.secret("github_token") or current.secret("git_token")
        if main and reads(main)["ok"]:
            found["warnings"].append("main_token_reads_secrets")
        return found

    def secrets_request(self, method, url, body=None):
        """A request that carries the secrets token, never the main one."""
        status, text = http(method, url, self.headers(self.secrets_credential()), body)
        return self.judge(status, scrub(text), url, False, getattr(text, "headers", None))

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

    def merge_pull_request(self, number, sha, method="merge"):
        """The one merge call: GitHub refuses it when the head is not sha."""
        return self.request("PUT", self.url("/pulls/%s/merge" % number),
                            {"sha": sha, "merge_method": method})

    def land_pull_request(self, number, sha):
        """Merges one pull request at sha with a merge commit, without reading
        its checks: branch protection judges them. Gives {state: merged, sha},
        or {state: waiting, reason} while GitHub has not computed whether it
        merges. GitHub's refusal raises ForgeError."""
        data = self.request("GET", self.url("/pulls/%s" % number))
        if data.get("merged") or data.get("merged_at"):
            return {"state": "merged", "sha": data.get("merge_commit_sha")}
        if data.get("mergeable") is None:
            return {"state": "waiting",
                    "reason": "github has not yet computed whether the pull request merges"}
        return {"state": "merged", "sha": self.merge_pull_request(number, sha).get("sha")}

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
        return [self._run(item) for item in data.get("workflow_runs") or []]

    def _run(self, item):
        return {
            "id": item.get("id"), "number": item.get("run_number"),
            "state": (item.get("status") or "").lower(),
            "result": (item.get("conclusion") or "").lower(),
            "created": item.get("created_at"), "completed": item.get("updated_at"),
            "commit": item.get("head_sha"), "url": item.get("html_url"),
            "kind": item.get("name"), "branch": item.get("head_branch"),
        }

    def pipeline(self, run_id):
        """Gives one workflow run, shaped as pipelines gives it."""
        return self._run(self.request("GET", self.url("/actions/runs/%s" % run_id)))

    def workflow_runs(self, workflow, branch, limit=10, event="workflow_dispatch"):
        """Gives the runs of one workflow on one branch, newest first: the dispatched
        ones, or those of event, or with event None the runs of every event."""
        query = {"branch": branch, "per_page": limit}
        if event:
            query["event"] = event
        data = self.request("GET", self.url("/actions/workflows/%s/runs" % workflow, **query))
        return [self._run(item) for item in data.get("workflow_runs") or []]

    def dispatch_workflow(self, workflow, ref, inputs):
        """Starts one workflow on ref with these inputs. GitHub answers no run id."""
        self.request("POST", self.url("/actions/workflows/%s/dispatches" % workflow),
                     {"ref": ref, "inputs": inputs})
        return True

    def set_secret(self, name, value):
        """Seals value with the repository's Actions public key and PUTs it as
        the secret name. Gives the secret's updated_at. The value leaves sealed."""
        key = self.secrets_request("GET", self.url("/actions/secrets/public-key"))
        self.secrets_request("PUT", self.url("/actions/secrets/%s" % name),
                             {"encrypted_value": seal(key["key"], value), "key_id": key["key_id"]})
        return self.secrets_request("GET", self.url("/actions/secrets/%s" % name)).get("updated_at")

    def secrets(self):
        """Gives the Actions secrets as [{name, updated_at}]; GitHub never answers a value."""
        found, page = [], 1
        while True:
            data = self.secrets_request("GET", self.url("/actions/secrets", per_page=100, page=page))
            items = data.get("secrets") or []
            found.extend({"name": item.get("name"), "updated_at": item.get("updated_at")}
                         for item in items)
            if len(items) < 100:
                return found
            page += 1

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
                "interrupted": job_interrupted(job),
            })
        return found

    def rerun_failed_jobs(self, run_id):
        """Starts the failed and cancelled jobs of one workflow run again."""
        self.request("POST", self.url("/actions/runs/%s/rerun-failed-jobs" % run_id), {})
        return True

    def step_log(self, pipeline_id, step_id):
        # The log of a job comes as a redirect to a zip; the tail is what matters.
        try:
            return self.request("GET", self.url("/actions/jobs/%s/logs" % step_id), accept_text=True)
        except ForgeError as exc:
            return "(no log: %s)" % exc

    def check_runs(self, sha, limit=100):
        """Gives the check runs on one commit. A fine-grained token cannot hold
        the Checks permission, so on a private repository check-runs answers
        401 or 403; then each Actions job on the commit stands in for a check
        run of the same name (the token reads Actions)."""
        try:
            data = self.request("GET", self.url("/commits/%s/check-runs" % sha, per_page=limit))
        except ForgeError as exc:
            if exc.status not in (401, 403):
                raise
            return self.action_jobs(sha)
        return data.get("check_runs") or []

    def action_jobs(self, sha):
        """Gives the jobs of every Actions run on one commit, shaped as check runs."""
        runs = self.request("GET", self.url("/actions/runs", head_sha=sha, per_page=100))
        found = []
        for run in runs.get("workflow_runs") or []:
            data = self.request("GET", self.url("/actions/runs/%s/jobs" % run.get("id"),
                                                per_page=100))
            for job in data.get("jobs") or []:
                found.append({
                    "id": job.get("id"),
                    "name": job.get("name"), "status": job.get("status"),
                    "conclusion": job.get("conclusion"), "html_url": job.get("html_url"),
                    "completed_at": job.get("completed_at"), "output": {},
                })
        return found

    def statuses(self, sha):
        found = []
        for item in self.check_runs(sha, limit=50):
            state = item.get("conclusion") or item.get("status") or ""
            found.append({
                "name": item.get("name"), "key": item.get("name"),
                "state": {"success": "successful", "failure": "failed",
                          "in_progress": "inprogress", "queued": "inprogress"}.get(state, state),
                "description": ((item.get("output") or {}).get("title") or ""),
                "url": item.get("html_url"), "updated": item.get("completed_at"),
            })
        return found

    def check_states(self, sha):
        """Gives {name: success | failure | pending} for the check runs AND the
        commit statuses on one commit. A check run that has not completed is
        pending; one that completed with anything but success is a failure. A
        name with several check runs (a job that ran again, or the jobs of
        every Actions run on the commit) takes its newest run: one that has
        not completed, else the latest completed_at, then the highest id. A
        name that is both a check run and a status takes the worse of the two."""
        rank = {"success": 0, "pending": 1, "failure": 2}
        states = {}

        def put(name, state):
            if name and rank[state] >= rank.get(states.get(name), -1):
                states[name] = state

        def age(item):
            return (item.get("status") != "completed", item.get("completed_at") or "",
                    item.get("id") or 0)

        newest = {}
        for item in self.check_runs(sha):
            name = item.get("name")
            if name not in newest or age(item) >= age(newest[name]):
                newest[name] = item
        for item in newest.values():
            if item.get("status") != "completed":
                put(item.get("name"), "pending")
            else:
                put(item.get("name"),
                    "success" if item.get("conclusion") == "success" else "failure")
        combined = self.request("GET", self.url("/commits/%s/status" % sha, per_page=100))
        for item in combined.get("statuses") or []:
            state = item.get("state")
            put(item.get("context"), {"success": "success", "pending": "pending"}.get(
                state, "failure"))
        return states

    def merge_when_green(self, number, head_sha, required_checks, method="merge"):
        """Merges one pull request when every required check is green on its
        head. Gives one answer: {state: merged|closed|waiting|red, ...} or
        {refused, reason}. The merge names head_sha, so GitHub refuses it when
        the head moved in between."""
        if not required_checks:
            return {"refused": "no_required_checks",
                    "reason": "the rig never merges a change nothing has tested"}
        data = self.request("GET", self.url("/pulls/%s" % number))
        if data.get("merged") or data.get("merged_at"):
            return {"state": "merged", "sha": data.get("merge_commit_sha")}
        if data.get("state") == "closed":
            return {"state": "closed"}
        head = (data.get("head") or {}).get("sha")
        if head != head_sha:
            return {"refused": "head_moved", "head": head,
                    "reason": "the head is %s, not %s: something was pushed since" % (
                        head, head_sha)}
        if data.get("draft"):
            return {"refused": "draft", "reason": "the pull request is a draft"}
        if data.get("mergeable") is False:
            return {"refused": "not_mergeable",
                    "reason": "github says the pull request is not mergeable (a conflict)"}
        states = self.check_states(head_sha)
        failed = [name for name in required_checks if states.get(name) == "failure"]
        if failed:
            return {"state": "red", "failed": failed}
        pending = [name for name in required_checks if states.get(name) != "success"]
        if pending:
            return {"state": "waiting", "pending": pending}
        if data.get("mergeable_state") == "behind":
            # Branch protection wants the branch up to date: update_branch first.
            return {"state": "behind",
                    "reason": "the branch is behind its base: update the branch first"}
        if data.get("mergeable") is None:
            # GitHub has not yet computed whether it merges cleanly.
            return {"state": "waiting", "pending": [],
                    "reason": "github has not yet computed whether the pull request merges"}
        try:
            merged = self.merge_pull_request(number, head_sha, method)
        except ForgeError as exc:
            return {"refused": "merge_refused", "reason": str(exc)}
        return {"state": "merged", "sha": merged.get("sha")}

    def update_branch(self, number, head_sha):
        """Merges the base into a pull request's branch; never a rebase, never
        a force. Gives {state: updated|current} or {refused, reason}. The
        call names head_sha, so GitHub refuses it when the head moved."""
        url = self.url("/pulls/%s/update-branch" % number)
        status, text = http("PUT", url, self.headers(), {"expected_head_sha": head_sha})
        headers = getattr(text, "headers", None)
        text = scrub(text)
        if status != 422:
            self.judge(status, text, url, headers=headers)
            return {"state": "updated"}
        try:
            message = str(json.loads(text).get("message") or "")
        except (ValueError, AttributeError):
            message = text
        lowered = message.lower()
        if "head sha" in lowered or "head ref" in lowered:
            return {"refused": "head_moved", "reason": message or "the head moved"}
        if "no new commits" in lowered:
            return {"state": "current"}
        if "conflict" in lowered:
            return {"refused": "not_mergeable", "reason": message}
        return {"refused": "update_refused", "reason": "github answered 422: %s" % message[:300]}


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
        found = Bitbucket(owner, name)
        found.close_source = bool((spec or {}).get("close_source_branch", True))
        return found
    if provider == "github":
        return GitHub(owner, name)
    raise ForgeError("unknown forge: %s" % provider)
