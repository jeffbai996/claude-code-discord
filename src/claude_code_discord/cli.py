"""Command-line entry point: `claude-code-discord <command>`.

Commands:
  init      write the Discord token + merge our hooks into settings.json, ping
  status    show which of our hooks are wired and whether the bot can reach out
  off       remove our hooks from settings.json (leaves the token in place)

The load-bearing logic is the settings MERGE. ~/.claude/settings.json is the
user's file — it may already carry unrelated hooks and other keys. So the merge
must ADD our hook commands without clobbering anything, and be IDEMPOTENT
(re-running `init` never duplicates our commands). `off` is its inverse: strip
ONLY our commands, touch nothing else. Those three transforms (merge / unmerge /
find) are pure dict→dict and are unit-tested; the surrounding I/O (read token,
write settings, test-ping) is thin glue.

The settings.json path is ~/.claude/settings.json by default, overridable with
CCD_SETTINGS_PATH so tests point it at a tmp file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from claude_code_discord import _core

# ---------------------------------------------------------------------------
# The hook wiring — Claude Code hook event → the console-script commands we add
# ---------------------------------------------------------------------------
#
# These command strings are the console scripts declared in pyproject
# [project.scripts]. They all share the "claude-code-discord-" prefix, which is
# also how unmerge/find IDENTIFY our entries — anything under that prefix is
# ours, anything else is the user's and is left alone.

HOOK_CMD_PREFIX = "claude-code-discord-"

HOOK_WIRING: dict[str, list[str]] = {
    "PreToolUse": [
        "claude-code-discord-turn-state --mode working",
        "claude-code-discord-approve",
    ],
    "PostToolUse": [
        "claude-code-discord-tool-trace",
    ],
    "PostToolUseFailure": [
        "claude-code-discord-tool-trace --failure",
    ],
    "UserPromptSubmit": [
        "claude-code-discord-turn-state --mode received",
        "claude-code-discord-narrate --mode watch",
    ],
    "Stop": [
        "claude-code-discord-turn-state --mode terminal",
        "claude-code-discord-narrate --mode finalize",
    ],
}


# ---------------------------------------------------------------------------
# Pure settings transforms (unit-tested)
# ---------------------------------------------------------------------------


def _is_ours(command: str) -> bool:
    """Is this a command WE wired? Matched on the console-script prefix so we
    only ever add/remove our own entries and never the user's other hooks."""
    return isinstance(command, str) and command.strip().startswith(HOOK_CMD_PREFIX)


def _clone(settings: dict) -> dict:
    """Deep-copy via JSON so a transform can't mutate the caller's dict.

    Settings are plain JSON (no cycles, no non-serializable values), so a
    json round-trip is a cheap, dependency-free deep copy — safer than tracking
    which nested lists/dicts we alias.
    """
    return json.loads(json.dumps(settings)) if settings else {}


def merge_hooks(settings: dict, entries: dict[str, list[str]]) -> dict:
    """Return a NEW settings dict with our hook commands added under each event.

    Preserves every existing hook group and every non-hooks key. Idempotent: a
    command we already wired is not re-added, so running this twice yields the
    same result as running it once. Our commands land in their OWN hook group
    per event (never merged into a user's group), so `off` can lift them cleanly.
    """
    out = _clone(settings)
    hooks = out.setdefault("hooks", {})

    for event, commands in entries.items():
        event_groups = hooks.setdefault(event, [])
        existing = _commands_in_event(event_groups)
        to_add = [c for c in commands if c not in existing]
        if to_add:
            event_groups.append({
                "hooks": [{"type": "command", "command": c} for c in to_add]
            })
    return out


def unmerge_hooks(settings: dict) -> dict:
    """Return a NEW settings dict with ONLY our hook commands removed.

    Every foreign hook and every non-hooks key is left exactly as-is. A hook
    group is dropped only when removing our commands empties it; an event key is
    dropped only when it has no groups left — so we don't leave empty scaffolding
    behind, but we also never touch a group that holds a user's command.
    """
    out = _clone(settings)
    hooks = out.get("hooks")
    if not isinstance(hooks, dict):
        return out

    for event in list(hooks.keys()):
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            continue
        new_groups = []
        for group in groups:
            kept = [h for h in group.get("hooks", [])
                    if not _is_ours(h.get("command", ""))]
            if kept:
                new_group = dict(group)
                new_group["hooks"] = kept
                new_groups.append(new_group)
        if new_groups:
            hooks[event] = new_groups
        else:
            del hooks[event]
    if not hooks:
        del out["hooks"]
    return out


def find_our_hooks(settings: dict) -> list[str]:
    """List the command strings WE wired that are currently present — for
    `status`. Order follows the settings file's own event/group order."""
    found: list[str] = []
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return found
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            for h in group.get("hooks", []):
                cmd = h.get("command", "")
                if _is_ours(cmd):
                    found.append(cmd)
    return found


