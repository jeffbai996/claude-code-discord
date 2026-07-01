"""Unit tests for claude_code_discord.hooks.narrate (Feature A: the narration
blockquote).

No network calls — discord_send_message / discord_edit_message /
discord_delete_message are monkeypatched everywhere they'd fire. Tests drive
the pure transcript scanners, the per-channel mode resolver, the watch decision
(with all Discord + state I/O faked), and main()'s stdin/JSON + argparse
contract (which proves the think-updater mode is gone).
"""

from __future__ import annotations

import io
import json

import pytest

from claude_code_discord import _core
from claude_code_discord.hooks import narrate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state_in_tmp(tmp_path, monkeypatch):
    """Redirect the shared _core state paths at a tmp dir.

    narrate reads/writes state via the _core primitives, and _save_last_origin
    imports _core.STATE_DIR, so patching the _core constants isolates all
    on-disk narrate state.
    """
    state_dir = tmp_path / "ccd_state"
    state_path = state_dir / "state.json"
    monkeypatch.setattr(_core, "STATE_DIR", str(state_dir))
    monkeypatch.setattr(_core, "STATE_PATH", str(state_path))
    monkeypatch.setattr(_core, "_LOCK_PATH", str(state_path) + ".lock")
    monkeypatch.setattr(_core, "_lock_depth", 0)
    return state_dir


def _write_transcript(tmp_path, entries: list[dict]) -> str:
    """Write JSONL transcript entries to a tmp file, return its path."""
    path = tmp_path / "transcript.jsonl"
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return str(path)


def _assistant_text(text: str, uuid: str = "u1") -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "message": {"content": [{"type": "text", "text": text}]},
    }


# ---------------------------------------------------------------------------
# _clean_narrate_text — HTML strip, no fence/list handling (that's _blockquote)
# ---------------------------------------------------------------------------


def test_clean_strips_html_tags():
    # Tags vanish; surrounding text (and its spacing) is preserved.
    assert narrate._clean_narrate_text("hello <br>there<span>!</span>") == "hello there!"
    assert narrate._clean_narrate_text("a<span style='x'>b</span>c") == "abc"


def test_clean_empty_returns_empty():
    assert narrate._clean_narrate_text("   ") == ""
    assert narrate._clean_narrate_text("") == ""


# ---------------------------------------------------------------------------
# _new_text_blocks_since — the byte-offset scanner
# ---------------------------------------------------------------------------


def test_new_text_blocks_detects_two_blocks(tmp_path):
    """Two assistant text messages after offset 0 → both blocks, both uuids."""
    path = _write_transcript(tmp_path, [
        _assistant_text("first thought", uuid="a"),
        _assistant_text("second thought", uuid="b"),
    ])
    texts, uuids, offset = narrate._new_text_blocks_since(path, 0)
    assert texts == ["first thought", "second thought"]
    assert uuids == ["a", "b"]
    assert offset > 0


def test_new_text_blocks_none_since_current_offset(tmp_path):
    """No new bytes past the recorded offset → empty result, offset preserved."""
    path = _write_transcript(tmp_path, [_assistant_text("only block", uuid="a")])
    _, _, end = narrate._new_text_blocks_since(path, 0)
    # Re-scan from end-of-file: nothing new.
    texts, uuids, offset = narrate._new_text_blocks_since(path, end)
    assert texts == []
    assert uuids == []
    assert offset == end


def test_new_text_blocks_skips_seen_uuids(tmp_path):
    """A uuid in skip_uuids is not re-emitted even if it's in the read range."""
    path = _write_transcript(tmp_path, [
        _assistant_text("first", uuid="a"),
        _assistant_text("second", uuid="b"),
    ])
    texts, uuids, _ = narrate._new_text_blocks_since(path, 0, skip_uuids=["a"])
    assert texts == ["second"]
    assert uuids == ["b"]


