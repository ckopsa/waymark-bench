# waymark-bench

The bench rig. It is a small MCP server over git. It holds a bare clone
for each repository and a git worktree for each branch. It gives eight
tools: `prepare`, `status`, `find`, `read`, `edit`, `pull`, `submit` and
`discard`. It needs Python 3.11 and git, and no other dependency.

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

## The credential

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

## The mount in Gate

Mount the rig as the rig `bench`, so the tools come to a seat as
`bench__prepare`, `bench__status`, `bench__find`, `bench__read`,
`bench__edit`, `bench__pull`, `bench__submit` and `bench__discard`. Gate
gives the HTTP address of the rig. The engine calls `prepare` before a
sitting, so the model finds the worktree made.

## The tests

`python -m unittest -v`. The tests use a git origin on the disk.
