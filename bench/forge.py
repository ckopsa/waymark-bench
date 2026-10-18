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
import contextlib
import datetime
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import settings


TIMEOUT = 60
# The least a call inside a window is given, so a lookup at the window's end is still made.
WINDOW_FLOOR = 1
BITBUCKET = re.compile(r"bitbucket\.org[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")
GITHUB = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")
RATE_LIMIT = re.compile(r"rate limit", re.IGNORECASE)
# A throttle that names no time: GitHub asks for a minute at least.
THROTTLE_WAIT = 60
# How many times a call that nothing answers is made, and the pause between two.
TRIES = 3
RETRY_SECONDS = 2

# The tests replace it.
_sleep = time.sleep

# The build-state table is the factory kit's (bench/kit/SOURCE names the
# commit): change it there and vendor it again, never here.
KIT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kit")
with open(os.path.join(KIT, "build-states.json"), encoding="utf-8") as _handle:
    BUILD_STATES = json.load(_handle)
# check_states spells green and red as it always did.
_STATE_WORDS = {"green": "success", "red": "failure"}


def build_state(forge, raw):
    """Gives the kit's verdict (green, red, pending or interrupted) of one raw
    build state of a forge, or None for a state the table does not hold."""
    table = BUILD_STATES["forges"][forge]
    for verdict in BUILD_STATES["verdicts"]:
        if raw in table.get(verdict, ()):
            return verdict
    return None


def check_state(forge, check):
    """Gives the kit's verdict of one check as its forge answers it. A GitHub
    check run that has not completed is pending, and a completed one is read
    by its conclusion; a Bitbucket status is read by its state. A state the
    table does not hold is red on GitHub and pending on Bitbucket, as the rig
    read it before the table."""
    if forge == "github":
        if check.get("status") != "completed":
            return "pending"
        return build_state(forge, check.get("conclusion")) or "red"
    return build_state(forge, (check.get("state") or "").upper()) or "pending"


def head_verdict(states, required, advisory=()):
    """Gives the kit's verdict of one head from its check_states: None while
    a required check is pending or has no run, else {verdict: red, names},
    {verdict: interrupted, names} or {verdict: green}. A red check beside an
    interrupted one is red. An advisory check counts only when it is red: a
    pending or missing one does not hold."""
    if any(states.get(name, "pending") == "pending" for name in required):
        return None
    red = [name for name in list(required) + list(advisory) if states.get(name) == "failure"]
    if red:
        return {"verdict": "red", "names": red}
    stopped = [name for name in required if states.get(name) == "interrupted"]
    if stopped:
        return {"verdict": "interrupted", "names": stopped}
    return {"verdict": "green"}


def held_answer(states, required, advisory=()):
    """Gives the merge's answer while the required checks are not green:
    waiting, red or interrupted. None when they are green. A red advisory
    check is red; a pending or missing one changes nothing."""
    verdict = head_verdict(states, required, advisory or ())
    if verdict is None:
        return {"state": "waiting", "waits_on": "checks",
                "pending": [name for name in required
                            if states.get(name, "pending") == "pending"]}
    if verdict["verdict"] == "red":
        return {"state": "red", "failed": verdict["names"]}
    if verdict["verdict"] == "interrupted":
        return {"state": "interrupted", "interrupted": verdict["names"]}
    return None


def behind(behind_by):
    """True when a head lacks this many commits of its base, and it is not 0."""
    return (behind_by or 0) > 0


class ForgeError(Exception):
    """The forge did not answer, or refused. `status` is the HTTP status of a
    refusal, or None when nothing answered. `reset` is the instant a spent
    rate limit resets, and None when the refusal is not a throttle."""

    def __init__(self, message, status=None, reset=None):
        Exception.__init__(self, message)
        self.status = status
        self.reset = reset


class NoAnswer(ForgeError):
    """Nothing answered: the name did not resolve, the connection closed, or
    the call timed out. A read can be made again. `sent` is False when the
    call never left (no name, no connection), so a write can be made again
    too; when it is True the forge may have done the write."""

    def __init__(self, message, sent=True):
        ForgeError.__init__(self, message)
        self.sent = sent


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
    meant for. GitHub answers the log of a job, and Bitbucket the log of a
    step, with a 302 to a signed URL on a blob store; that store refuses a request that carries both its
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


