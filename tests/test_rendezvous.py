"""Tests for the local file-based approval rendezvous.

The rendezvous is the request/response channel between the in-session PreToolUse
hook and the out-of-session Discord bot. The fail-closed invariant lives here: a
decision that never arrives (or arrives malformed) MUST resolve to "deny", because
the alternative is running an unapproved tool.
"""
from __future__ import annotations

import pytest

from claude_code_discord import _rendezvous


@pytest.fixture
def rdv(tmp_path, monkeypatch):
    """Point the rendezvous at an isolated temp dir per test."""
    monkeypatch.setenv("CCD_STATE_DIR", str(tmp_path))
    # _rendezvous resolves its dir from the core state dir at call time.
    return _rendezvous


# ── The cornerstone: no response before the deadline must fail CLOSED ──

def test_await_decision_returns_deny_when_no_response_before_deadline(rdv):
    req_id = rdv.create_request("Bash", {"command": "rm -rf build"})
    # No one ever writes a response. A tiny deadline so the test is fast.
    decision = rdv.await_decision(req_id, deadline_s=0.2)
    assert decision == "deny"


def test_await_decision_returns_allow_when_bot_writes_allow(rdv):
    req_id = rdv.create_request("Write", {"file_path": "/x"})
    rdv.write_decision(req_id, "allow")
    decision = rdv.await_decision(req_id, deadline_s=2.0)
    assert decision == "allow"


def test_await_decision_returns_deny_when_bot_writes_deny(rdv):
    req_id = rdv.create_request("Write", {"file_path": "/x"})
    rdv.write_decision(req_id, "deny")
    decision = rdv.await_decision(req_id, deadline_s=2.0)
    assert decision == "deny"


def test_await_decision_deny_wins_on_malformed_response(rdv, tmp_path):
    req_id = rdv.create_request("Bash", {"command": "echo hi"})
    # Simulate the bot writing a garbage response file (partial write / corruption).
    resp_path = rdv.response_path(req_id)
    resp_path.write_text("this is not valid json {{{")
    decision = rdv.await_decision(req_id, deadline_s=2.0)
    assert decision == "deny"


def test_create_request_roundtrip(rdv):
    req_id_a = rdv.create_request("Edit", {"file_path": "/a"})
    req_id_b = rdv.create_request("Edit", {"file_path": "/b"})
    # Unique ids across calls.
    assert req_id_a != req_id_b
    # The request is readable and records the tool name.
    req = rdv.read_request(req_id_a)
    assert req["tool_name"] == "Edit"


def test_cleanup_removes_files(rdv):
    req_id = rdv.create_request("Write", {"file_path": "/x"})
    rdv.write_decision(req_id, "allow")
    assert rdv.request_path(req_id).exists()
    assert rdv.response_path(req_id).exists()
    rdv.cleanup(req_id)
    assert not rdv.request_path(req_id).exists()
    assert not rdv.response_path(req_id).exists()
