"""Bot-side approval poster — the out-of-session half of tap-to-approve.

The PreToolUse `approve` hook runs INSIDE a Claude Code session: it opens a
rendezvous request and then blocks, fail-closed, waiting for a decision. This
module runs in the separate, always-on BOT process. It:

  1. notices a pending rendezvous request (``scan_pending``),
  2. posts a ✅/❌ approval card to Discord (``post_card``),
  3. on the owner's tap, writes the decision back into the rendezvous
     (``handle_reaction``) so the waiting hook unblocks.

Why the split: the hook must stay offline and synchronous (block → deny on
timeout), while the actual Discord I/O — posting the card, seeding the two
reactions, watching for a tap — is inherently async and lives with the bot's
event loop. The rendezvous files are the only contract between the two halves,
so this module never imports the hook and the hook never imports this.

The pure logic (card text, emoji→decision, reaction→write, pending scan) is
factored out so it's unit-testable without a network or an event loop. The one
network function, ``post_card``, is a thin wrapper over already-tested _core
primitives — kept deliberately dumb so there's nothing in it to test offline.
"""
from __future__ import annotations

from typing import Any

from claude_code_discord import _core, _rendezvous

# The two approval reactions. ``handle_reaction`` matches on the normalized
# (VS16-stripped) form so a reaction delivered with or without the U+FE0F
# emoji-presentation selector resolves the same way.
APPROVE_EMOJI = "✅"
DENY_EMOJI = "❌"


def _norm_emoji(emoji: str) -> str:
    """Strip the U+FE0F variation selector so ``✅`` and ``✅️`` compare equal.

    Discord can deliver a reaction with or without the emoji-presentation
    selector depending on the client that added it; matching on the bare
    codepoint keeps the ✅/❌ decision from silently missing.
    """
    return (emoji or "").replace("️", "")


def render_card(req: dict[str, Any]) -> str:
    """Format the approval card body from a rendezvous request dict.

    Shows the tool name and a short, human-readable argument summary so the
    owner knows what they're approving from their phone — for a Bash call that
    means the actual command string, since that's the whole risk surface.

    Defensive against a malformed request: a missing tool_name or a non-dict
    tool_input must still render a card (a crash here would strand the waiting
    hook until it times out to deny — worse than an ugly card).
    """
    tool_name = req.get("tool_name") or "unknown tool"
    tool_input = req.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    summary = _summarize_input(tool_name, tool_input)
    lines = [f"🔐 **Approve `{tool_name}`?**"]
    if summary:
        lines.append(summary)
    lines.append(f"{APPROVE_EMOJI} allow   {DENY_EMOJI} deny")
    return "\n".join(lines)


def _summarize_input(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Best one-line digest of a tool's arguments for the card.

    Bash → the command (the thing being run IS the risk). Write/Edit → the
    target path. Anything else → a compact key=value join, truncated so a huge
    input can't blow past Discord's message limit or bury the ✅/❌ line.
    """
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if isinstance(cmd, str) and cmd:
            return f"```\n{cmd[:300]}\n```"
    path = tool_input.get("file_path") or tool_input.get("path")
    if isinstance(path, str) and path:
        return f"`{path}`"
    if not tool_input:
        return ""
    parts = [f"{k}={v!r}" for k, v in list(tool_input.items())[:4]]
    joined = ", ".join(parts)
    return f"`{joined[:300]}`"


def decision_for_emoji(emoji: str) -> str | None:
    """Map an approval reaction to a rendezvous decision.

    ✅ → "allow", ❌ → "deny", anything else → None (not an approval reaction,
    so it must be ignored, not treated as a deny — a random emoji tap is noise,
    not a decision). VS16-insensitive.
    """
    norm = _norm_emoji(emoji)
    if norm == _norm_emoji(APPROVE_EMOJI):
        return "allow"
    if norm == _norm_emoji(DENY_EMOJI):
        return "deny"
    return None


def handle_reaction(req_id: str, emoji: str) -> bool:
    """Apply an owner reaction to a pending request.

    If the emoji is a real approval reaction, write the decision into the
    rendezvous (unblocking the waiting hook) and return True. If it's anything
    else, do nothing and return False — the hook keeps waiting and will fail
    closed on its own deadline if no valid tap ever lands.
    """
    decision = decision_for_emoji(emoji)
    if decision is None:
        return False
    _rendezvous.write_decision(req_id, decision)
    return True


def scan_pending() -> list[str]:
    """Return the ids of requests awaiting a decision (a ``.req`` with no
    ``.resp`` yet). This is the bot's cheap poll for "is there a new card to
    post?" — an answered request already has its decision written, so it's
    skipped.
    """
    rdv_dir = _rendezvous._rendezvous_dir()
    pending: list[str] = []
    for req_file in rdv_dir.glob("*.req"):
        req_id = req_file.stem
        if not _rendezvous.response_path(req_id).exists():
            pending.append(req_id)
    return pending


def post_card(token: str, chat_id: str, req_id: str) -> str | None:
    """Post the approval card for ``req_id`` and seed the ✅/❌ reactions.

    A thin wrapper over already-tested pieces: read the request, render the
    card, send it via ``_core.discord_send_message``, then PUT ✅ and ❌ via
    ``_core._discord_reaction_call`` so the owner just taps. Returns the posted
    message id (or None if the send failed).

    This is the only network path in the module and is intentionally dumb — all
    the decision logic it depends on is unit-tested; there's nothing here to
    test offline beyond "it calls the tested pieces in order".
    """
    try:
        req = _rendezvous.read_request(req_id)
    except (OSError, ValueError):
        _core.log(f"post_card: unreadable request {req_id}")
        return None

    msg_id = _core.discord_send_message(token, chat_id, render_card(req))
    if not msg_id:
        return None
    # Seed both reactions so approving is a single tap, not "find the emoji".
    _core._discord_reaction_call("PUT", token, chat_id, msg_id, APPROVE_EMOJI)
    _core._discord_reaction_call("PUT", token, chat_id, msg_id, DENY_EMOJI)
    return msg_id
