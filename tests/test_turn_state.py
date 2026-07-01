"""Unit tests for claude_code_discord.hooks.turn_state.

No network calls — the Discord REST layer (_discord_reaction_call / discord_react
/ discord_unreact) is monkeypatched to record calls, so nothing hits the wire.
Tests cover:
  * categorize_pretooluse tool → state mapping,
  * the remove-old-add-new transition sequence funneled through _do_react,
  * the transcript-inspection helpers (reply/react/error detection),
  * main()'s stdin/JSON contract (bad JSON → 0, empty payload → 0).
"""

from __future__ import annotations

import io
import json

import pytest

from claude_code_discord.hooks import turn_state


# ---------------------------------------------------------------------------
# categorize_pretooluse — tool → working-state mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool,expected",
    [
        ("Edit", "editing"),
        ("Write", "editing"),
        ("MultiEdit", "editing"),
        ("WebSearch", "researching"),
        ("WebFetch", "researching"),
        ("Agent", "delegating"),
        ("Task", "delegating"),
        ("Bash", "thinking"),
        ("Read", None),
        ("Glob", None),
        ("Grep", None),
        ("SomeUnknownTool", None),
    ],
)
def test_categorize_pretooluse(tool, expected):
    assert turn_state.categorize_pretooluse(tool) == expected


def test_categorize_edit_beats_thinking_priority():
    """Edit is more specific than the Bash 'thinking' catch-all."""
    assert turn_state.categorize_pretooluse("Edit") == "editing"


# ---------------------------------------------------------------------------
# _do_react — remove-old-add-new transition sequence
# ---------------------------------------------------------------------------


@pytest.fixture
def recorder(monkeypatch):
    """Record every react/unreact call instead of hitting Discord.

    Returns a list of ("PUT"|"DELETE", emoji) tuples in call order. Also stubs
    token resolution so _do_react proceeds past its no-token guard.
    """
    calls: list[tuple[str, str]] = []

    def fake_react(token, chat_id, msg_id, emoji):
        calls.append(("PUT", emoji))
        return True

    def fake_unreact(token, chat_id, msg_id, emoji):
        calls.append(("DELETE", emoji))
        return True

    monkeypatch.setattr(turn_state, "discord_react", fake_react)
    monkeypatch.setattr(turn_state, "discord_unreact", fake_unreact)
    monkeypatch.setattr(turn_state, "read_bot_token", lambda _sd: "fake-token")
    monkeypatch.setattr(turn_state, "detect_discord_state_dir", lambda: "/tmp/x")
    # Kill the inter-call sleep so the transition test runs instantly.
    monkeypatch.setattr(turn_state.time, "sleep", lambda _s: None)
    return calls


def test_do_react_received_puts_eye_and_clears_notified(recorder):
    turn_state._do_react("chat", "msg", turn_state.EMOJI["received"], "received")
    # received clears the 'notified' predecessor, then PUTs 👀.
    assert ("DELETE", turn_state.EMOJI["notified"]) in recorder
    assert recorder[-1] == ("PUT", turn_state.EMOJI["received"])


def test_do_react_working_clears_received_before_adding_wrench(recorder):
    """Received 👀 → editing 🔧: the old 👀 must be removed before the new 🔧."""
    turn_state._do_react("chat", "msg", turn_state.EMOJI["editing"], "editing")
    deletes = [e for m, e in recorder if m == "DELETE"]
    puts = [e for m, e in recorder if m == "PUT"]
    assert turn_state.EMOJI["received"] in deletes  # old eye removed
    assert puts == [turn_state.EMOJI["editing"]]    # exactly one PUT, the wrench
    # And the PUT is the last call (add-after-remove).
    assert recorder[-1] == ("PUT", turn_state.EMOJI["editing"])


def test_do_react_replied_clears_all_transients(recorder):
    """A final ✅ clears every transient (👀🤔🔧🌐🤖) + 🔔 before landing."""
    turn_state._do_react("chat", "msg", turn_state.EMOJI["replied"], "replied")
    deleted = {e for m, e in recorder if m == "DELETE"}
    for state_name in turn_state.ALL_TRANSIENTS:
        assert turn_state.EMOJI[state_name] in deleted
    assert turn_state.EMOJI["notified"] in deleted
    assert recorder[-1] == ("PUT", turn_state.EMOJI["replied"])


def test_do_react_no_token_is_silent_noop(monkeypatch):
    """No bot token → no reaction calls, returns 0 cleanly."""
    called: list = []
    monkeypatch.setattr(turn_state, "read_bot_token", lambda _sd: None)
    monkeypatch.setattr(turn_state, "detect_discord_state_dir", lambda: "/tmp/x")
    monkeypatch.setattr(turn_state, "discord_react",
                        lambda *a: called.append(a))
    monkeypatch.setattr(turn_state, "discord_unreact",
                        lambda *a: called.append(a))
    rc = turn_state._do_react("chat", "msg", turn_state.EMOJI["received"], "received")
    assert rc == 0
    assert called == []


# ---------------------------------------------------------------------------
# handle_received / handle_working — end-to-end react choice
# ---------------------------------------------------------------------------


def test_handle_received_reacts_eye_on_origin(recorder):
    payload = {
        "prompt": '<channel source="discord" chat_id="12345678901234567" '
                  'message_id="98765432109876543">hi',
    }
    turn_state.handle_received(payload)
    assert recorder[-1] == ("PUT", turn_state.EMOJI["received"])