def _commands_in_event(event_groups: list) -> set[str]:
    """All command strings already present under one event (any group)."""
    present: set[str] = set()
    for group in event_groups:
        if not isinstance(group, dict):
            continue
        for h in group.get("hooks", []):
            cmd = h.get("command")
            if isinstance(cmd, str):
                present.add(cmd)
    return present


# ---------------------------------------------------------------------------
# Settings file I/O (thin glue)
# ---------------------------------------------------------------------------


def _settings_path() -> str:
    """~/.claude/settings.json, overridable with CCD_SETTINGS_PATH (for tests)."""
    override = os.environ.get("CCD_SETTINGS_PATH", "")
    if override:
        return os.path.expanduser(override)
    return os.path.expanduser("~/.claude/settings.json")


def _load_settings() -> dict:
    """Read the settings file, returning {} if it's absent or unparseable.

    A missing file is normal on first `init` (we create it). A parse error is
    surfaced by returning {} so we don't crash — but note the merge that follows
    would then write a fresh file, so a corrupt settings.json is effectively
    reset; that's an acceptable failure mode for an opt-in dev tool.
    """
    path = _settings_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_settings(settings: dict) -> None:
    """Write settings back as pretty JSON (atomic via tmp + rename)."""
    path = _settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    os.rename(tmp, path)


def _write_token(state_dir: str, token: str) -> None:
    """Persist the bot token to <discord_state_dir>/.env as DISCORD_BOT_TOKEN.

    Rewrites any existing DISCORD_BOT_TOKEN line and preserves other lines, so
    re-running init just updates the token without stomping sibling config.
    """
    os.makedirs(state_dir, exist_ok=True)
    env_path = os.path.join(state_dir, ".env")
    line = f'DISCORD_BOT_TOKEN="{token}"'
    lines: list[str] = []
    replaced = False
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for raw in f:
                if raw.strip().startswith("DISCORD_BOT_TOKEN="):
                    lines.append(line)
                    replaced = True
                else:
                    lines.append(raw.rstrip("\n"))
    if not replaced:
        lines.append(line)
    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    """Write the token, merge our hooks into settings.json, optional test ping.

    Token + channel come from --token/--channel flags, else an interactive
    prompt. The merge NEVER clobbers existing settings — see merge_hooks. A test
    ping (skippable with --no-ping) confirms the bot can actually post before we
    call it configured.
    """
    token = getattr(args, "token", None) or _prompt("Discord bot token: ")
    channel = getattr(args, "channel", None) or _prompt("Channel id: ")
    if not token:
        print("no token provided; aborting", file=sys.stderr)
        return 1

    state_dir = _core.detect_discord_state_dir()
    _write_token(state_dir, token)

    settings = _load_settings()
    _save_settings(merge_hooks(settings, HOOK_WIRING))

    if not getattr(args, "no_ping", False) and channel:
        msg_id = _core.discord_send_message(
            token, channel, "✅ claude-code-discord wired up — you'll see me here.")
        if msg_id:
            print(f"test ping sent to channel {channel}")
        else:
            print("hooks wired, but the test ping failed — check token/channel",
                  file=sys.stderr)
    print(f"wired {len(find_our_hooks(_load_settings()))} hooks into "
          f"{_settings_path()}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """List our wired hooks and check whether the bot token is present."""
    settings = _load_settings()
    ours = find_our_hooks(settings)
    if ours:
        print(f"{len(ours)} claude-code-discord hooks wired in {_settings_path()}:")
        for cmd in ours:
            print(f"  • {cmd}")
    else:
        print(f"no claude-code-discord hooks wired in {_settings_path()}")

    state_dir = _core.detect_discord_state_dir()
    token = _core.read_bot_token(state_dir)
    print(f"bot token: {'present' if token else 'MISSING'} ({state_dir})")
    return 0


def cmd_off(args: argparse.Namespace) -> int:
    """Remove our hooks from settings.json (leaves the token so init re-enables
    fast). Foreign hooks and other settings keys are untouched — see unmerge."""
    settings = _load_settings()
    removed = find_our_hooks(settings)
    _save_settings(unmerge_hooks(settings))
    print(f"removed {len(removed)} claude-code-discord hooks from {_settings_path()}")
    return 0


def _prompt(label: str) -> str:
    """Interactive prompt (only reached when a flag wasn't supplied)."""
    try:
        return input(label).strip()
    except EOFError:
        return ""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="claude-code-discord",
        description="Surface a Claude Code session into Discord.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="write token + merge hooks into settings")
    p_init.add_argument("--token", help="Discord bot token (else prompt)")
    p_init.add_argument("--channel", help="Discord channel id for the test ping")
    p_init.add_argument("--no-ping", action="store_true",
                        help="skip the test ping")
    p_init.set_defaults(func=cmd_init)

    p_status = sub.add_parser("status", help="show hook wiring + bot reachability")
    p_status.set_defaults(func=cmd_status)

    p_off = sub.add_parser("off", help="remove our hooks (keep the token)")
    p_off.set_defaults(func=cmd_off)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
