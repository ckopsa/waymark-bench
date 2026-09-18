"""Helpers for the tests: a real git origin and a data directory.

No test uses the network. The origin is a bare repository on the disk,
and the clone URL is a file:// URL.
"""

import os
import subprocess

from bench import config as config_module


IDENTITY = [
    "-c", "user.name=test",
    "-c", "user.email=test@waymark.invalid",
    "-c", "commit.gpgsign=false",
]


def git(args, cwd):
    """Runs one git command for the test setup."""
    return subprocess.run(
        ["git"] + IDENTITY + list(args),
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


SEED = {
    "README.md": "# demo\n\nThe demo repository of the bench.\n",
    "src/app.py": (
        "def main():\n"
        "    # TODO: give a better name\n"
        "    value = 1\n"
        "    # TODO: add a test\n"
        "    return value\n"
        "\n"
        "MARKER_ONE = 'one'\n"
    ),
    "docs/a.txt": "alpha\nbravo\ncharlie\n",
    "keys/server.pem": "NOT A REAL KEY\n",
    ".github/workflows/ci.yml": "name: ci\n",
}


def make_origin(root):
    """Makes a bare origin with one commit on main. Gives the file:// URL."""
    origin = os.path.join(root, "origin.git")
    seed = os.path.join(root, "seed")
    git(["init", "--bare", "-b", "main", origin], cwd=root)
    os.makedirs(seed, exist_ok=True)
    git(["init", "-b", "main"], cwd=seed)
    for name, text in SEED.items():
        write(os.path.join(seed, name), text)
    git(["add", "-A"], cwd=seed)
    git(["commit", "-m", "the first commit"], cwd=seed)
    git(["remote", "add", "origin", "file://" + origin], cwd=seed)
    git(["push", "-u", "origin", "main"], cwd=seed)
    return "file://" + origin


def make_config(root, clone_url, name="demo"):
    """Gives a Config with one repository and a data directory."""
    data_dir = os.path.join(root, "data")
    os.makedirs(data_dir, exist_ok=True)
    return config_module.from_dict({
        "data_dir": data_dir,
        "repos": {
            name: {
                "clone_url": clone_url,
                "default_branch": "main",
                "deny": ["*.pem", ".env*", "**/secrets/**"],
            },
        },
    })


def clone(root, clone_url, name="other"):
    """Makes a second clone, for a change that comes from another person."""
    path = os.path.join(root, name)
    git(["clone", clone_url, path], cwd=root)
    return path


def push_change(path, branch, name, text, message="a change from another person"):
    """Commits one file on a branch of a clone and pushes it."""
    git(["checkout", "-B", branch], cwd=path)
    write(os.path.join(path, name), text)
    git(["add", "-A"], cwd=path)
    git(["commit", "-m", message], cwd=path)
    git(["push", "origin", branch], cwd=path)