def test_new_text_blocks_ignores_non_assistant(tmp_path):
    """User entries and tool_use-only assistant entries yield no narration."""
    path = _write_transcript(tmp_path, [
        {"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"type": "assistant", "uuid": "t",
         "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}},
    ])
    texts, uuids, _ = narrate._new_text_blocks_since(path, 0)
    assert texts == []
    assert uuids == []


# ---------------------------------------------------------------------------
# _scan_turn_text_blocks — the prereply fallback (turn-bounded, uuid-deduped)
# ---------------------------------------------------------------------------


def test_scan_turn_collects_after_turn_ts(tmp_path):
    """Only assistant text AFTER the user entry matching turn_ts is collected."""
    path = _write_transcript(tmp_path, [
        _assistant_text("pre-turn (ignored)", uuid="old"),
        {"type": "user", "timestamp": "T1",
         "message": {"content": [{"type": "text", "text": "prompt"}]}},
        _assistant_text("in-turn one", uuid="a"),
        _assistant_text("in-turn two", uuid="b"),
    ])
    texts, seen = narrate._scan_turn_text_blocks(path, "T1", [])
    assert texts == ["in-turn one", "in-turn two"]
    assert set(seen) == {"a", "b"}


def test_scan_turn_dedupes_seen(tmp_path):
    """A uuid already in seen_uuids is skipped."""
    path = _write_transcript(tmp_path, [
        {"type": "user", "timestamp": "T1",
         "message": {"content": [{"type": "text", "text": "prompt"}]}},
        _assistant_text("one", uuid="a"),
        _assistant_text("two", uuid="b"),
    ])
    texts, seen = narrate._scan_turn_text_blocks(path, "T1", ["a"])
    assert texts == ["two"]
    assert "b" in seen


# ---------------------------------------------------------------------------
# channel_mode — per-channel narrate config (auto→collapse migration, default)
# ---------------------------------------------------------------------------


def test_channel_mode_default_never_when_no_config(tmp_path, monkeypatch):
    monkeypatch.setattr(narrate, "detect_discord_state_dir", lambda: str(tmp_path))
    assert narrate.channel_mode("123") == "never"


def test_channel_mode_reads_and_migrates_auto(tmp_path, monkeypatch):
    cfg = tmp_path / "narrate.json"
    cfg.write_text(json.dumps({"111": "collapse", "222": "auto", "333": "bogus"}))
    monkeypatch.setattr(narrate, "detect_discord_state_dir", lambda: str(tmp_path))
    assert narrate.channel_mode("111") == "collapse"
    assert narrate.channel_mode("222") == "collapse"  # legacy alias migrated
    assert narrate.channel_mode("333") == "never"     # invalid → never
    assert narrate.channel_mode("444") == "never"     # unknown chat → never


# ---------------------------------------------------------------------------
# count_discord_replies — mid-turn reply detection for segment rotation
# ---------------------------------------------------------------------------


def test_count_discord_replies_bounds_at_user_turn(tmp_path):
    """Counts reply tool_use calls only in the current turn (after last user)."""
    reply = {"type": "assistant", "uuid": "r", "message": {"content": [
        {"type": "tool_use", "name": "mcp__plugin_discord_discord__reply",
         "input": {"chat_id": "9"}}]}}
    path = _write_transcript(tmp_path, [
        reply,  # PRIOR turn's reply — must not be counted
        {"type": "user", "message": {"content": [{"type": "text", "text": "new prompt"}]}},
        reply,  # this turn
        reply,  # this turn
    ])
    assert narrate.count_discord_replies(path) == 2


# ---------------------------------------------------------------------------
# _handle_watch_locked — the watch decision (all network + I/O faked)
# ---------------------------------------------------------------------------


def test_watch_posts_blockquote_of_prose(tmp_path, state_in_tmp, monkeypatch):
    """New prose → one send of a blockquote containing that prose."""
    path = _write_transcript(tmp_path, [
        {"type": "user", "timestamp": "T1",
         "message": {"content": [{"type": "text", "text": "prompt"}]}},
        _assistant_text("weighing the options here", uuid="a"),
    ])

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(narrate, "read_bot_token", lambda sd: "faketoken")
    monkeypatch.setattr(narrate, "detect_discord_state_dir", lambda: str(tmp_path))
    monkeypatch.setattr(narrate, "channel_mode", lambda cid: "collapse")

    def fake_send(token, chat_id, content):
        sent.append((chat_id, content))
        return "msg-1"

    monkeypatch.setattr(narrate, "discord_send_message", fake_send)
    monkeypatch.setattr(narrate, "discord_edit_message",
                        lambda *a, **k: pytest.fail("should not edit on first post"))

    rc = narrate._handle_watch_locked(
        {"transcript_path": path}, path, chat_id="9", mode="collapse",
        turn_key="key-9", turn_ts="T1",
    )
    assert rc == 0
    assert len(sent) == 1
    chat_id, content = sent[0]
    assert chat_id == "9"
    # It's a blockquote of the prose.
    assert content.startswith(narrate.NARRATE_PREFIX_AUTO)
    assert content.rstrip().endswith("weighing the options here") or \
        "weighing the options here" in content
    assert ">>> " in content

    # State persisted the new placeholder id + advanced offset.
    turn = _core._load_state()["key-9"]
    assert turn["placeholder_msg_id"] == "msg-1"
    assert turn["last_byte_offset"] > 0


def test_watch_no_prose_no_placeholder(tmp_path, state_in_tmp, monkeypatch):
    """No new text blocks and not prereply-always → no Discord call at all."""
    path = _write_transcript(tmp_path, [
        {"type": "user", "timestamp": "T1",
         "message": {"content": [{"type": "text", "text": "prompt"}]}},
    ])
    monkeypatch.setattr(narrate, "read_bot_token", lambda sd: "faketoken")
    monkeypatch.setattr(narrate, "detect_discord_state_dir", lambda: str(tmp_path))
    monkeypatch.setattr(narrate, "discord_send_message",
                        lambda *a, **k: pytest.fail("should not send with no prose"))

    rc = narrate._handle_watch_locked(
        {"transcript_path": path}, path, chat_id="9", mode="collapse",
        turn_key="key-9", turn_ts="T1",
    )
    assert rc == 0


# ---------------------------------------------------------------------------
# handle_finalize — collapse deletes, always settles
# ---------------------------------------------------------------------------


def test_finalize_collapse_deletes_placeholder(tmp_path, state_in_tmp, monkeypatch):
    _core._save_state({
        "key-9": {
            "chat_id": "9", "placeholder_msg_id": "m1", "buffer": "some prose",
            "mode": "collapse", "finalized": False, "sealed_placeholders": [],
        }
    })
    deleted: list[str] = []
    monkeypatch.setattr(narrate, "read_bot_token", lambda sd: "faketoken")
    monkeypatch.setattr(narrate, "detect_discord_state_dir", lambda: str(tmp_path))
    monkeypatch.setattr(narrate, "channel_mode", lambda cid: "collapse")
    monkeypatch.setattr(narrate, "discord_delete_message",
                        lambda t, c, m: deleted.append(m) or True)
    monkeypatch.setattr(narrate, "discord_edit_message",
                        lambda *a, **k: pytest.fail("collapse must not edit"))

    narrate.handle_finalize({"transcript_path": ""})
    assert deleted == ["m1"]
    assert _core._load_state()["key-9"]["finalized"] is True


def test_finalize_always_settles_placeholder(tmp_path, state_in_tmp, monkeypatch):
    _core._save_state({
        "key-9": {
            "chat_id": "9", "placeholder_msg_id": "m1", "buffer": "kept prose",
            "mode": "always", "finalized": False, "sealed_placeholders": [],
        }
    })
    edited: list[tuple[str, str]] = []
    monkeypatch.setattr(narrate, "read_bot_token", lambda sd: "faketoken")
    monkeypatch.setattr(narrate, "detect_discord_state_dir", lambda: str(tmp_path))
    monkeypatch.setattr(narrate, "channel_mode", lambda cid: "always")
    monkeypatch.setattr(narrate, "discord_delete_message",
                        lambda *a, **k: pytest.fail("always must not delete kept prose"))
    monkeypatch.setattr(narrate, "discord_edit_message",
                        lambda t, c, m, content: edited.append((m, content)) or True)

    narrate.handle_finalize({"transcript_path": ""})
    assert len(edited) == 1
    m, content = edited[0]
    assert m == "m1"
    assert content.startswith(narrate.NARRATE_PREFIX_ALWAYS)
    assert "kept prose" in content


# ---------------------------------------------------------------------------
# main() — stdin/JSON + argparse contract (proves think-updater is GONE)
# ---------------------------------------------------------------------------


def test_main_bad_json_returns_zero(monkeypatch):
    monkeypatch.setattr("sys.argv", ["narrate", "--mode", "watch"])
    monkeypatch.setattr("sys.stdin", io.StringIO("not json{{{"))
    assert narrate.main() == 0


def test_main_empty_stdin_returns_zero(monkeypatch):
    monkeypatch.setattr("sys.argv", ["narrate", "--mode", "watch"])
    monkeypatch.setattr("sys.stdin", io.StringIO("   "))
    assert narrate.main() == 0


def test_main_watch_empty_payload_no_crash(monkeypatch):
    """--mode watch with an empty JSON object exits 0 without touching Discord."""
    monkeypatch.setattr("sys.argv", ["narrate", "--mode", "watch"])
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert narrate.main() == 0


def test_main_rejects_think_updater_mode(monkeypatch):
    """The think-updater mode must be gone — argparse rejects it (SystemExit)."""
    monkeypatch.setattr("sys.argv", ["narrate", "--mode", "think-updater"])
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    with pytest.raises(SystemExit):
        narrate.main()


def test_no_think_symbols_present():
    """Feature B is fully cut: no think machinery survives in the module."""
    names = dir(narrate)
    assert not any("think" in n.lower() for n in names)
    assert not hasattr(narrate, "run_think_updater")
    assert not hasattr(narrate, "handle_think_prereply")