def test_handle_received_non_discord_turn_is_noop(recorder):
    turn_state.handle_received({"prompt": "just a terminal prompt, no tag"})
    assert recorder == []


def test_handle_working_edit_reacts_wrench(recorder):
    payload = {
        "tool_name": "Edit",
        "prompt": '<channel source="discord" chat_id="12345678901234567" '
                  'message_id="98765432109876543">go',
    }
    turn_state.handle_working(payload)
    assert recorder[-1] == ("PUT", turn_state.EMOJI["editing"])


def test_handle_working_fast_tool_no_react(recorder):
    payload = {
        "tool_name": "Read",
        "prompt": '<channel source="discord" chat_id="12345678901234567" '
                  'message_id="98765432109876543">go',
    }
    turn_state.handle_working(payload)
    assert recorder == []


# ---------------------------------------------------------------------------
# Transcript inspection helpers
# ---------------------------------------------------------------------------


def _write_transcript(tmp_path, entries):
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries))
    return str(p)


def test_assistant_called_discord_reply_true(tmp_path):
    entries = [
        {"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use",
             "name": "mcp__plugin_discord_discord__reply",
             "input": {"text": "ok"}},
        ]}},
    ]
    path = _write_transcript(tmp_path, entries)
    assert turn_state.assistant_called_discord_reply(path) is True
    assert turn_state.assistant_called_discord_react(path) is False


def test_turn_had_errors_detects_tool_error(tmp_path):
    entries = [
        {"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": True, "content": "boom"}]}},
    ]
    path = _write_transcript(tmp_path, entries)
    had_error, perm_denied = turn_state.turn_had_errors(path)
    assert had_error is True
    assert perm_denied is False


def test_turn_had_errors_detects_permission_denial(tmp_path):
    entries = [
        {"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": True,
             "content": "Permission to use Edit has been denied because ..."}]}},
    ]
    path = _write_transcript(tmp_path, entries)
    had_error, perm_denied = turn_state.turn_had_errors(path)
    assert had_error is True
    assert perm_denied is True


def test_turn_had_errors_clean_turn(tmp_path):
    entries = [
        {"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "all good"}]}},
    ]
    path = _write_transcript(tmp_path, entries)
    assert turn_state.turn_had_errors(path) == (False, False)


# ---------------------------------------------------------------------------
# handle_terminal — final-state selection
# ---------------------------------------------------------------------------


def test_handle_terminal_replied_stamps_check(recorder, tmp_path, monkeypatch):
    monkeypatch.setattr(turn_state, "assistant_called_discord_reply",
                        lambda _p: True)
    monkeypatch.setattr(turn_state, "assistant_called_discord_react",
                        lambda _p: False)
    monkeypatch.setattr(turn_state, "turn_had_errors", lambda _p: (False, False))
    payload = {
        "transcript_path": "/nope",
        "prompt": '<channel source="discord" chat_id="12345678901234567" '
                  'message_id="98765432109876543">go',
    }
    turn_state.handle_terminal(payload)
    assert recorder[-1] == ("PUT", turn_state.EMOJI["replied"])


def test_handle_terminal_error_stamps_cross(recorder, tmp_path, monkeypatch):
    monkeypatch.setattr(turn_state, "assistant_called_discord_reply",
                        lambda _p: False)
    monkeypatch.setattr(turn_state, "assistant_called_discord_react",
                        lambda _p: False)
    monkeypatch.setattr(turn_state, "turn_had_errors", lambda _p: (True, False))
    payload = {
        "transcript_path": "/nope",
        "prompt": '<channel source="discord" chat_id="12345678901234567" '
                  'message_id="98765432109876543">go',
    }
    turn_state.handle_terminal(payload)
    assert recorder[-1] == ("PUT", turn_state.EMOJI["errored"])


def test_handle_terminal_silent_stamps_monitor(recorder, tmp_path, monkeypatch):
    monkeypatch.setattr(turn_state, "assistant_called_discord_reply",
                        lambda _p: False)
    monkeypatch.setattr(turn_state, "assistant_called_discord_react",
                        lambda _p: False)
    monkeypatch.setattr(turn_state, "turn_had_errors", lambda _p: (False, False))
    payload = {
        "transcript_path": "/nope",
        "prompt": '<channel source="discord" chat_id="12345678901234567" '
                  'message_id="98765432109876543">go',
    }
    turn_state.handle_terminal(payload)
    assert recorder[-1] == ("PUT", turn_state.EMOJI["terminal"])


# ---------------------------------------------------------------------------
# main() stdin / JSON contract
# ---------------------------------------------------------------------------


def test_main_bad_json_returns_zero(monkeypatch):
    monkeypatch.setattr("sys.argv", ["turn_state", "--mode", "received"])
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    assert turn_state.main() == 0


def test_main_empty_payload_valid_mode_returns_zero(monkeypatch):
    monkeypatch.setattr("sys.argv", ["turn_state", "--mode", "terminal"])
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert turn_state.main() == 0


def test_main_interrupt_check_is_noop(monkeypatch):
    monkeypatch.setattr("sys.argv", ["turn_state", "--mode", "interrupt_check"])
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert turn_state.main() == 0
