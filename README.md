# waymark-bench

The bench rig. It is a small MCP server over git. It holds a bare clone
for each repository and a git worktree for each branch. It gives nine
tools: `prepare`, `status`, `find`, `read`, `edit`, `pull`, `submit`,
`feedback` and `discard`. It needs Python 3.11, git, and one library:
pydantic-settings, for the settings.

```
uv sync                      # makes .venv with the one dependency
uv run python -m bench --http 8101 --config bench.json
```

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

The key is the repository's name as the forge spells it, `owner/name`
(the engine names it so), or a plain name. The rig makes
`<data_dir>/<repo>/bare.git` and `<data_dir>/<repo>/wt/<branch>/`. A path that matches a `deny` glob, or
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
python -m bench --http 8101 --config bench.json
python -m bench --stdio --config bench.json
```

The HTTP transport listens on `/mcp/`. It answers JSON-RPC 2.0 on POST:
`initialize`, `notifications/initialized`, `tools/list` and `tools/call`.
Each `tools/call` answer carries the same JSON two times: as one text
part, and as `structuredContent.result`. A refusal is a normal answer
with `isError` true, and its JSON has the field `refused` with the name
of the refusal. The stdio transport reads one message for each line.

## The rig as a service

On macOS the rig runs as a user LaunchAgent, written and started by the
Makefile. The variables `PORT`, `CONFIG` and `LABEL` have defaults
(8101, `~/.config/bench/bench.json`, `io.kopsa.bench`).

```
make install     # venv, LaunchAgent, start; idempotent
make status      # is it running, does it answer on /health
make logs        # follow ~/Library/Logs/bench.log
make restart     # after a change to bench.json
make update      # take a new version: git pull, uv sync, restart
make stop        # stop it and remove the LaunchAgent
```

A restart cuts a running landing: its state reads as failed with the
reason "the bench restarted", and the next `submit` on that branch lands
again. Check `make status` and the landings under `<data_dir>/<repo>/landings/`
before `make update`.

## The call form

```
python -m bench call submit repo=app branch=TICKET-1234 message="fix the thing" --url http://127.0.0.1:8101/mcp/
```

The call form drives one tool of a running rig from the shell, for a
person who lands their own branch. A value that parses as JSON is JSON
(`wait=0`, `pull_request=false`); any other value is a text. It prints
the answer and exits 1 on a refusal.

## The row in waymark

The engine holds one `mcp_server` row with the name `bench`. The
transport is stdio. The command is
`python3 -m bench --stdio --config …`. The `auth_env` is
`BENCH_GIT_TOKEN`. The powers entries of the row name `find`, `read`,
`edit` and `pull`. The tools `prepare`, `status`, `submit`, `feedback`
and `discard` are the engine's own. The engine calls `prepare` before a
sitting, so the model finds the worktree made.

## The narrow call

Each tool takes `seat` and `sitting`. Each one is a text. The rig
writes both in its log line for the call. A refusal gives both back.
On `submit` without `trailers`, the rig writes the trailers
`Waymark-Seat` and `Waymark-Sitting` from them.

`find`, `read` and `edit` also take `allow`: a list of globs in the
`deny` grammar. A path must match one glob of the list. The rig
refuses every other path with `denied` and the list. An empty list
refuses every path. The `deny` globs come first, and a denied path
keeps its `denied` answer with the `deny` glob. In `find`, a path that
no glob matches is not in the answer, and it is not in `dropped`.

The rig holds no seat and no rule between the calls. The engine judges
the grant, and the rig obeys the arguments of the call.

## The tests

`python -m unittest -v`. The tests use a git origin on the disk.
