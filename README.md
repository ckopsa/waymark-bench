# waymark-bench

The bench rig. It is a small MCP server over git. It holds a bare clone
for each repository and a git worktree for each branch. It gives twenty-nine
tools: `prepare`, `status`, `find`, `read`, `symbols`, `read_symbol`, `check`, `edit`,
`edit_many`, `pull`, `conflicts`, `submit`, `feedback`, `log`, `discard`, `merge`, `update_branch`, `rerun`, `test`,
`test_result`, `enroll`, `repos`, `unenroll`, `train_build`, `train_checks`,
`train_open`, `train_status`, `train_land` and `train_delete`. It
needs Python
3.11, git, and two libraries: pydantic-settings, for the settings, and
PyYAML, so `check` can parse the changed `.github` YAML files.

```
uv sync                      # makes .venv with the two dependencies
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

The file can hold `data_dir` only. The repositories then come from the
engine: it calls `enroll` for each one. The rig holds those entries in
`<data_dir>/repos.json`, and it writes that file itself. The rig reads
bench.json first, and it reads `repos.json` after it. An entry of
`repos.json` wins over an entry of bench.json with the same name.

## A worktree that is old

`prepare` fetches, but it does not move a worktree that exists. Its
answer tells you how old the worktree is. `behind` counts the commits of
the base that the worktree does not have, as `status` does.
`behind_remote` counts the commits of `origin/<branch>` that the
worktree does not have. It is `null` when the branch is not on the
remote. When one of the two is more than zero, `note` names the pull to
use: `pull` from `head` moves the worktree to the remote branch, and
`pull` from `base` merges the base in.

`dirty` counts the uncommitted paths of the worktree, and `dirty_paths`
names them, up to 100. When a worktree that exists is dirty and its head
is already pushed (it is `origin/<branch>` or an ancestor of it), the
uncommitted paths are no seat's work: a failed pull left them. `prepare`
resets them and names them in `dropped`, up to 100. It drops nothing
when the branch is not on the remote, when the branch has commits the
remote does not, or while a merge is in progress, and it keeps an
untracked path that matches a `deny` glob. `dropped` is `[]` on a
worktree that `prepare` made.

A `pull` that fails puts the worktree back as it was before it. Its
refusal, `not_fast_forward` (from `head`) or `merge_failed` (from
`base`, with no conflicts), carries `reset`: the paths it put back, up
to 100.

`conflicts` trial-merges the base (`base`, default the repo's default
branch) into a clean worktree, gives the unmerged `paths`, and aborts
the merge, so the worktree is left as it was. A dirty worktree is
refused (`dirty`). The engine calls it; it is in no powers entry.

`read` with a `ref` reads the ref as the last fetch left it. `prepare`
and `pull` fetch.

## Definitions by name

| Tool | Input | Answer |
| --- | --- | --- |
| `symbols` | `path` (a file or a directory), `pattern` (a regex over the names), `max_matches` | `symbols`: each `{name, kind, path, line, end_line}` |
| `read_symbol` | `path`, `symbol`, and `ref` if you want one | `definitions`: each match with its range and its numbered `lines` |

`find` has no mode `symbols` and `read` takes no `symbol`: each refuses
it and names the tool to use.

A Clojure, ClojureScript or edn definition is a top-level form whose
head starts with `def`, with or without an alias: `defn-`, `defmethod`,
`g/defguard`. Its `end_line` is the line where its parens balance; the
reader skips strings, comments and character literals. A Python
definition is a `def`, an `async def` or a `class` at column 0, and a
method one level in, named `Class.method`. A `read_symbol` of several matches
(the `defmethod`s of one multi) gives each, up to 20, under `max_bytes`.
A name the file does not define is refused `not_found` with `close`,
the names that look like it.

## The enrollment

The rig holds a mirror of the engine's rows. It is not a second ledger.
Only the engine calls these three tools. Put them in no powers entry.

`enroll` puts one repository on the rig. Give `repo` (the name) and
`clone_url`. Give `default_branch` (the default is `main`), `deny` (the
default is the rig's list) and `land` (the landing block, as bench.json
spells it). The rig makes the bare clone one time, then it writes the
entry in `repos.json`. The answer gives the entry, `bare` (the path of
the clone) and `cloned` (true when this call made the clone). An
`enroll` for a name the rig holds replaces the entry and keeps the
clone. A clone that fails is a refusal `clone_failed` with the reason
from git, and the rig writes no entry.

`repos` gives every repository on the rig, by name. Each one gives its
entry, `bare_exists` for the clone on the disk, and `source`: `file` for
a repository from `enroll`, `config` for a repository from bench.json.

`unenroll` takes one repository off the rig. Give `repo`. The rig
removes the entry from `repos.json`. The rig keeps the clone and the
worktrees on the disk, and the answer gives that path as `kept`. A
repository from bench.json is a refusal `config_repo`: remove it from
bench.json.

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
                         "repo": "app", "close_source_branch": true,
                         "auto_merge": false}
      }
    }
  }
}
```

