"""The configuration file of the bench.

The file is JSON. It gives a data directory and a map of repositories.
The git credential is never in the file. Git reads the credential from
the environment variable BENCH_GIT_TOKEN, or from the SSH agent.
"""

import json
import os


DEFAULT_DENY = ["*.pem", "*.key", ".env*", "**/secrets/**"]


class ConfigError(Exception):
    """The configuration file is not correct."""


class RepoConfig:
    """One repository on the bench."""

    def __init__(self, name, clone_url, default_branch="main", deny=None):
        self.name = name
        self.clone_url = clone_url
        self.default_branch = default_branch or "main"
        self.deny = list(deny) if deny is not None else list(DEFAULT_DENY)

    def to_dict(self):
        return {
            "name": self.name,
            "clone_url": self.clone_url,
            "default_branch": self.default_branch,
            "deny": list(self.deny),
        }


class Config:
    """The full configuration."""

    def __init__(self, data_dir, repos):
        self.data_dir = data_dir
        self.repos = repos

    def repo(self, name):
        """Gives the repository, or raises ConfigError."""
        try:
            return self.repos[name]
        except KeyError:
            raise ConfigError("unknown repo: %s" % name)

    def names(self):
        return sorted(self.repos)


def from_dict(data, base_dir=None):
    """Makes a Config from a dictionary."""
    if not isinstance(data, dict):
        raise ConfigError("the configuration must be a JSON object")
    data_dir = data.get("data_dir")
    if not data_dir:
        raise ConfigError("data_dir is necessary")
    data_dir = os.path.expanduser(str(data_dir))
    if base_dir and not os.path.isabs(data_dir):
        data_dir = os.path.join(base_dir, data_dir)
    repos_in = data.get("repos") or {}
    if not isinstance(repos_in, dict):
        raise ConfigError("repos must be a JSON object")
    repos = {}
    for name, spec in repos_in.items():
        if not isinstance(spec, dict):
            raise ConfigError("repo %s must be a JSON object" % name)
        clone_url = spec.get("clone_url")
        if not clone_url:
            raise ConfigError("repo %s needs a clone_url" % name)
        repos[name] = RepoConfig(
            name=name,
            clone_url=str(clone_url),
            default_branch=spec.get("default_branch", "main"),
            deny=spec.get("deny"),
        )
    return Config(os.path.abspath(data_dir), repos)


def load(path):
    """Reads the configuration file at path."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise ConfigError("cannot read %s: %s" % (path, exc))
    except ValueError as exc:
        raise ConfigError("cannot parse %s: %s" % (path, exc))
    return from_dict(data, base_dir=os.path.dirname(os.path.abspath(path)))
