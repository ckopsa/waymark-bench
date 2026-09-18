"""The entry point of the bench rig.

  python -m bench --http 8101 --config bench.json
  python -m bench --stdio --config bench.json
"""

import argparse
import os
import sys

from . import config as config_module
from . import mcp
from .tools import Bench


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="bench", description="The bench rig: git worktrees over MCP.")
    parser.add_argument("--config", default=os.environ.get("BENCH_CONFIG", "bench.json"),
                        help="The path of bench.json.")
    parser.add_argument("--http", type=int, metavar="PORT",
                        help="Serve MCP over HTTP at /mcp/ on this port.")
    parser.add_argument("--host", default=os.environ.get("BENCH_HOST", "127.0.0.1"),
                        help="The address to listen on. The default is 127.0.0.1.")
    parser.add_argument("--stdio", action="store_true",
                        help="Serve MCP over stdin and stdout.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
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