`target` defaults to the default branch. `rebase` defaults to true. A
branch that carries a merge commit (a seat merged the target in to
resolve conflicts) takes the target by a merge instead, so the
resolution is kept. The
names `rebase`, `push` and `pull_request` are the rig's own steps. `env`
is added to the environment of every step; a `~` is expanded. The
`pull_request` block names the forge (`bitbucket` or `github`); with
`true` the forge is read from the clone URL. `close_source_branch`
deletes the branch on the merge. `auto_merge` turns auto-merge on for the
pull request: the forge merges it when the checks are green, with the
merge method of the repository. It defaults to false, and only GitHub
does it. A forge that refuses auto-merge gives a finding `auto_merge` in
`feedback`; the landing keeps the pull request.

GitHub's auto-merge needs a public repository or a paid plan. With
`"merge_by": "house"` in the `pull_request` block the rig does not ask
the forge for auto-merge; the house merges with the `merge` tool
instead. Give `number`, `head_sha` and `required_checks` (and `method`:
`merge`, `squash` or `rebase`). The rig reads the check runs and the
commit statuses on `head_sha` and answers `waiting` (a required check is
missing or running), `red` (one failed), `closed`, or `merged` - it
merges only when every required check is success, and names `head_sha`
in the merge so GitHub refuses a moved head. It refuses a moved head
(`head_moved`), a draft, a conflict (`not_mergeable`) and an empty
`required_checks`: the rig never merges a change nothing has tested.
When the checks are green but GitHub says the branch is `behind` its
base, `merge` answers `behind` and does not try the merge.

`update_branch` brings such a branch up to date: give `number` and
`head_sha` (the head the engine saw). GitHub merges the base into the
branch - never a rebase, never a force - and the call names `head_sha`,
so a moved head is refused (`head_moved`). It answers `updated` (the
head will move and the checks run again) or `current` (already up to
date), and refuses a conflict (`not_mergeable`), any other GitHub
refusal (`update_refused`) and a forge that is not GitHub
(`unsupported`).

CI can die with the code sound: a runner freezes, a job is cancelled or
times out, or it fails in checkout or container set-up before any test
runs. `feedback` gives such a run one finding of severity `interrupted`
(not `error`) whose message starts `ci: interrupted`. `rerun` (give
`repo` and `branch`) then asks GitHub to re-run the failed jobs of each
such run on the branch's pushed head, and answers the `run_id`. It does
so one time per head (`already_rerun` after), and refuses `not_pushed`,
`red` (a job failed in a test step: fix the code) and
`nothing_interrupted`.

