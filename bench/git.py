"""Thin wrappers around the git command.

Every call has a timeout and a working directory. The git credential
comes from the setting BENCH_GIT_TOKEN (see settings.py) through a
credential helper that reads it from the environment of the git call.
The rig never writes the credential to disk, and it removes the
credential from every message that it gives back.
"""

import os
import subprocess

from . import settings


DEFAULT_TIMEOUT = 120

IDENTITY = [
    "-c", "user.name=waymark-bench",
    "-c", "user.email=bench@waymark.invalid",
]

# The helper reads the token from the environment at the time of the call.
CREDENTIAL_HELPER = (
    "!f() { "
    'echo "username=x-access-token"; '
    'echo "password=$BENCH_GIT_TOKEN"; '
    "}; f"
)


class GitError(Exception):
    """A git command failed."""

    def __init__(self, argv, returncode, stderr):
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr
        Exception.__init__(
            self, "git %s failed (%s): %s" % (" ".join(argv[:2]), returncode, stderr)
        )


def scrub(text):
    """Removes every secret from a text."""
    return settings.scrub(text)


def config_args(token=None):
    """Gives the -c flags for every call."""
    args = list(IDENTITY) + ["-c", "advice.detachedHead=false"]
    if token:
        # An empty value first: it drops the helpers of the system.
        args += ["-c", "credential.helper=", "-c", "credential.helper=" + CREDENTIAL_HELPER]
    return args


def environment(token=None):
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "true"
    env["LC_ALL"] = "C"
    if token:
        env["BENCH_GIT_TOKEN"] = token  # The helper reads it here, wherever it came from.
    else:
        env.pop("BENCH_GIT_TOKEN", None)
    return env


def run(argv, cwd=None, timeout=DEFAULT_TIMEOUT, check=True):
    """Runs one git command. Gives (returncode, stdout, stderr)."""
    token = settings.load().secret("git_token")
    command = ["git"] + config_args(token) + list(argv)
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            env=environment(token),
            timeout=timeout,
            capture_output=True,
            text=True,
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise GitError(list(argv), -1, "timeout after %s seconds" % timeout)
    out = scrub(proc.stdout)
    err = scrub(proc.stderr)
    if check and proc.returncode != 0:
        raise GitError(list(argv), proc.returncode, err.strip() or out.strip())
    return proc.returncode, out, err


def out(argv, cwd=None, timeout=DEFAULT_TIMEOUT):
    """Runs one git command and gives the standard output."""
    return run(argv, cwd=cwd, timeout=timeout)[1]


def line(argv, cwd=None, timeout=DEFAULT_TIMEOUT):
    """Runs one git command and gives the first line of the output."""
    return out(argv, cwd=cwd, timeout=timeout).strip()


def ref_exists(ref, cwd):
    """Tells if a ref is in the repository."""
    code = run(["rev-parse", "--verify", "--quiet", ref + "^{commit}"], cwd=cwd, check=False)[0]
    return code == 0


def rev_parse(ref, cwd):
    """Gives the object name of a ref."""
    return line(["rev-parse", ref], cwd=cwd)
