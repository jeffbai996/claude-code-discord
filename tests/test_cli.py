"""Unit tests for claude_code_discord.cli — settings-merge core + init/off round-trip.

The load-bearing invariant is the settings merge: `init` must ADD our hook
commands to ~/.claude/settings.json WITHOUT clobbering any pre-existing hooks or
other settings keys, and must be IDEMPOTENT (re-running never duplicates our
commands). `off` must remove ONLY our commands, leaving co-resident hooks intact.

These are all pure-dict transforms except the round-trip test, which uses a tmp
settings file via the CCD_SETTINGS_PATH env override. No network.
"""
from __future__ import annotations

import json

import pytest

from claude_code_discord import cli


# A pre-existing, unrelated hook the user already had — must survive every op.
_FOREIGN_HOOK = {
    "hooks": [{"type": "command", "command": "some-other-tool --flag"}]
}


# ---------------------------------------------------------------------------
# merge_hooks — add ours, preserve theirs, idempotent, don't touch other keys
# ---------------------------------------------------------------------------


def test_merge_into_empty_settings_adds_our_hooks():
    merged = cli.merge_hooks({}, cli.HOOK_WIRING)
    assert "hooks" in merged
    # Every event we wire is present with at least one of our commands.
    for event, commands in cli.HOOK_WIRING.items():
        found = _all_commands(merged, event)
        for cmd in commands:
            assert cmd in found, f"{cmd} missing under {event}"


def test_merge_preserves_preexisting_unrelated_hook():
    """A hook the user already had under PreToolUse stays AND ours is added."""
    settings = {"hooks": {"PreToolUse": [dict(_FOREIGN_HOOK)]}}
    merged = cli.merge_hooks(settings, cli.HOOK_WIRING)

    commands = _all_commands(merged, "PreToolUse")
    assert "some-other-tool --flag" in commands, "foreign hook must survive"
    for cmd in cli.HOOK_WIRING["PreToolUse"]:
        assert cmd in commands, "our PreToolUse hooks must be added alongside"


def test_merge_is_idempotent():
    """Running merge twice must not duplicate our command entries."""
    once = cli.merge_hooks({}, cli.HOOK_WIRING)
    twice = cli.merge_hooks(once, cli.HOOK_WIRING)

    for event, commands in cli.HOOK_WIRING.items():
        found = _all_commands(twice, event)
        for cmd in commands:
            assert found.count(cmd) == 1, f"{cmd} duplicated under {event}"


def test_merge_leaves_non_hook_keys_untouched():
    """A sibling setting like 'model' must pass through unchanged."""
    settings = {"model": "claude-opus-4", "env": {"FOO": "bar"}}
    merged = cli.merge_hooks(settings, cli.HOOK_WIRING)
    assert merged["model"] == "claude-opus-4"
    assert merged["env"] == {"FOO": "bar"}


def test_merge_does_not_mutate_input():
    """merge_hooks returns a NEW dict — the caller's original is untouched."""
    settings = {"hooks": {"PreToolUse": [dict(_FOREIGN_HOOK)]}}
    snapshot = json.loads(json.dumps(settings))
    cli.merge_hooks(settings, cli.HOOK_WIRING)
    assert settings == snapshot, "input settings must not be mutated in place"


# ---------------------------------------------------------------------------
# unmerge_hooks — remove ONLY ours
# ---------------------------------------------------------------------------


def test_unmerge_removes_only_our_hooks():
    settings = {"hooks": {"PreToolUse": [dict(_FOREIGN_HOOK)]}}
    merged = cli.merge_hooks(settings, cli.HOOK_WIRING)
    unmerged = cli.unmerge_hooks(merged)

    commands = _all_commands(unmerged, "PreToolUse")
    assert "some-other-tool --flag" in commands, "foreign hook must remain"
    for cmd in cli.HOOK_WIRING["PreToolUse"]:
        assert cmd not in commands, "our hooks must be gone after unmerge"


