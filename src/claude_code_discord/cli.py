"""Command-line entry point: `claude-code-discord <command>`.

MVP commands (stubs for now — implemented incrementally):
  init      interactive setup: Discord token + channel, merge hooks into settings.json, test ping
  status    show which hooks are wired and whether the bot can reach the channel
  off       disable the hooks (leave config in place)

This is a skeleton. Each command below raises NotImplementedError with a note
on what it will do, so the surface is defined before the implementation lands.
"""

from __future__ import annotations

import argparse
import sys


def cmd_init(args: argparse.Namespace) -> int:
    """Interactive setup. Will: prompt for a Discord bot token (or walk through
    creating one), ask which channel, merge the hook block into
    ~/.claude/settings.json (merge, never clobber), then send a test ping to
    confirm the bot can post."""
    raise NotImplementedError("init: interactive setup not yet implemented")


def cmd_status(args: argparse.Namespace) -> int:
    """Report which hooks are wired in settings.json and whether the configured
    bot token can reach the configured channel."""
    raise NotImplementedError("status: not yet implemented")


def cmd_off(args: argparse.Namespace) -> int:
    """Disable the hooks without deleting config, so `init` can re-enable fast."""
    raise NotImplementedError("off: not yet implemented")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="claude-code-discord",
        description="Surface a Claude Code session into Discord.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="interactive setup")
    p_init.set_defaults(func=cmd_init)

    p_status = sub.add_parser("status", help="show hook wiring + bot reachability")
    p_status.set_defaults(func=cmd_status)

    p_off = sub.add_parser("off", help="disable the hooks (keep config)")
    p_off.set_defaults(func=cmd_off)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except NotImplementedError as e:
        print(f"not yet implemented: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
