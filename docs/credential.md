# The rig's GitHub credential

The rig holds one GitHub token (`BENCH_GITHUB_TOKEN`, or `BENCH_GIT_TOKEN`).
Seats never hold it: they reach GitHub only through the rig's tools. This
page names every thing the rig does with GitHub and the permission each one
needs, so the token can be made whole, and no larger.

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

In short: a classic token needs `repo` and `workflow`. A fine-grained token
needs these repository permissions: Contents (read and write), Pull requests
(read and write), Workflows (read and write), Commit statuses (read), Checks
(read), Actions (read and write) and Metadata (read).

Use a fine-grained token, limited to the enrolled repositories. Add a
repository to the token when you enroll it.

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
  When GitHub refuses `rerun`, it answers `token_lacks_actions_write`.

`repos` and `enroll` answer each repository's check as `credential`:

```json
{"checked": true, "ok": false, "token": "classic",
 "missing": ["workflows"], "unverified": []}
```

A repository without a GitHub forge answers `{"checked": false, ...}` with the
reason.

When the check says Workflows is missing, a `submit` whose change touches
`.github/workflows/` refuses before it commits, with
`missing_workflow_permission`, instead of failing at the push.
