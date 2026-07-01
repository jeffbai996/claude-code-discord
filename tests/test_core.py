"""Unit tests for claude_code_discord._core.

No network calls — discord_send_message / discord_edit_message are NOT
exercised here (they'd hit Discord's REST API; integration-tested elsewhere).
State/token/path tests use tmp_path; env-dependent behavior uses monkeypatch.
"""

from __future__ import annotations

import os

import pytest

from claude_code_discord import _core


@pytest.fixture
def state_in_tmp(tmp_path, monkeypatch):
    """Redirect the module-level state paths at a tmp dir.

    STATE_DIR / STATE_PATH / _LOCK_PATH are resolved once at import, so tests
    must patch the constants (not just the env var) to isolate on-disk state.
    """
    state_dir = tmp_path / "ccd_state"
    state_path = state_dir / "state.json"
    monkeypatch.setattr(_core, "STATE_DIR", str(state_dir))
    monkeypatch.setattr(_core, "STATE_PATH", str(state_path))
    monkeypatch.setattr(_core, "_LOCK_PATH", str(state_path) + ".lock")
    # Reset the reentrancy counter so a prior test can't leak lock depth.
    monkeypatch.setattr(_core, "_lock_depth", 0)
    return state_dir


def test_state_round_trip(state_in_tmp):
    """_save_state then _load_state returns an identical dict."""
    original = {
        "1234567890": {
            "chat_id": "999",
            "placeholder_msg_id": "abc",
            "buffer": "hello world",
            "mode": "collapse",
            "finalized": False,
        }
    }
    _core._save_state(original)
    loaded = _core._load_state()
    assert loaded == original


def test_state_lock_sequential_no_deadlock_and_cleanup(state_in_tmp):
    """Two sequential acquisitions don't deadlock; depth returns to 0 each time.

    Reentrant acquisition (nested `with`) must also pass through without
    self-deadlocking on the same fd.
    """
    # First acquisition.
    with _core._state_lock():
        assert _core._lock_depth == 1
    assert _core._lock_depth == 0

    # Second, independent acquisition — proves the first released cleanly.
    with _core._state_lock():
        assert _core._lock_depth == 1
        # Nested (reentrant) acquire must not block on the fd this process holds.
        with _core._state_lock():
            assert _core._lock_depth == 2
        assert _core._lock_depth == 1
    assert _core._lock_depth == 0

    # The lock file is created on first use and left in place (an empty
    # advisory lock file), but the state dir exists and nothing is wedged.
    assert os.path.isdir(state_in_tmp)


def test_blockquote_golden_and_list_indent():
    """Known input → known blockquoted output; list lines get NBSP indent."""
    out = _core._blockquote("plain line\n- bullet one\n1. numbered")
    lines = out.split("\n")
    # Whole block is prefixed with the `>>>` blockquote marker.
    assert lines[0].startswith(">>> ")
    assert lines[0] == ">>> plain line"
    # Bullet + numbered list lines are indented with two base NBSPs.
    nbsp = _core._NBSP
    assert lines[1] == nbsp * 2 + "- bullet one"
    assert lines[2] == nbsp * 2 + "1. numbered"


def test_blockquote_neutralizes_fences():
    """Triple-backtick runs are replaced with the ZWSP-spliced safe form so
    Discord's fence regex never fires inside the blockquote."""
    out = _core._blockquote("before ``` after")
    assert "```" not in out
    assert _core._FENCE_SAFE in out


def test_read_bot_token_present_and_absent(tmp_path):
    """Reads DISCORD_BOT_TOKEN from a temp .env; returns None when absent."""
    state_dir = tmp_path / "discord"
    state_dir.mkdir()
    env_file = state_dir / ".env"
    env_file.write_text('DISCORD_BOT_TOKEN="tok-abc-123"\nOTHER=nope\n')
    assert _core.read_bot_token(str(state_dir)) == "tok-abc-123"

    # Absent .env → None.
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert _core.read_bot_token(str(empty_dir)) is None


def test_detect_discord_state_dir_env_and_fallback(monkeypatch):
    """DISCORD_STATE_DIR wins; else CLAUDE_CONFIG_DIR/channels/discord; else default."""
    monkeypatch.setenv("DISCORD_STATE_DIR", "/explicit/dir")
    assert _core.detect_discord_state_dir() == "/explicit/dir"

    monkeypatch.delenv("DISCORD_STATE_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/cfg")
    assert _core.detect_discord_state_dir() == os.path.join(
        "/cfg", "channels", "discord")

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert _core.detect_discord_state_dir() == os.path.expanduser(
        "~/.claude/channels/discord")
