"""The settings of the rig: the addresses and the credentials.

Every setting is an environment variable with the prefix BENCH_, or a
line of a .env file in the working directory (BENCH_ENV_FILE names
another file). The credentials are secrets: they never print, and
`scrub` removes their values from every message the rig gives.

  BENCH_CONFIG            the path of bench.json
  BENCH_HOST              the address the HTTP transport listens on
  BENCH_URL               the address the call form talks to
  BENCH_GIT_TOKEN         the git credential for an HTTPS clone URL, and GitHub
  BENCH_BITBUCKET_USER    the Bitbucket user (an email or a username)
  BENCH_BITBUCKET_TOKEN   the Bitbucket app password
  BENCH_GITHUB_TOKEN      the GitHub token, when it is not the git token
"""

import os

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BENCH_", env_file=".env", env_file_encoding="utf-8", extra="ignore")

    config: str = "bench.json"
    host: str = "127.0.0.1"
    url: str = "http://127.0.0.1:8101/mcp/"
    git_token: SecretStr | None = None
    bitbucket_user: str | None = None
    bitbucket_token: SecretStr | None = None
    github_token: SecretStr | None = None

    def secret(self, name):
        """Gives the value of one secret, or None."""
        value = getattr(self, name)
        if value is None:
            return None
        value = value.get_secret_value()
        return value or None

    def secrets(self):
        """Gives the values of every set secret."""
        found = []
        for name in ("git_token", "bitbucket_token", "github_token"):
            value = self.secret(name)
            if value:
                found.append(value)
        return found


def load(env_file=None):
    """Reads the settings now. Nothing is cached: a test may change the environment."""
    return Settings(_env_file=env_file or os.environ.get("BENCH_ENV_FILE", ".env"))


def scrub(text):
    """Removes every secret from a text."""
    text = text or ""
    for value in load().secrets():
        text = text.replace(value, "***")
    return text