_window = threading.local()


@contextlib.contextmanager
def within(seconds):
    """Bounds every HTTP call made in the block, on this thread, by the time
    left of seconds. A tool that waits inside the engine's limit on a call
    gives its wait here, so a lookup that hangs is NoAnswer inside the wait
    and not after TIMEOUT. A window inside a window keeps the earlier end."""
    before = getattr(_window, "deadline", None)
    deadline = time.monotonic() + seconds
    _window.deadline = deadline if before is None else min(before, deadline)
    try:
        yield
    finally:
        _window.deadline = before


def timeout():
    """Gives the seconds one HTTP call may take now: TIMEOUT, or the time left
    of the window when that is less, and never less than WINDOW_FLOOR."""
    deadline = getattr(_window, "deadline", None)
    if deadline is None:
        return TIMEOUT
    return min(TIMEOUT, max(WINDOW_FLOOR, deadline - time.monotonic()))


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
        with opener.open(request, timeout=timeout()) as answer:
            return answer.status, _text(answer.read(), answer.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, _text(exc.read(), exc.headers)
    except (urllib.error.URLError, OSError) as exc:
        raise _no_answer(url, exc)


def _no_answer(url, exc):
    """Gives the NoAnswer of a network error. A name that did not resolve and
    a connection that was refused sent nothing."""
    reason = getattr(exc, "reason", exc)
    return NoAnswer("no answer from %s: %s" % (urllib.parse.urlsplit(url).netloc, exc),
                    sent=not isinstance(reason, (socket.gaierror, ConnectionRefusedError)))


def _retried(call, read):
    """Makes a call that nothing answers again, TRIES times at most. A write
    that may have left is not made again: its NoAnswer says the call was not
    confirmed. An HTTP status is an answer, so it is never tried again."""
    for left in range(TRIES - 1, -1, -1):
        try:
            return call()
        except NoAnswer as exc:
            if not read and exc.sent:
                raise NoAnswer("%s; the call was not confirmed: the forge may have done it" % exc)
            if not left:
                raise
            _sleep(RETRY_SECONDS)


def ask(method, url, headers, body=None):
    """The one HTTP call, made again when nothing answers (see _retried)."""
    return _retried(lambda: http(method, url, headers, body), method == "GET")


def _text(raw, headers):
    text = Text(raw.decode("utf-8", "replace"))
    text.headers = {name.lower(): value for name, value in (headers.items() if headers else ())}
    return text


# The scopes of a classic token ride a header of any answer. The tests replace it.
def token_scopes(url, headers):
    """Gives the scopes that X-OAuth-Scopes names, as a list, or None when
    the answer has no such header: a fine-grained token names none. Raises
    NoAnswer when nothing answers."""
    request = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
            found = answer.headers.get("X-OAuth-Scopes")
    except urllib.error.HTTPError as exc:
        found = exc.headers.get("X-OAuth-Scopes") if exc.headers else None
    except (urllib.error.URLError, OSError) as exc:
        raise _no_answer(url, exc)
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

    def request(self, method, url, body=None, accept_text=False, accept=None):
        # accept replaces the forge's Accept for an endpoint that serves no JSON.
        headers = self.headers()
        if accept:
            headers = dict(headers, Accept=accept)
        status, text = ask(method, url, headers, body)
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

    def check_states(self, sha):
        """Gives {key: success | failure | pending | interrupted} for the commit
        statuses on one commit, by the kit's table: STOPPED is interrupted,
        not red. A key with several statuses takes the highest id, then the
        last one."""
        newest = {}
        for item in self.all_values("/commit/%s/statuses" % sha):
            key = item.get("key")
            if key not in newest or (item.get("id") or 0) >= (newest[key].get("id") or 0):
                newest[key] = item
        states = {}
        for key, item in newest.items():
            verdict = check_state(self.provider, item)
            states[key] = _STATE_WORDS.get(verdict, verdict)
        return states

    def merge_when_green(self, number, head_sha, required_checks, method="merge",
                         advisory_checks=()):
        """Merges one pull request when every required status is green on its
        head, in the answers of the GitHub path. A required name is the `key`
        of a commit status; Bitbucket's source hash is short, so the head
        matches head_sha by a prefix. A red advisory status holds the merge."""
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
        held = held_answer(self.check_states(head_sha), required_checks, advisory_checks)
        if held:
            return held
        url = self.url("/pullrequests/%s/merge" % number)
        status, text = ask("POST", url, self.headers(), {
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

    def cancel_run(self, run_id):
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
        # The log is plain text: Accept application/json gets a 406. It comes
        # as a redirect to a signed URL, which the opener follows without the
        # credential (see _KeepCredentialOnHost).
        try:
            return self.request("GET", self.url("/pipelines/%s/steps/%s/log" % (pipeline_id, step_id)),
                                accept_text=True, accept="*/*")
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
    out, or failed with no step past setup. A setup step that failed before
    any other step ran ends the question: a step after it (a crash report
    with `if: always()`) ran on nothing, and its failure is the setup's."""
    conclusion = job.get("conclusion")
    if conclusion in ("cancelled", "timed_out"):
        return True
    if conclusion != "failure":
        return False
    for step in job.get("steps") or []:
        if step.get("conclusion") in (None, "skipped"):
            continue
        if not SETUP_STEP.match(step.get("name") or ""):
            return False
        if step.get("conclusion") == "failure":
            return True
    return True


def workflow_needs(text):
    """Gives {job name: [the names of the jobs it needs]} of a workflow file's
    text, or {} when the text is no workflow."""
    try:
        import yaml
        data = yaml.safe_load(text)
    except Exception:
        return {}
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, dict):
        return {}
    jobs = {str(key): job for key, job in jobs.items() if isinstance(job, dict)}
    names = {key: str(job.get("name") or key) for key, job in jobs.items()}
    found = {}
    for key, job in jobs.items():
        needs = job.get("needs") or []
        if isinstance(needs, str):
            needs = [needs]
        if needs:
            found[names[key]] = [names.get(str(need), str(need)) for need in needs]
    return found


WORKFLOW_LEVELS = 4
"""How deep GitHub lets reusable workflows call one another: the run's own
file is the first level."""


def workflow_calls(text):
    """Gives {job name: path} of each job of a workflow file's text that calls
    a reusable workflow of its own repository, `uses: ./.github/workflows/x.yml`."""
    try:
        import yaml
        data = yaml.safe_load(text)
    except Exception:
        return {}
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, dict):
        return {}
    found = {}
    for key, job in jobs.items():
        uses = job.get("uses") if isinstance(job, dict) else None
        if isinstance(uses, str) and uses.startswith("./.github/workflows/"):
            found[str(job.get("name") or key)] = uses[2:].split("@")[0]
    return found


def job_named(job_name, name, called=False):
    """Tells if a run's job is the workflow's job of this name. A matrix job
    carries its values after the name, or in the place of an expression. With
    called, a job of the reusable workflow this job calls is it too: GitHub
    names that one `caller / job`."""
    if "${{" in name:
        return job_name.startswith(name.split("${{")[0])
    if job_name == name or job_name.startswith(name + " ("):
        return True
    return called and job_name.startswith(name + " / ")


def matrix_leg(job_name, name):
    """Gives (caller, leg) when a run's job is the `caller / job` of this name in
    one leg of a matrix caller at any ` / ` level: `outer (a) / inner / job` or
    `outer / inner (a) / job`. The caller is the name up to that level, the leg
    is the run's job up to it, `outer / inner (a)`. Or None."""
    segments = name.split(" / ")
    for level in range(1, len(segments)):
        caller = " / ".join(segments[:level])
        if not job_name.startswith(caller + " ("):
            continue
        end = job_name.find(") / ", len(caller))
        if end >= 0 and job_named(job_name[end + 4:], " / ".join(segments[level:])):
            return caller, job_name[:end + 1]
    return None


def needs_of_job(job_name, needs):
    """Gives the names of the jobs a run's job needs. A `caller / job` whose
    caller, at any level, is a matrix job needs the jobs of its own leg only."""
    wanted = []
    for name, listed in needs.items():
        if job_named(job_name, name):
            wanted.extend(listed)
            continue
        scope = matrix_leg(job_name, name)
        if scope:
            caller, leg = scope
            wanted.extend(leg + need[len(caller):] if need.startswith(caller + " / ") else need
                          for need in listed)
    return wanted


def mark_aggregates(steps, needs):
    """Marks interrupted each failed job that only gathers the others: every
    job it needs is success, skipped or interrupted, and one is interrupted.
    No test of its own failed, so its red is the runner's too. A job it needs
    can be one that gathers, in any order, so it runs until nothing changes."""
    changed = True
    while changed:
        changed = False
        for step in steps:
            if step.get("result") != "failure" or step.get("interrupted"):
                continue
            wanted = needs_of_job(step.get("name") or "", needs)
            needed = [s for s in steps
                      if s is not step and any(job_named(s.get("name") or "", need, called=True)
                                               for need in wanted)]
            if (any(s.get("interrupted") for s in needed)
                    and all(s.get("interrupted") or s.get("result") in ("success", "skipped")
                            for s in needed)):
                step["interrupted"] = True
                step["aggregate"] = True
                changed = True
    return steps


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
            scopes = _retried(lambda: token_scopes(self.url(""), self.headers()), True)
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
                status, _ = ask("GET", url, self.headers())
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
                status, _ = ask("GET", url, self.headers(token))
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
        status, text = ask(method, url, self.headers(self.secrets_credential()), body)
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
        or {state: waiting, waits_on: github, reason} while GitHub has not
        computed whether it merges. It does not read a second time, as
        merge_when_green does: it has no `behind` to tell from `waiting`, and
        its caller asks again. GitHub's refusal raises ForgeError."""
        data = self.request("GET", self.url("/pulls/%s" % number))
        if data.get("merged") or data.get("merged_at"):
            return {"state": "merged", "sha": data.get("merge_commit_sha")}
        if data.get("mergeable") is None:
            return {"state": "waiting", "waits_on": "github",
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
                "started": job.get("started_at"),
                "interrupted": job_interrupted(job),
            })
        # a red job beside an interrupted one may only gather it: the workflow file tells
        if (any(s["result"] == "failure" and not s["interrupted"] for s in found)
                and any(s["interrupted"] for s in found)):
            mark_aggregates(found, self.needs_of_run(pipeline_id))
        return found

    def needs_of_run(self, run_id):
        """Gives the needs of each job of a run's workflow file, as
        workflow_needs gives them, or {} when the file cannot be read. A job
        that calls a reusable workflow of the repository adds that file's
        needs, both sides named `caller / job` as GitHub names them, and so
        on through each workflow that one calls, `outer / inner / job`."""
        try:
            run = self.request("GET", self.url("/actions/runs/%s" % run_id))
        except (ForgeError, ValueError):
            return {}
        path = (run.get("path") or "").split("@")[0]
        head = run.get("head_sha")
        if not path.startswith(".github/workflows/") or not head:
            return {}
        text = self.workflow_text(path, head)
        found = workflow_needs(text)
        self.add_called_needs(found, text, head, "", 1)
        return found

    def add_called_needs(self, found, text, head, prefix, level):
        """Adds to found the needs of each reusable workflow that text calls,
        named with prefix and the caller, then the needs of the ones those
        call, until GitHub's last level."""
        if level >= WORKFLOW_LEVELS:
            return
        for caller, called in workflow_calls(text).items():
            outer = "%s%s / " % (prefix, caller)
            inner = self.workflow_text(called, head)
            for name, listed in workflow_needs(inner).items():
                found[outer + name] = [outer + need for need in listed]
            self.add_called_needs(found, inner, head, outer, level + 1)

    def workflow_text(self, path, ref):
        """Gives the text of one workflow file at ref, or "" when it cannot be read."""
        try:
            data = self.request("GET", self.url("/contents/" + urllib.parse.quote(path), ref=ref))
            if not isinstance(data, dict):
                return ""
            return base64.b64decode(data.get("content") or "").decode("utf-8", "replace")
        except (ForgeError, ValueError):
            return ""

    def rerun_failed_jobs(self, run_id):
        """Starts the failed and cancelled jobs of one workflow run again."""
        self.request("POST", self.url("/actions/runs/%s/rerun-failed-jobs" % run_id), {})
        return True

    def cancel_run(self, run_id):
        """Cancels one workflow run. A 409, the run already done, counts as done."""
        try:
            self.request("POST", self.url("/actions/runs/%s/cancel" % run_id), {})
        except ForgeError as error:
            if error.status != 409:
                raise
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
        """Gives {name: success | failure | pending | interrupted} for the
        check runs AND the commit statuses on one commit. A check run that
        has not completed is pending; a completed one is read by the kit's
        table (check_state): success, skipped or neutral is a success,
        cancelled, stale or action_required is interrupted, and any other
        conclusion is a failure. A
        name with several check runs (a job that ran again, or the jobs of
        every Actions run on the commit) takes its newest run: one that has
        not completed, else the latest completed_at, then the highest id. A
        name that is both a check run and a status takes the worse of the two."""
        rank = {"success": 0, "pending": 1, "interrupted": 2, "failure": 3}
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
            verdict = check_state(self.provider, item)
            put(item.get("name"), _STATE_WORDS.get(verdict, verdict))
        combined = self.request("GET", self.url("/commits/%s/status" % sha, per_page=100))
        for item in combined.get("statuses") or []:
            state = item.get("state")
            put(item.get("context"), {"success": "success", "pending": "pending"}.get(
                state, "failure"))
        return states

    def merge_when_green(self, number, head_sha, required_checks, method="merge",
                         advisory_checks=()):
        """Merges one pull request when every required check is green on its
        head and no advisory check is red. Gives one answer: {state: merged|closed|waiting|red|interrupted, ...} or
        {refused, reason}. A waiting answer names what it waits on in
        waits_on: `checks` (a required check, named in pending) or `github`
        (the checks are green and GitHub has not judged the pull request).
        The merge names head_sha, so GitHub refuses it when the head moved in
        between."""
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
        held = held_answer(self.check_states(head_sha), required_checks, advisory_checks)
        if held:
            return held
        for _ in range(TRIES - 1):
            if data.get("mergeable") is not None or data.get("mergeable_state") == "behind":
                break
            # GitHub judges a pull request again when its base moves, and the
            # read that finds no judgment starts it: read again, so a green
            # head that is only behind is `behind` and not `waiting`.
            _sleep(RETRY_SECONDS)
            data = self.request("GET", self.url("/pulls/%s" % number))
            if (data.get("merged") or data.get("merged_at") or data.get("state") == "closed"
                    or (data.get("head") or {}).get("sha") != head_sha
                    or data.get("draft") or data.get("mergeable") is False):
                return self.merge_when_green(number, head_sha, required_checks, method,
                                             advisory_checks)
        if data.get("mergeable_state") == "behind":
            # Branch protection wants the branch up to date: update_branch first.
            return {"state": "behind",
                    "reason": "the branch is behind its base: update the branch first"}
        if data.get("mergeable") is None:
            # GitHub has not yet computed whether it merges cleanly. A base
            # that moved is what starts that computing, so a head that lacks
            # commits of its base is behind, not waiting.
            if self.behind_base(data):
                return {"state": "behind",
                        "reason": "the branch is behind its base: update the branch first"}
            return {"state": "waiting", "waits_on": "github", "pending": [],
                    "reason": "github has not yet computed whether the pull request merges"}
        try:
            merged = self.merge_pull_request(number, head_sha, method)
        except ForgeError as exc:
            return {"refused": "merge_refused", "reason": str(exc)}
        return {"state": "merged", "sha": merged.get("sha")}

    def behind_base(self, pr):
        """True when the pull request's head lacks commits of its base branch,
        by the compare. A compare GitHub refuses is not behind."""
        base = (pr.get("base") or {}).get("ref")
        head = (pr.get("head") or {}).get("sha")
        if not base or not head:
            return False
        try:
            data = self.request("GET", self.url("/compare/%s...%s" % (
                urllib.parse.quote(base), head)))
        except ForgeError:
            return False
        return behind(data.get("behind_by"))

    def update_branch(self, number, head_sha):
        """Merges the base into a pull request's branch; never a rebase, never
        a force. Gives {state: updated|current} or {refused, reason}. The
        call names head_sha, so GitHub refuses it when the head moved."""
        url = self.url("/pulls/%s/update-branch" % number)
        status, text = ask("PUT", url, self.headers(), {"expected_head_sha": head_sha})
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
