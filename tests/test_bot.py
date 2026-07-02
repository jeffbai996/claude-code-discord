"""Unit tests for claude_code_discord.bot — the out-of-session approval poster.

The bot runs OUTSIDE a Claude Code session: it posts the ✅/❌ card when the
PreToolUse approve hook opens a rendezvous, and on the owner's tap writes the
decision back into the rendezvous. The PURE logic (card text, emoji→decision,
reaction→write, pending scan) is what these tests cover — the live Discord POST
is a thin wrapper over already-tested pieces and is deliberately not exercised.

No network. handle_reaction is driven against a REAL _rendezvous rooted at a
tmp CCD_STATE_DIR, proving a ✅ makes await_decision return "allow".
"""
from __future__ import annotations

import pytest

from claude_code_discord import _rendezvous, bot


# ---------------------------------------------------------------------------
# render_card — pure card-text formatting
# ---------------------------------------------------------------------------


def test_render_card_shows_tool_name():
    text = bot.render_card({"tool_name": "Write", "tool_input": {"file_path": "/x"}})
    assert "Write" in text


def test_render_card_shows_dangerous_bash_command():
    """The exact command the owner is approving must be visible on the card."""
    text = bot.render_card({
        "tool_name": "Bash",
        "tool_input": {"command": "rm -rf build"},
    })
    assert "Bash" in text
    assert "rm -rf build" in text


def test_render_card_survives_missing_fields():
    """A malformed request dict must not crash the poster — it still renders."""
    text = bot.render_card({})
    assert isinstance(text, str) and text


# ---------------------------------------------------------------------------
# decision_for_emoji — ✅→allow, ❌→deny, else None (VS16-insensitive)
# ---------------------------------------------------------------------------


def test_decision_for_emoji_check_is_allow():
    assert bot.decision_for_emoji("✅") == "allow"


def test_decision_for_emoji_cross_is_deny():
    assert bot.decision_for_emoji("❌") == "deny"


def test_decision_for_emoji_other_is_none():
    assert bot.decision_for_emoji("🤔") is None
    assert bot.decision_for_emoji("") is None


def test_decision_for_emoji_ignores_variation_selector():
    """A trailing U+FE0F (emoji-presentation VS16) must not defeat the match —
    Discord sometimes delivers the reaction with or without it."""
    assert bot.decision_for_emoji("✅️") == "allow"
    assert bot.decision_for_emoji("❌️") == "deny"


# ---------------------------------------------------------------------------
# handle_reaction — ✅ writes allow, ❌ writes deny, other writes nothing
# ---------------------------------------------------------------------------


@pytest.fixture
def rdv(tmp_path, monkeypatch):
    """Root the rendezvous at an isolated tmp dir per test."""
    monkeypatch.setenv("CCD_STATE_DIR", str(tmp_path))
    return _rendezvous


def test_handle_reaction_check_writes_allow(rdv):
    req_id = rdv.create_request("Bash", {"command": "ls"})
    assert bot.handle_reaction(req_id, "✅") is True
    # The decision must be visible to the waiting hook.
    assert rdv.await_decision(req_id, deadline_s=2.0) == "allow"


def test_handle_reaction_cross_writes_deny(rdv):
    req_id = rdv.create_request("Write", {"file_path": "/x"})
    assert bot.handle_reaction(req_id, "❌") is True
    assert rdv.await_decision(req_id, deadline_s=2.0) == "deny"


def test_handle_reaction_random_emoji_writes_nothing(rdv):
    req_id = rdv.create_request("Write", {"file_path": "/x"})
    assert bot.handle_reaction(req_id, "🤔") is False
    # No response file was written → the waiter fails closed on timeout.
    assert not rdv.response_path(req_id).exists()
    assert rdv.await_decision(req_id, deadline_s=0.2) == "deny"


# ---------------------------------------------------------------------------
# scan_pending — list *.req without a matching *.resp
# ---------------------------------------------------------------------------


def test_scan_pending_finds_unanswered_skips_answered(rdv):
    open_id = rdv.create_request("Bash", {"command": "ls"})
    answered_id = rdv.create_request("Write", {"file_path": "/x"})
    rdv.write_decision(answered_id, "allow")  # this one now has a .resp

    pending = bot.scan_pending()
    assert open_id in pending
    assert answered_id not in pending


def test_scan_pending_empty_when_nothing_open(rdv):
    assert bot.scan_pending() == []
