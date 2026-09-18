# waymark-bench

The bench rig. It is a small MCP server over git. It holds a bare clone
for each repository and a git worktree for each branch. It gives nine
tools: `prepare`, `status`, `find`, `read`, `edit`, `pull`, `submit`,
`feedback` and `discard`. It needs Python 3.11 and git, and no other
dependency.

## Configuration

The configuration is one JSON file. The credential is never in the file.

```json
{
  "data_dir": "/var/lib/bench",
  "repos": {
    "waymark": {
      "clone_url": "https://github.com/ckopsa/waymark",
      "default_branch": "main",
      "deny": ["*.pem", "*.key", ".env*", "**/secrets/**"]
    }
  }
}
```

The rig makes `<data_dir>/<repo>/bare.git` and
`<data_dir>/<repo>/wt/<branch>/`. A path that matches a `deny` glob, or
that goes out of the worktree, is never served.

## The landing

A repository with a `land` block does not just push on `submit`. It
lands: it rebases the branch onto the target, runs the steps in order,
pushes, and opens the pull request. Each step is a shell command in the
worktree. A step with `commit` commits what it changed (a formatter).

```json
{
  "repos": {
    "app": {
      "clone_url": "git@bitbucket.org:acme/app.git",
      "default_branch": "dev",
      "land": {
        "target": "dev",
        "rebase": true,
        "stages": [
          {"name": "setup",  "command": "uv sync --extra test --extra dev", "timeout": 900},
          {"name": "format", "command": "uv run ruff format && uv run ruff check --fix",
           "commit": "auto-format"},
          {"name": "test",   "command": "uv run pytest -x -q", "timeout": 3600}
        ],
        "env": {"DOCKER_HOST": "unix://~/.colima/default/docker.sock"},
        "pull_request": {"provider": "bitbucket", "workspace": "acme",
                         "repo": "app", "close_source_branch": true}
      }
    }
  }
}
```

`target` defaults to the default branch. `rebase` defaults to true. The
names `rebase`, `push` and `pull_request` are the rig's own steps. `env`
is added to the environment of every step; a `~` is expanded. The
`pull_request` block names the forge (`bitbucket` or `github`); with
`true` the forge is read from the clone URL.

The landing runs in the background. `submit` waits up to `wait` seconds
(the default is 600) and gives `landing.steps`: one entry for each step,
with its state, seconds, exit code and the tail of its output. A failed
step is a refusal `landing_failed` that carries the same. While a landing
runs, `edit`, `pull`, `discard` and `submit` are refused with
`landing_running`; `status` and `feedback` give its state. The state is
on disk under `<data_dir>/<repo>/landings/`, so it outlives a restart.

`feedback` gathers what the change caused: the landing, the pull request
and its state, the newest pipelines with the log of each failed step, the
commit statuses (a quality gate is one), and the review comments. Every
item also comes as one finding: a `source` (`landing`, `pipeline`,
`status`, `review`, `pull_request`), a `severity`, a `message`, and the
`path:line` locations it names. A source the rig cannot reach is named in
`unavailable`; the answer never fails for it.

## The credential

The forge credential comes from the environment: `BENCH_BITBUCKET_USER`
and `BENCH_BITBUCKET_TOKEN` (an app password) for Bitbucket,
`BENCH_GITHUB_TOKEN` or `BENCH_GIT_TOKEN` for GitHub. On macOS, Bitbucket
also reads the keychain item for `bitbucket.org` when the environment has
nothing. The rig removes every credential from every message it gives.


Git reads the credential from the environment variable
`BENCH_GIT_TOKEN`, through a credential helper. For an SSH clone URL,
use the SSH agent. The rig does not write the credential to disk, and it
removes the credential from every message that it gives back.

## The two transports

```
BENCH_GIT_TOKEN=... python -m bench --http 8101 --config bench.json
BENCH_GIT_TOKEN=... python -m bench --stdio --config bench.json
```

The HTTP transport listens on `/mcp/`. It answers JSON-RPC 2.0 on POST:
`initialize`, `notifications/initialized`, `tools/list` and `tools/call`.
Each `tools/call` answer carries the same JSON two times: as one text
part, and as `structuredContent.result`. A refusal is a normal answer
with `isError` true, and its JSON has the field `refused` with the name
of the refusal. The stdio transport reads one message for each line.

## The call form

```
python -m bench call submit repo=app branch=TICKET-1234 message="fix the thing" --url http://127.0.0.1:8101/mcp/
```

The call form drives one tool of a running rig from the shell, for a
person who lands their own branch. A value that parses as JSON is JSON
(`wait=0`, `pull_request=false`); any other value is a text. It prints
the answer and exits 1 on a refusal.

## The mount in Gate

Mount the rig as the rig `bench`, so the tools come to a seat as
`bench__prepare`, `bench__status`, `bench__find`, `bench__read`,
`bench__edit`, `bench__pull`, `bench__submit`, `bench__feedback` and
`bench__discard`. Gate
gives the HTTP address of the rig. The engine calls `prepare` before a
sitting, so the model finds the worktree made.

## The tests

`python -m unittest -v`. The tests use a git origin on the disk.
