"""The configuration file of the bench.

The file is JSON. It gives a data directory and a map of repositories.
The git credential is never in the file. Git reads the credential from
the environment variable BENCH_GIT_TOKEN, or from the SSH agent.
"""

import json
import os
import re


DEFAULT_DENY = ["*.pem", "*.key", ".env*", "**/secrets/**"]
DEFAULT_STEP_TIMEOUT = 1800
CEILING_STEP_TIMEOUT = 7200
STAGE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
RESERVED_STAGES = ("rebase", "push", "pull_request")


class ConfigError(Exception):
    """The configuration file is not correct."""


class StageConfig:
    """One step of a landing: a shell command in the worktree."""

    def __init__(self, name, command, commit=None, timeout=DEFAULT_STEP_TIMEOUT):
        self.name = name
        self.command = command
        self.commit = commit
        self.timeout = timeout

    def to_dict(self):
        return {"name": self.name, "command": self.command, "commit": self.commit,
                "timeout": self.timeout}


class LandConfig:
    """What submit does after the commit, for one repository."""

    def __init__(self, target, rebase=True, stages=None, env=None, pull_request=None):
        self.target = target
        self.rebase = rebase
        self.stages = list(stages or [])
        self.env = dict(env or {})
        self.pull_request = pull_request

    def to_dict(self):
        return {
            "target": self.target,
            "rebase": self.rebase,
            "stages": [stage.to_dict() for stage in self.stages],
            "env": dict(self.env),
            "pull_request": self.pull_request,
        }


class RepoConfig:
    """One repository on the bench."""

    def __init__(self, name, clone_url, default_branch="main", deny=None, land=None):
        self.name = name
        self.clone_url = clone_url
        self.default_branch = default_branch or "main"
        self.deny = list(deny) if deny is not None else list(DEFAULT_DENY)
        self.land = land

    def to_dict(self):
        return {
            "name": self.name,
            "clone_url": self.clone_url,
            "default_branch": self.default_branch,
            "deny": list(self.deny),
            "land": self.land.to_dict() if self.land else None,
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
        default_branch = spec.get("default_branch", "main")
        repos[name] = RepoConfig(
            name=name,
            clone_url=str(clone_url),
            default_branch=default_branch,
            deny=spec.get("deny"),
            land=land_from_dict(name, spec.get("land"), default_branch),
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


def land_from_dict(repo_name, data, default_branch):
    """Makes a LandConfig from the land block of one repository, or None."""
    if data is None or data is False:
        return None
    if not isinstance(data, dict):
        raise ConfigError("repo %s: land must be a JSON object" % repo_name)
    stages = []
    names = set()
    for index, item in enumerate(data.get("stages") or []):
        if not isinstance(item, dict):
            raise ConfigError("repo %s: stage %s must be a JSON object" % (repo_name, index))
        stage_name = str(item.get("name") or "")
        command = item.get("command")
        if not STAGE_NAME.match(stage_name):
            raise ConfigError("repo %s: stage %s needs a name of A-Z a-z 0-9 _ -"
                              % (repo_name, index))
        if stage_name in RESERVED_STAGES or stage_name in names:
            raise ConfigError("repo %s: the stage name %s is taken" % (repo_name, stage_name))
        if not command or not isinstance(command, str):
            raise ConfigError("repo %s: stage %s needs a command" % (repo_name, stage_name))
        timeout = item.get("timeout", DEFAULT_STEP_TIMEOUT)
        try:
            timeout = min(max(int(timeout), 1), CEILING_STEP_TIMEOUT)
        except (TypeError, ValueError):
            raise ConfigError("repo %s: stage %s has a bad timeout" % (repo_name, stage_name))
        commit = item.get("commit")
        if commit is not None and not isinstance(commit, str):
            raise ConfigError("repo %s: stage %s: commit is the commit message, a text"
                              % (repo_name, stage_name))
        names.add(stage_name)
        stages.append(StageConfig(stage_name, command, commit=commit or None, timeout=timeout))
    env = data.get("env") or {}
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        raise ConfigError("repo %s: land.env must map names to texts" % repo_name)
    env = {str(k): os.path.expanduser(v) for k, v in env.items()}
    pull_request = data.get("pull_request")
    if pull_request is True:
        pull_request = {}
    if pull_request is False:
        pull_request = None
    if pull_request is not None and not isinstance(pull_request, dict):
        raise ConfigError("repo %s: land.pull_request must be a JSON object or a boolean"
                          % repo_name)
    return LandConfig(
        target=str(data.get("target") or default_branch),
        rebase=bool(data.get("rebase", True)),
        stages=stages,
        env=env,
        pull_request=pull_request,
    )
