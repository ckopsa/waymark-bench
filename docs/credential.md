# The rig's GitHub credentials

The rig holds two GitHub tokens: the main token (`BENCH_GITHUB_TOKEN`, or
`BENCH_GIT_TOKEN`) and the secrets token (`BENCH_SECRETS_TOKEN`). Seats never
hold them: they reach GitHub only through the rig's tools. This page names
every thing the rig does with GitHub and the permission each one needs, so
each token can be made whole, and no larger.

## What the rig does, and what it needs

| Tool | What it does on GitHub | Fine-grained permission | Classic scope |
|---|---|---|---|
| `prepare`, `pull`, `conflicts` | fetch the repository | Contents: read, Metadata: read | `repo` |
| `enroll` | clone the repository | Contents: read, Metadata: read | `repo` |
| `edit`, `edit_many` | nothing: it writes the worktree on the disk | none | none |
| `check` | nothing: it reads the worktree on the disk | none | none |
| `submit` | push the branch | Contents: read and write | `repo` |
| `submit` of a change under `.github/workflows/` | push a workflow file | Workflows: read and write | `workflow` |
| `submit` (landing) | open the pull request, turn on auto-merge | Pull requests: read and write | `repo` |
| `feedback` | read comments, workflow runs and jobs | Pull requests: read, Actions: read | `repo` |
| `feedback`, `merge` | read check runs | Checks: read | `repo` |
| `feedback`, `merge` | read commit statuses | Commit statuses: read | `repo` |
| `merge` | merge the pull request | Contents: read and write, Pull requests: read and write | `repo` |
| `rerun` | re-run the failed jobs of an interrupted workflow run | Actions: read and write | `repo` |
| `dispatch` | start a `workflow_dispatch` workflow, read its runs | Actions: read and write | `repo` |
| `run_status` | read one workflow run and its jobs | Actions: read | `repo` |
| `secret_list` | read the names of the repository's Actions secrets | the secrets token. Secrets: read | not used |
| `secret_set` | read the Actions public key, write one Actions secret | the secrets token. Secrets: read and write | not used |

In short: a classic main token needs `repo` and `workflow`. A fine-grained
main token needs these repository permissions: Contents (read and write),
Pull requests (read and write), Workflows (read and write), Commit statuses
(read), Checks (read), Actions (read and write) and Metadata (read). It does
not need Secrets: remove Secrets from the main token.

Use a fine-grained token, limited to the enrolled repositories. Add a
repository to the token when you enroll it.

## The secrets token

A fine-grained permission applies to every repository the token selects. With
Secrets on the main token, the rig could overwrite the Actions secrets of
every enrolled repository. So `secret_set` and `secret_list` use a second
token, `BENCH_SECRETS_TOKEN`, and no other tool ever uses it. When it is not
set, the two refuse `no_secrets_token`; the main token never stands in for it.

Make it a fine-grained token with only these repository permissions: Secrets
(read and write) and Metadata (read). Select only the repositories that are
meant to take secrets from the rig (today: ckopsa/home-infrastructure). Do
not use a classic token: its `repo` scope reaches every repository.

It reaches the rig the same way `BENCH_GITHUB_TOKEN` does: a Nomad variable
that the job gives the task as the environment variable `BENCH_SECRETS_TOKEN`.
The rig never logs it, and removes its value from every message it gives.

A fine-grained token expires: GitHub asks for an expiry date when you make
it, a year ahead at most. Write the date down beside the Nomad variable. After
that date GitHub answers 401: `secret_set` and `secret_list` refuse `forge`,
and the check reports `secrets_token.ok` false. Make a new token with the same
permissions and repositories, and put it in the Nomad variable. The main
token expires in the same way, on its own date.

## The check

The rig checks its token at start, and at `enroll` for the repository it
enrolls. For each repository with a GitHub forge it reads the repository,
then:

- a classic token names its scopes in the `X-OAuth-Scopes` header, so the
  rig knows each permission above: `repo` gives all but Workflows, and
  `workflow` gives Workflows;
- a fine-grained token names nothing, so the rig reads the check runs and the
  commit statuses of the default branch. A refusal there is a missing
  permission. What only a write could prove (Contents, Pull requests,
  Workflows, and the write half of Actions) is `unverified`, not missing.
  When GitHub refuses `rerun` or `dispatch`, it answers `token_lacks_actions_write`.

Secrets is in neither `missing` nor `unverified`: the main token should not
hold it. The check reports the two tokens apart. It reads the names of the
repository's Actions secrets one time with each token:

- `secrets_token` is `{set, ok}`, with a `reason` when `ok` is not true.
  `set` is false when `BENCH_SECRETS_TOKEN` is not set. `ok` is false when
  GitHub refuses the read: the token expired, or it does not select this
  repository, which is right for a repository that takes no secrets.
- `warnings` holds `main_token_reads_secrets` when the main token can read
  them. Remove Secrets from the main token then. A classic token with `repo`
  always gives this warning.

`ok`, `missing` and `unverified` speak of the main token only.

`repos` and `enroll` answer each repository's check as `credential`:

```json
{"checked": true, "ok": false, "token": "classic",
 "missing": ["workflows"], "unverified": [],
 "secrets_token": {"set": true, "ok": true},
 "warnings": ["main_token_reads_secrets"]}
```

A repository without a GitHub forge answers `{"checked": false, ...}` with the
reason.

When the check says Workflows is missing, a `submit` whose change touches
`.github/workflows/` refuses before it commits, with
`missing_workflow_permission`, instead of failing at the push.
