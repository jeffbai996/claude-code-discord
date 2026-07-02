"""Tests for the tap-to-approve PreToolUse hook.

The hook's job: read a tool call on stdin, decide whether to auto-allow (read-only
tools), and for everything else post a Discord card and block on the rendezvous
until the owner taps — defaulting to "deny" on timeout, malformed input, or any
internal error. It must ALWAYS emit an explicit PreToolUse decision and exit 0;
exiting silently would fall through to Claude Code's normal (possibly permissive)
flow.
"""
from __future__ import annotations

import io
import json

import pytest

from claude_code_discord.hooks import approve


def _run_hook(payload: dict, monkeypatch) -> dict:
    """Feed `payload` to the hook on stdin, capture its stdout JSON decision."""
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    rc = approve.main()
    assert rc == 0, "hook must always exit 0 with an explicit decision"
    text = out.getvalue().strip()
    return json.loads(text)


def _decision(result: dict) -> str:
    return result["hookSpecificOutput"]["permissionDecision"]


# ── Read-only tools are allowed with no prompt at all ──

def test_readonly_tool_allowed_without_prompt(monkeypatch):
    called = {"create": False, "post": False}
    monkeypatch.setattr(approve, "_create_and_post",
                        lambda *a, **k: called.__setitem__("create", True) or "x")
    result = _run_hook({"tool_name": "Read", "tool_input": {"file_path": "/x"}},
                       monkeypatch)
    assert _decision(result) == "allow"
    assert called["create"] is False, "read-only tool must not open a rendezvous"


# ── The integration fail-closed test: write tool, no tap, must deny ──

def test_write_tool_prompts_and_denies_on_timeout(monkeypatch):
    monkeypatch.setattr(approve, "_create_and_post", lambda *a, **k: "req123")
    # await_decision times out → deny
    monkeypatch.setattr(approve, "_await", lambda req_id: "deny")
    result = _run_hook({"tool_name": "Write", "tool_input": {"file_path": "/x"}},
                       monkeypatch)
    assert _decision(result) == "deny"


def test_write_tool_allows_when_tapped_allow(monkeypatch):
    monkeypatch.setattr(approve, "_create_and_post", lambda *a, **k: "req123")
    monkeypatch.setattr(approve, "_await", lambda req_id: "allow")
    result = _run_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}},
                       monkeypatch)
    assert _decision(result) == "allow"


def test_hook_output_is_valid_pretooluse_json(monkeypatch):
    monkeypatch.setattr(approve, "_create_and_post", lambda *a, **k: "req123")
    monkeypatch.setattr(approve, "_await", lambda req_id: "allow")
    result = _run_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}},
                       monkeypatch)
    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] in ("allow", "deny", "ask")
    assert "permissionDecisionReason" in hso


def test_hook_always_emits_decision_never_silent(monkeypatch):
    # An internal failure (rendezvous raises) must still yield an explicit deny.
    def _boom(*a, **k):
        raise RuntimeError("rendezvous exploded")
    monkeypatch.setattr(approve, "_create_and_post", _boom)
    result = _run_hook({"tool_name": "Write", "tool_input": {"file_path": "/x"}},
                       monkeypatch)
    assert _decision(result) == "deny"


@pytest.mark.parametrize("tool", ["Bash", "Write", "Edit", "MultiEdit"])
def test_dangerous_tools_are_not_treated_as_readonly(tool, monkeypatch):
    # Guard against a policy bug: these must take the prompt path, never auto-allow.
    seen = {"prompted": False}
    monkeypatch.setattr(approve, "_create_and_post",
                        lambda *a, **k: seen.__setitem__("prompted", True) or "r")
    monkeypatch.setattr(approve, "_await", lambda req_id: "deny")
    result = _run_hook({"tool_name": tool, "tool_input": {}}, monkeypatch)
    assert seen["prompted"] is True, f"{tool} must be prompted, never auto-allowed"
    assert _decision(result) == "deny"