def test_unmerge_round_trip_restores_foreign_only_settings():
    """merge then unmerge returns to a state with only the foreign hook."""
    original = {"hooks": {"PreToolUse": [dict(_FOREIGN_HOOK)]}, "model": "x"}
    round_trip = cli.unmerge_hooks(cli.merge_hooks(original, cli.HOOK_WIRING))
    assert round_trip["model"] == "x"
    assert cli.find_our_hooks(round_trip) == []
    assert "some-other-tool --flag" in _all_commands(round_trip, "PreToolUse")


def test_unmerge_on_clean_settings_is_noop():
    settings = {"model": "x"}
    assert cli.unmerge_hooks(settings) == {"model": "x"}


# ---------------------------------------------------------------------------
# find_our_hooks — status introspection
# ---------------------------------------------------------------------------


def test_find_our_hooks_lists_wired_commands():
    merged = cli.merge_hooks({}, cli.HOOK_WIRING)
    found = cli.find_our_hooks(merged)
    # Every command we wire should be discoverable for `status`.
    for commands in cli.HOOK_WIRING.values():
        for cmd in commands:
            assert cmd in found


def test_find_our_hooks_empty_when_none_wired():
    assert cli.find_our_hooks({"hooks": {"PreToolUse": [dict(_FOREIGN_HOOK)]}}) == []


# ---------------------------------------------------------------------------
# cmd_init / cmd_off round-trip against a tmp CCD_SETTINGS_PATH
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    """Point the CLI's settings path at a tmp file, isolate token dir + network."""
    settings_path = tmp_path / "settings.json"
    monkeypatch.setenv("CCD_SETTINGS_PATH", str(settings_path))
    monkeypatch.setenv("DISCORD_STATE_DIR", str(tmp_path / "discord"))
    # No test ping — stub the network so init/off never touch Discord.
    monkeypatch.setattr(cli._core, "discord_send_message", lambda *a, **k: "msg1")
    return settings_path


def _args(**kw):
    class _NS:
        pass
    ns = _NS()
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_cmd_init_writes_hooks_and_token(tmp_settings):
    rc = cli.cmd_init(_args(token="tok-xyz", channel="123", no_ping=True))
    assert rc == 0
    saved = json.loads(tmp_settings.read_text())
    for commands in cli.HOOK_WIRING.values():
        for cmd in commands:
            assert cmd in _all_commands_all_events(saved)
    # Token was written to the discord state dir .env.
    from claude_code_discord import _core
    token = _core.read_bot_token(_core.detect_discord_state_dir())
    assert token == "tok-xyz"


def test_cmd_init_then_off_restores_foreign_settings(tmp_settings):
    # Seed a pre-existing unrelated hook + a sibling key.
    tmp_settings.write_text(json.dumps(
        {"hooks": {"PreToolUse": [dict(_FOREIGN_HOOK)]}, "model": "keepme"}))

    cli.cmd_init(_args(token="tok", channel="1", no_ping=True))
    after_init = json.loads(tmp_settings.read_text())
    assert "some-other-tool --flag" in _all_commands(after_init, "PreToolUse")
    assert after_init["model"] == "keepme"

    cli.cmd_off(_args())
    after_off = json.loads(tmp_settings.read_text())
    # Our hooks gone, the foreign hook and sibling key survive.
    assert cli.find_our_hooks(after_off) == []
    assert "some-other-tool --flag" in _all_commands(after_off, "PreToolUse")
    assert after_off["model"] == "keepme"


def test_cmd_init_idempotent_on_disk(tmp_settings):
    cli.cmd_init(_args(token="t", channel="1", no_ping=True))
    cli.cmd_init(_args(token="t", channel="1", no_ping=True))
    saved = json.loads(tmp_settings.read_text())
    for commands in cli.HOOK_WIRING.values():
        for cmd in commands:
            assert _all_commands_all_events(saved).count(cmd) == 1


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _all_commands(settings: dict, event: str) -> list[str]:
    """Flat list of every command string wired under one event."""
    out: list[str] = []
    for group in settings.get("hooks", {}).get(event, []):
        for h in group.get("hooks", []):
            cmd = h.get("command")
            if isinstance(cmd, str):
                out.append(cmd)
    return out


def _all_commands_all_events(settings: dict) -> list[str]:
    out: list[str] = []
    for event in settings.get("hooks", {}):
        out.extend(_all_commands(settings, event))
    return out
