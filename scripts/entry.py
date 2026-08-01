"""engraphis - the single front door, so the portal's copy-paste command actually runs.

Every capability already ships as its own ``engraphis-<verb>`` console script; this adds
the spelling customers are *shown*.  The account portal hands out

    engraphis connect --token engr_ct_...

and until there was an ``engraphis`` executable that string was not a runnable command.

This is a dispatcher, not a second CLI: it rewrites ``sys.argv`` and calls the same
``main()`` the matching ``engraphis-<verb>`` script calls, so a verb behaves identically
either way and there is exactly one implementation of each command.  The import is lazy
so ``engraphis connect`` never drags in the memory/embedding stack that ``engraphis cli``
needs.
"""
from __future__ import annotations

import sys
from importlib import import_module

#: verb -> "module:function", mirroring ``[project.scripts]`` with the prefix dropped.
COMMANDS = {
    "connect": "scripts.connect:main",
    "init": "scripts.init:main",
    "cli": "scripts.cli:main",
    "mcp": "engraphis.mcp_cli:main",
    "server": "scripts.start_server:main",
    "dashboard": "scripts.start_dashboard:main",
    "inspector": "scripts.inspector:main",
    "consolidate": "scripts.consolidate:main",
    "graph": "scripts.graph_cli:main",
    "graph-server": "scripts.graph_server:main",
    "update": "scripts.update:main",
}

_USAGE = """usage: engraphis <command> [options]

commands:
  connect        redeem the connect token from your account portal
  init           write a project .env and print agent setup snippets
  cli            store and recall memories from the terminal
  mcp            run the MCP server (Claude Code, Cursor, Cline, Zed)
  server         run the v2 REST server without opening a browser (compatibility alias)
  dashboard      run the product dashboard
  inspector      inspect the local database
  consolidate    run consolidation over stored memories
  graph          query the knowledge graph
  graph-server   run the graph server
  update         check for and install a newer Engraphis release

Run `engraphis <command> --help` for a command's options.
Every command is also installed as `engraphis-<command>`."""


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        print(_USAGE)
        return 0 if args else 2
    if args[0] in {"-V", "--version"}:
        from engraphis import __version__

        print(__version__)
        return 0

    verb = args[0]
    target = COMMANDS.get(verb)
    if target is None:
        print("engraphis: unknown command %r\n" % verb, file=sys.stderr)
        print(_USAGE, file=sys.stderr)
        return 2

    module_name, _, attribute = target.partition(":")
    command = getattr(import_module(module_name), attribute)

    # Hand the verb its own argv. Some targets take ``main(argv)`` and some read
    # ``sys.argv`` directly, so rewriting is the one approach that works for all of
    # them -- and it also puts "engraphis connect" in that command's --help/usage.
    saved = sys.argv
    sys.argv = ["engraphis %s" % verb] + args[1:]
    try:
        result = command()
    finally:
        sys.argv = saved
    return 0 if result is None else int(result)


if __name__ == "__main__":
    raise SystemExit(main())