`test` dispatches one test selection of a branch on the repository's
own CI and answers at once `{run_id, run_url, conclusion: pending}`.
It tests the worktree as it stands: when the worktree holds uncommitted
edits, `test` commits them to a scratch commit on the head, pushes that
to the `bench-test/<branch>` scratch ref (never the work branch or the
pull request) and answers `head` (the scratch commit),
`dirty_included: true` and `paths`; a clean worktree runs its head with
`dirty_included: false`. `test_result` echoes `dirty_included`.
`test_result {run_id, wait_seconds}` reads that run every 5 seconds for
up to `wait_seconds` (25 by default, the setting `BENCH_TEST_WAIT`; 28 at
most) and answers `success`, `cancelled`, `failure` with
`failures: [{test, job, lines}]` (4 KB in all), or `pending`: ask again.
It waits up to 25 s, under the engine's 30 s limit on a call,
and `test_result` never dispatches. When the run did not show within
about 15 seconds, `test` answers `run_id: null` with `head` and
`dispatched_at`, and `test_result` finds the run by them.

The repository's `test` block is `{workflow, input, select_pattern}`.
`select_pattern` is optional: a regex that the whole `select` must
match, or `test` refuses `input` before it dispatches. Without it,
`select` must have the Clojure shape: a namespace, dotted and ending in
`-test` (`factory10.merge-line-test`), or `namespace/test-name`. A
Python repository names its unittest shape, as waymark-bench does:

```json
"test": {"workflow": "tests.yml", "input": "only",
         "select_pattern": "^[A-Za-z_]\\w*(?:\\.[A-Za-z_]\\w*)+$"}
```

It passes `tests.test_bench_test` and `tests.test_bench_test.TestTrain`,
and it refuses `test-factory`.

`test` takes `order` instead of `select`: a list of namespaces to run in
that order in one run, for a failure that depends on the order. Each
namespace must pass the same check as `select`; the rig joins them with
spaces and dispatches them as the input the test block's optional
`order_input` names (`order` by default). `order: ["a.x-test",
"a.y-test"]` sends `order="a.x-test a.y-test"`. Giving both `select`
and `order` is refused `input`, and `select` alone works as before.

The landing runs in the background, and `submit` answers at once with
the landing running: follow it with `status` or `feedback`. Give `wait`
to have `submit` wait up to that many seconds instead (the ceiling is
28, under the engine's 30 s limit on a call; a larger wait is cut to
it). The default is 0 because a landing runs a repo's whole test suite,
and a client that brokers the call - a waymark engine gives up on any
call after 30 seconds and marks the server dark - must not be held that
long. `landing.steps` has one entry for each step, with its state,
seconds, exit code and the tail of its output. A failed step is a
refusal `landing_failed` that carries the same. While a landing
runs, `edit`, `pull`, `discard` and `submit` are refused with
`landing_running`; `status` and `feedback` give its state. The state is
on disk under `<data_dir>/<repo>/landings/`, so it outlives a restart.

`feedback` gathers what the change caused: the landing, the pull request
and its state, the newest pipelines with the log of each failed step, the
commit statuses (a quality gate is one), and the review comments. Every
item also comes as one finding: a `source` (`landing`, `pipeline`,
`status`, `review`, `pull_request`, `auto_merge`), a `severity`, a
`message`, and the `path:line` locations it names. A source the rig cannot reach is named in
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

## The rig as a container

The image is `ghcr.io/ckopsa/waymark-bench`, built for arm64 by
`.github/workflows/image.yml` on each push to main that changes the rig,
and tagged with the short commit sha and `latest`. A pull request
builds the image and publishes nothing.

The image holds a configuration with the data directory only,
`/etc/bench/bench.json` with `data_dir` `/data`. Repositories come from
the engine through `enroll` and persist in `/data/repos.json`. Mount a
volume at `/data`: it holds the clones, the worktrees and the landings.
A container that loses it clones again on the next `enroll` or
`prepare`.

The job gives the container these things:

| what | how |
|---|---|
| the port | publish 8101; the rig binds `0.0.0.0` in the image |
| the volume | a host volume at `/data` |
| the git credential | `BENCH_GIT_TOKEN` in the environment |
| the forge token | `BENCH_GITHUB_TOKEN` (or `BENCH_BITBUCKET_USER` and `BENCH_BITBUCKET_TOKEN`) when `feedback` reads pull requests and pipelines |
| the health check | `GET /health` answers 200 |

