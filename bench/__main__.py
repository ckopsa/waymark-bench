"""The entry point of the bench rig.

  python -m bench --http 8101 --config bench.json
  python -m bench --stdio --config bench.json
  python -m bench call submit repo=app branch=TICKET-1 message="fix it" --url http://127.0.0.1:8101/mcp/

The call form drives one tool of a running rig from the shell. A value
that parses as JSON is JSON (true, 3, ["a"]); any other value is a text.
"""

import argparse
import json
import os
import sys
import urllib.request

from . import config as config_module
from . import mcp, settings
from .tools import Bench


def parse_args(argv):
    current = settings.load()
    parser = argparse.ArgumentParser(prog="bench", description="The bench rig: git worktrees over MCP.")
    parser.add_argument("--config", default=current.config,
                        help="The path of bench.json. The setting is BENCH_CONFIG.")
    parser.add_argument("--http", type=int, metavar="PORT",
                        help="Serve MCP over HTTP at /mcp/ on this port.")
    parser.add_argument("--host", default=current.host,
                        help="The address to listen on. The default is 127.0.0.1. The setting is BENCH_HOST.")
    parser.add_argument("--stdio", action="store_true",
                        help="Serve MCP over stdin and stdout.")
    return parser.parse_args(argv)


def parse_call_args(argv):
    parser = argparse.ArgumentParser(prog="bench call", description="Call one tool of a running rig.")
    parser.add_argument("tool", help="The tool name: prepare, status, find, read, edit, pull, "
                                     "submit, feedback, discard, enroll, repos or unenroll.")
    parser.add_argument("pairs", nargs="*", metavar="key=value", help="The arguments of the tool.")
    parser.add_argument("--url", default=settings.load().url,
                        help="The address of the rig. The default is http://127.0.0.1:8101/mcp/. "
                             "The setting is BENCH_URL.")
    parser.add_argument("--timeout", type=int, default=3900, help="The seconds to wait for the answer.")
    return parser.parse_args(argv)


def call(argv):
    """Runs one tool over HTTP and prints its JSON. Exits 1 on a refusal."""
    args = parse_call_args(argv)
    arguments = {}
    for pair in args.pairs:
        if "=" not in pair:
            print("give key=value, not %r" % pair, file=sys.stderr)
            return 2
        key, value = pair.split("=", 1)
        try:
            arguments[key] = json.loads(value)
        except ValueError:
            arguments[key] = value
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": args.tool, "arguments": arguments}}).encode("utf-8")
    request = urllib.request.Request(args.url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as answer:
            message = json.load(answer)
    except OSError as exc:
        print("no answer from %s: %s" % (args.url, exc), file=sys.stderr)
        return 2
    if "error" in message:
        print(json.dumps(message["error"], indent=1), file=sys.stderr)
        return 2
    result = message["result"]
    print(json.dumps(result["structuredContent"]["result"], indent=1, sort_keys=True))
    return 1 if result.get("isError") else 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "call":
        return call(argv[1:])
    args = parse_args(argv)
    if args.http is None and not args.stdio:
        print("give --http PORT or --stdio", file=sys.stderr)
        return 2
    try:
        config = config_module.load(args.config)
    except config_module.ConfigError as exc:
        print("configuration: %s" % exc, file=sys.stderr)
        return 2
    os.makedirs(config.data_dir, exist_ok=True)
    bench = Bench(config)
    if args.stdio:
        mcp.serve_stdio(bench)
        return 0
    httpd = mcp.serve_http(bench, args.http, host=args.host)
    print("bench on http://%s:%s/mcp/ with %s repos"
          % (args.host, httpd.server_address[1], len(config.repos)), file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
