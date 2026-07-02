"""Tap-to-approve — a PreToolUse hook.

Reads a pending tool call on stdin. Read-only tools are allowed immediately. For
anything that writes or runs commands, it posts a ✅/❌ card to Discord and blocks
on the local rendezvous until the owner taps — then returns the matching
`permissionDecision` to Claude Code.

Safety model — FAIL CLOSED:
  * No tap within our own deadline → deny. Our deadline is deliberately well under
    Claude Code's hook timeout (default 600s), whose timeout is fail-OPEN: if this
    hook were killed at 600s the tool would RUN. So we must decide first.
  * A malformed rendezvous response → deny.
  * ANY internal error → deny, emitted explicitly.
  * The hook ALWAYS prints an explicit PreToolUse decision and exits 0. Exiting
    silently would fall through to Claude Code's normal (possibly permissive)
    permission flow — which for a pre-approved tool could run it.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from claude_code_discord import _core, _rendezvous

# Tools that only read state — never gated, so you're not tapping ✅ on every
# Read. Everything NOT in this set is treated as needing approval (fail safe:
# an unknown/new tool prompts rather than auto-runs).
READONLY_TOOLS = frozenset({
    "Read", "Grep", "Glob", "NotebookRead", "WebFetch", "WebSearch",
    "TodoWrite", "TodoRead",
})

# Our decision deadline — MUST stay well under Claude Code's 600s hook timeout,
# because that timeout is fail-open. Configurable, but never rely on the CC one.
_DEADLINE_S = float(os.environ.get("CCD_APPROVE_TIMEOUT", "120"))


def _is_readonly(tool_name: str) -> bool:
    return tool_name in READONLY_TOOLS


def _create_and_post(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Open a rendezvous request and post the approval card to Discord.

    Returns the request id. (The Discord-post side is wired separately; for the
    hook's own logic and tests, creating the request is the seam.)"""
    req_id = _rendezvous.create_request(tool_name, tool_input)
    _post_card(req_id, tool_name, tool_input)
    return req_id


def _post_card(req_id: str, tool_name: str, tool_input: dict[str, Any]) -> None:
    """Post the ✅/❌ approval card. Best-effort: a failed post must not crash the
    hook (the await will simply time out → deny). Kept as its own seam so the bot
    wiring can evolve without touching the decision logic.

    Fail-closed by omission: this posts ONLY when both a bot token and a target
    channel resolve. The channel comes from the durable last-origin stamp the
    turn-state/narrate hooks write on every Discord-origin turn — the approve
    hook fires mid-turn and carries no <channel> tag of its own, so the stamp is
    how it learns where to post. If the token OR the origin is missing (not a
    Discord-origin turn, or the bot isn't configured), this stays a no-op and
    the await simply times out → deny. Any error is swallowed for the same
    reason: a crash here must not preempt the fail-closed await.
    """
    try:
        from claude_code_discord import bot

        state_dir = _core.detect_discord_state_dir()
        token = _core.read_bot_token(state_dir)
        origin = _core._load_last_origin()
        if not token or origin is None:
            return None  # unconfigured or non-Discord turn → no-op, await denies
        chat_id, _message_id, _turn_ts = origin
        bot.post_card(token, chat_id, req_id)
    except Exception as e:  # noqa: BLE001 — never let a post error break fail-closed
        _core.log(f"_post_card failed (staying fail-closed): {e}")
    return None


def _await(req_id: str) -> str:
    """Block for the owner's decision, failing closed at our deadline."""
    return _rendezvous.await_decision(req_id, deadline_s=_DEADLINE_S)


def _emit(decision: str, reason: str) -> None:
    """Print a PreToolUse decision. Always well-formed so CC honors it."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }))


def _decide(payload: dict[str, Any]) -> tuple[str, str]:
    """Pure decision logic → (decision, reason). Never raises for a normal
    payload; the caller wraps this so ANY unexpected error still fails closed."""
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}

    if _is_readonly(tool_name):
        return "allow", f"{tool_name} is read-only; auto-approved"

    req_id = _create_and_post(tool_name, tool_input)
    decision = _await(req_id)
    _rendezvous.cleanup(req_id)
    if decision == "allow":
        return "allow", "approved via Discord tap"
    return "deny", "denied or no approval received"


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        decision, reason = _decide(payload)
    except Exception as e:  # noqa: BLE001 — fail closed on ANYTHING
        _emit("deny", f"approval hook error (fail-closed): {e}")
        return 0
    _emit(decision, reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