The deploy is one Nomad variable. The workflow writes
`nomad/jobs/waymark-bench/deploy` with `image_tag`, and the job template
reads it and restarts the task on the new tag. The workflow needs
`NOMAD_ADDR` and `NOMAD_TOKEN` in the repository's secrets; without them
it pushes the image and says that nothing is rolled. `make image` and
`make deploy` do the same from a laptop, with the same tag.

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
`check`, `edit` and `pull`. `check` lints the files a change touched
before submit: balanced forms for Clojure, with clj-kondo's errors when
the rig has it, and a compile check for Python. It never writes. The tools `prepare`, `status`, `submit`, `feedback`,
`discard`, `enroll`, `repos` and `unenroll` are the engine's own. The engine calls `prepare` before a
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

## Many edits in one call

`edit_many` takes `edits`: a list of up to 50 edits, each shaped as one edit
(`path` with `old` and `new`, `new` with `create: true`, `delete: true`,
or `move_to`). The paths may differ. The rig judges every edit in order
before it writes a byte: one refused edit writes none of them, and the
refusal names it in `item`, counting from 1. The answer gives each
edit's path and hash, never the content. `edits` beside `path` or the
other fields of one edit is refused. `edit` takes exactly one edit, and
refuses `edits` with the name of `edit_many`.

## A job's log

`feedback` gives about 4 KB of each failed job's log, with the job's name,
the numbers of its marked lines and a hint. `log {repo, branch, job?, mode}`
reads the rest, a small answer at a time, from the newest run on the
branch's head. Without `job` it lists the jobs with their result and line
count. `mode: "markers"` (the default) gives the test report's lines with
`context` lines around them; `mode: "grep"` gives what a regex `pattern`
finds; `mode: "range"` gives `limit` lines from `offset`, counting from 1.
Each line comes without colors and without GitHub's timestamp, cut at
`width` characters (200 by default) and ending in `… (+N)` when cut. The rig
keeps a log an hour, so paging fetches it once.

## The merge train

Five tools build, check, land and delete a `train/*` branch. Each names
`repo` (on the bench) and a `branch` that starts with `train/`; any other
branch is refused `not_train`. None of them force-pushes a base.

- `train_build {base, branch, prs}` resets `branch` at the base's current
  head, merges each pull request's head in the order of `prs` with a
  merge commit, skips one that does not merge cleanly (the merge is
  aborted), and pushes the branch. It answers
  `{branch, base_head, head, merged: [n...], conflicted: [n...]}`.
- `train_checks {branch, workflow, input}` dispatches `workflow` (default
  the `test` block's) on the pushed branch, with no narrowing, and answers
  `{workflow, run_id, head}`: `workflow` names the one it dispatched, also
  when `run_id` is `null` because no run showed within about 15 seconds.
  Give that `workflow` to `train_status` with the branch and head.
- `train_status {run_id}` (or `{branch, head, workflow}`) reads the run
  once and answers `{state: pending|success|failure|cancelled, head, url}`.
- `train_land {base, branch, expect_base_head, head}` lands the train
  through one pull request, so the base's branch protection stays on.
  When the base is still at `expect_base_head` and the branch at `head`,
  it opens a pull request from the branch into the base, titled
  `Merge train: #a #b` with one line per rider (read from the train's
  merge commits), or reuses the open one on a retry. It merges it at
  sha `head` with a merge commit, never squash or rebase, so each
  rider's commits stay reachable and GitHub marks each rider merged. It
  answers `{landed: true, number, sha}`, or `{state: waiting, number,
  pending}` while GitHub has not computed mergeability or a required
  check is pending: ask again. A moved base is refused `base_moved` with
  the `base_head` it is at, before any pull request is opened, and so is
  GitHub's refusal that the base was modified: build the train again.
  Any other GitHub refusal is `merge_refused` with its words in
  `reason`. `expect_base_head` must be a whole sha of 40 hex characters
  (case and spaces do not matter); any other value is refused `input`.
- `train_delete {branch}` deletes the train branch on the remote.

## The tests

`python -m unittest -v`. The tests use a git origin on the disk.
