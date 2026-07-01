"""Turn-state hook: one Discord reaction tracks a whole turn's progress.

Called by several Claude Code hook events with a different ``--mode`` each.
The single reaction on the inbound Discord message walks through the turn:

    received 👀  →  working (🔧 / 🌐 / 🤖 / 🤔)  →  replied ✅
                                                   ↘ terminal 🖥️ / errored ❌

Modes:
  --mode received    UserPromptSubmit: 👀 on the inbound Discord message.
  --mode working     PreToolUse: category emoji for the tool firing
                     (🔧 edit / 🌐 research / 🤖 delegate / 🤔 thinking).
  --mode replied     PostToolUse on the Discord reply tool: housekeeping only.
  --mode terminal    Stop: final state — ✅ replied / 🖥️ silent / ❌ errored.
  --mode notified    Notification: 🔔 (additive, sits alongside the state).
  --mode interrupt_check  Retired no-op — kept so stale wiring never KeyErrors.

Each transition REMOVES the old transient emoji and ADDS the new one, so at
any moment a message carries exactly one turn-state react. Which predecessors
a state clears is declared in ``TRANSIENT_PREDECESSORS``.

Hooks must be fast and silent: every reaction is fire-and-forget with a short
HTTP timeout, failures are logged and never propagate. State is keyed by the
bare Discord ``message_id`` (a globally-unique snowflake) — a single-user tool
has exactly one bot, so no per-bot partitioning is needed.

Note: interactive terminal sessions fire the Stop hook, so ``--mode terminal``
reliably finalizes the turn. A future headless / batched-Stop deployment may
want a retro-finalize on the next ``received`` to catch turns whose Stop was
skipped; that is intentionally out of scope for this MVP.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# Shared primitives — do NOT re-implement. State dir/path, the reentrant file
# lock, load/save, token resolution, transcript origin parsing all come from
# the core module the narrate + tool-trace hooks also build on.
from claude_code_discord._core import (
    _is_synthetic_hook_feedback,
    _last_user_entry,
    _load_state,
    _save_state,
    _state_lock,
    detect_discord_state_dir,
    log,
    parse_discord_origins,
    read_bot_token,
)


# ---------------------------------------------------------------------------
# Emoji alphabet — state name → unicode reaction
# ---------------------------------------------------------------------------

EMOJI = {
    "received":    "👀",
    "thinking":    "🤔",
    "editing":     "🔧",
    "researching": "🌐",
    "delegating":  "🤖",
    "replied":     "✅",
    "terminal":    "🖥️",
    "errored":     "❌",
    "notified":    "🔔",
    "cancelled":   "🛑",
}


# ---------------------------------------------------------------------------
# Tool → working-state categorization
# ---------------------------------------------------------------------------

# Tool name → working state. First match wins in categorize_pretooluse().
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
RESEARCH_TOOLS = {"WebFetch", "WebSearch"}
DELEGATE_TOOLS = {"Agent", "Task"}
THINKING_TOOLS = {"Bash"}  # slow catch-all → falls back to the "thinking" state

# Discord plugin tool that means "the bot posted a reply".
DISCORD_REPLY_TOOLS = {"mcp__plugin_discord_discord__reply"}

# Discord plugin tool that means "the bot answered with a reaction". When the
# bot's whole response IS a content react, terminal-mode 🖥️ suppresses (the
# react already conveys "I saw it and chose to reply via emoji").
DISCORD_REACT_TOOLS = {"mcp__plugin_discord_discord__react"}


def categorize_pretooluse(tool_name: str) -> str | None:
    """Return the working-state name for a tool, or None if it shouldn't react.

    Priority: edit > delegate > research > thinking. Edit and delegate are more
    specific than the Bash "thinking" catch-all, so they win when both could
    apply. Fast read-only tools (Read, Glob, Grep) map to nothing → no react.
    """
    if tool_name in EDIT_TOOLS:
        return "editing"
    if tool_name in DELEGATE_TOOLS:
        return "delegating"
    if tool_name in RESEARCH_TOOLS:
        return "researching"
    if tool_name in THINKING_TOOLS:
        return "thinking"
    return None


# ---------------------------------------------------------------------------
# Transition table — which transient emojis a new state clears before it lands
# ---------------------------------------------------------------------------

# The transient (in-progress) states. A final state clears ALL of them so the
# message ends with a single ✅ / 🖥️ / ❌.
ALL_TRANSIENTS = ["received", "thinking", "editing", "researching", "delegating"]

TRANSIENT_PREDECESSORS = {
    "received":    ["notified"],
    "thinking":    ["received", "notified"],
    "editing":     ["received", "thinking", "notified"],
    "researching": ["received", "thinking", "notified"],
    "delegating":  ["received", "thinking", "notified"],
    "replied":     ALL_TRANSIENTS + ["notified"],
    "terminal":    ALL_TRANSIENTS + ["notified"],
    "errored":     ALL_TRANSIENTS + ["notified"],
    # 🔔 means the bot is BLOCKED waiting on the user (a permission/elicitation
    # prompt) — that supersedes any in-flight "thinking/working" transient, so
    # clear them. It does NOT clear a FINAL state (✅/🖥️/❌ aren't transients).
    "notified":    ALL_TRANSIENTS,
}

# Discord's reaction route is bucketed at roughly 1 req / 0.25s per bot token.
# _do_react can fire several unreacts back-to-back (a final state clears every
# transient) which would blow past the bucket in one burst; a small fixed gap
# between successive calls keeps a single burst under the bucket.
_REACT_CALL_SPACING_SEC = 0.3

# ❌ the owner taps to interrupt a turn (reserved; the interrupt handler is a
# retired no-op in this build).
CANCEL_EMOJI = "❌"


# ---------------------------------------------------------------------------
# Minimal per-message applied-emoji state (idempotency)
# ---------------------------------------------------------------------------
#
# We track which emojis WE have already PUT on a message so a burst of
# PreToolUse events doesn't re-PUT the same emoji (each re-PUT pings the user).
# State lives under a reserved "_react" sub-dict inside the shared state.json
# so it never collides with the narrate/tool-trace per-turn placeholder keys.
# Keyed by the bare "<chat_id>:<message_id>" — one user means one bot, so no
# per-bot prefix is needed.

STATE_TTL_SEC = 3600  # per-message applied-emoji entries age out after an hour


def _react_key(chat_id: str, msg_id: str) -> str:
    return f"{chat_id}:{msg_id}"


def _react_table(state: dict[str, Any]) -> dict[str, Any]:
    table = state.get("_react")
    if not isinstance(table, dict):
        table = {}
        state["_react"] = table
    return table


def _prune_react_table(table: dict[str, Any]) -> None:
    """Drop applied-emoji entries older than the TTL (best-effort GC)."""
    cutoff = time.time() - STATE_TTL_SEC
    for k in [k for k, v in table.items()
              if not (isinstance(v, dict) and v.get("ts", 0) > cutoff)]:
        del table[k]


def _has_applied(chat_id: str, msg_id: str, emoji: str) -> bool:
    """Have we already PUT this emoji on this message?"""
    table = _react_table(_load_state())
    entry = table.get(_react_key(chat_id, msg_id))
    if not isinstance(entry, dict):
        return False
    return emoji in (entry.get("applied") or [])


def _mark_applied(chat_id: str, msg_id: str, emoji: str) -> None:
    key = _react_key(chat_id, msg_id)
    with _state_lock():
        state = _load_state()
        table = _react_table(state)
        entry = table.get(key)
        if not isinstance(entry, dict):
            entry = {"applied": [], "ts": 0}
        applied = entry.get("applied") or []
        if emoji not in applied:
            applied.append(emoji)
        entry["applied"] = applied
        entry["ts"] = time.time()
        table[key] = entry
        _prune_react_table(table)
        _save_state(state)


def _mark_removed(chat_id: str, msg_id: str, emoji: str) -> None:
    key = _react_key(chat_id, msg_id)
    with _state_lock():
        state = _load_state()
        table = _react_table(state)
        entry = table.get(key)
        if not isinstance(entry, dict):
            return
        applied = entry.get("applied") or []
        if emoji in applied:
            entry["applied"] = [e for e in applied if e != emoji]
            entry["ts"] = time.time()
            table[key] = entry
            _save_state(state)


# ---------------------------------------------------------------------------
# Discord REST — reaction PUT / DELETE (stdlib urllib)
# ---------------------------------------------------------------------------


def _discord_reaction_call(
    method: str, token: str, channel_id: str, message_id: str, emoji: str,
    _retry: bool = True,
) -> bool:
    """PUT or DELETE /channels/{ch}/messages/{msg}/reactions/{emoji}/@me.

    Honors Discord's Retry-After on 429 (single retry). A 404 on DELETE means
    the reaction/message is already gone — idempotent success, not a failure.
    """
    encoded = urllib.parse.quote(emoji)
    url = (
        f"https://discord.com/api/v10/channels/{channel_id}"
        f"/messages/{message_id}/reactions/{encoded}/@me"
    )
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Length": "0",
            "User-Agent": "claude-code-discord/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        if e.code == 429 and _retry:
            retry_after = e.headers.get("Retry-After", "0.5")
            try:
                wait = max(0.1, min(2.0, float(retry_after)))
            except ValueError:
                wait = 0.5
            log(f"react {method} 429 backoff {wait}s for {emoji!r}")
            time.sleep(wait)
            return _discord_reaction_call(method, token, channel_id, message_id,
                                          emoji, _retry=False)
        if e.code == 404:
            # Already gone — idempotent success so callers can update state.
            return True
        log(f"react {method} HTTP {e.code} {channel_id}/{message_id} {emoji!r}")
        return False
    except Exception as e:
        log(f"react {method} failed {channel_id}/{message_id} {emoji!r}: {e}")
        return False


def discord_react(token: str, channel_id: str, message_id: str, emoji: str) -> bool:
    """PUT a reaction. Idempotent: skips the HTTP call if local state shows we
    already PUT this emoji on this message, so repeated PreToolUse events don't
    re-PUT the same emoji and ping the user each time."""
    if _has_applied(channel_id, message_id, emoji):
        return True  # already applied — pretend success
    ok = _discord_reaction_call("PUT", token, channel_id, message_id, emoji)
    if ok:
        _mark_applied(channel_id, message_id, emoji)
    return ok


def discord_unreact(token: str, channel_id: str, message_id: str, emoji: str) -> bool:
    """DELETE the bot's own reaction. Skips the HTTP call if state shows the
    emoji isn't applied. Only marks local state removed when the DELETE actually
    succeeded (or Discord agrees it's gone via 404) — a real failure must NOT be
    marked removed, or the emoji is stranded on Discord while state insists it's
    gone (nothing revisits a message once its turn reaches a terminal state)."""
    if not _has_applied(channel_id, message_id, emoji):
        return True  # already absent — nothing to do
    ok = _discord_reaction_call("DELETE", token, channel_id, message_id, emoji)
    if ok:
        _mark_removed(channel_id, message_id, emoji)
    else:
        log(f"react DELETE gave up, NOT marking removed — {emoji!r} on "
            f"{channel_id}/{message_id} may still be stranded on Discord")
    return ok


def _do_react(chat_id: str, msg_id: str, emoji: str, mode: str) -> int:
    """Transition a message to `mode`: clear its transient predecessors, then
    PUT the new emoji. This is the single remove-old-add-new primitive every
    handler funnels through — the "one reaction tracks the turn" invariant.
    """
    state_dir = detect_discord_state_dir()
    token = read_bot_token(state_dir)
    if not token:
        log(f"no token, skipping {mode} react (state_dir={state_dir})")
        return 0

    # Clear transient predecessors first (fire-and-forget; 404 ok). Space the
    # calls out so a multi-predecessor cleanup doesn't burst past the bucket.
    predecessors = TRANSIENT_PREDECESSORS.get(mode, [])
    for i, prev_mode in enumerate(predecessors):
        if i > 0:
            time.sleep(_REACT_CALL_SPACING_SEC)
        discord_unreact(token, chat_id, msg_id, EMOJI[prev_mode])

    if predecessors:
        time.sleep(_REACT_CALL_SPACING_SEC)
    ok = discord_react(token, chat_id, msg_id, emoji)
    log(f"{mode}={ok} chat={chat_id} msg={msg_id} emoji={emoji}")
    return 0


# ---------------------------------------------------------------------------
# Transcript inspection — what did this turn do?
# ---------------------------------------------------------------------------

# Permission-denial tool_result text signatures.
PERM_DENIED_RE = re.compile(
    r"Permission to use \w+ has been denied|"
    r"permission denied because|"
    r"don't ask mode",
    re.IGNORECASE,
)


def _extract_text(msg_obj: dict) -> str:
    """Pull text from a transcript entry, including Discord reply tool-call args.

    Reads type='text' content blocks AND the `text` argument of Discord reply
    tool calls — a brief acknowledgment can be sent straight through the reply
    tool with no terminal text, and the inbound <channel> tag we key on lives
    only in the user message, so we need the reply arg path to not drop reacts.
    """
    msg = msg_obj.get("message", {})
    content = msg.get("content", [])
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif c.get("type") == "tool_use" and c.get("name", "") in DISCORD_REPLY_TOOLS:
                arg_text = (c.get("input") or {}).get("text", "")
                if isinstance(arg_text, str) and arg_text:
                    parts.append(arg_text)
        return "\n".join(parts)
    return ""


def _walk_current_turn(transcript_path: str) -> tuple[list[dict], list[dict]]:
    """Return (assistant_entries, user_tool_result_entries) for the current turn.

    The current turn is everything after the most recent REAL user message
    (text content, not a tool_result and not synthesized hook feedback). Both
    lists are returned chronological (oldest-first) so callers can index [-1]
    for "the turn's last entry".
    """
    if not transcript_path:
        return [], []
    try:
        with open(transcript_path) as f:
            lines = f.readlines()
    except OSError:
        return [], []

    assistant_entries: list[dict] = []
    user_tool_results: list[dict] = []
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = obj.get("type")
        if t == "user":
            msg = obj.get("message", {})
            content = msg.get("content", [])
            has_text = False
            has_tool_result = False
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict):
                        if c.get("type") == "text":
                            has_text = True
                        elif c.get("type") == "tool_result":
                            has_tool_result = True
            elif isinstance(content, str) and content.strip():
                has_text = True
            # Synthesized Stop-hook feedback looks like text but lives INSIDE
            # the same turn that's mid-retry — walk past it, don't bound here.
            if _is_synthetic_hook_feedback(content):
                continue
            if has_text and not has_tool_result:
                break  # real user message — this bounds the current turn
            # tool_result follow-up inside the turn
            user_tool_results.append(obj)
            continue
        if t == "assistant":
            assistant_entries.append(obj)

    assistant_entries.reverse()
    user_tool_results.reverse()
    return assistant_entries, user_tool_results


def _assistant_called(transcript_path: str, tool_names: set[str]) -> bool:
    """Did this turn's assistant call any tool in `tool_names`?"""
    assistant_entries, _ = _walk_current_turn(transcript_path)
    for obj in assistant_entries:
        content = obj.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for c in content:
            if (isinstance(c, dict) and c.get("type") == "tool_use"
                    and c.get("name", "") in tool_names):
                return True
    return False


def assistant_called_discord_reply(transcript_path: str) -> bool:
    return _assistant_called(transcript_path, DISCORD_REPLY_TOOLS)


def assistant_called_discord_react(transcript_path: str) -> bool:
    return _assistant_called(transcript_path, DISCORD_REACT_TOOLS)


def turn_had_errors(transcript_path: str) -> tuple[bool, bool]:
    """Return (had_any_error, had_permission_denial) for the current turn.

    Scans tool_result blocks for is_error=true and permission-denial text.
    """
    _, user_tool_results = _walk_current_turn(transcript_path)
    any_error = False
    perm_denied = False
    for obj in user_tool_results:
        content = obj.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict) or c.get("type") != "tool_result":
                continue
            if c.get("is_error"):
                any_error = True
            inner = c.get("content", "")
            if isinstance(inner, list):
                inner = " ".join(
                    item.get("text", "") for item in inner
                    if isinstance(item, dict) and item.get("type") == "text"
                )
            if isinstance(inner, str) and PERM_DENIED_RE.search(inner):
                perm_denied = True
                any_error = True
    return any_error, perm_denied


def _turn_origins(payload: dict) -> list[tuple[str, str]]:
    """Resolve the inbound Discord (chat_id, message_id) origins for this turn.

    Prefers the hook payload's own prompt/user_message; falls back to the most
    recent real user entry in the transcript. Returns [] when the turn isn't a
    Discord-origin turn (nothing to react to).
    """
    user_text = payload.get("prompt", "") or payload.get("user_message", "")
    if not user_text:
        last = _last_user_entry(payload.get("transcript_path", ""))
        if last:
            user_text = _extract_text(last)
    return parse_discord_origins(user_text)


def _latest_per_channel(origins: list[tuple[str, str]]) -> dict[str, str]:
    """Collapse origins to one message per channel — the newest (document order
    is chronological, so a later tag overwrites an earlier one). A burst of N
    piled-up inbound messages then gets ONE react on the newest, not N."""
    latest: dict[str, str] = {}
    for chat_id, msg_id in origins:
        latest[chat_id] = msg_id
    return latest


# ---------------------------------------------------------------------------
# Mode handlers
# ---------------------------------------------------------------------------


def handle_received(payload: dict) -> int:
    """UserPromptSubmit: 👀 on the inbound message (newest per channel)."""
    origins = _turn_origins(payload)
    if not origins:
        return 0
    for chat_id, msg_id in _latest_per_channel(origins).items():
        _do_react(chat_id, msg_id, EMOJI["received"], "received")
    return 0


def handle_working(payload: dict) -> int:
    """PreToolUse: emit the category emoji for the tool firing.

    Edit → 🔧, research → 🌐, delegate → 🤖, Bash → 🤔. Fast read-only tools
    (Read/Glob/Grep) categorize to None → no working react (but the turn is
    still in progress). One emoji per channel on its newest inbound message.
    """
    state = categorize_pretooluse(payload.get("tool_name", ""))
    if state is None:
        return 0
    origins = _turn_origins(payload)
    if not origins:
        return 0
    for chat_id, msg_id in _latest_per_channel(origins).items():
        _do_react(chat_id, msg_id, EMOJI[state], state)
    return 0


def handle_replied(payload: dict) -> int:
    """PostToolUse on the Discord reply tool: housekeeping only.

    Does NOT stamp ✅ — a reply can be INTERMEDIATE (a status message posted
    between two tasks in the same turn). ✅ means the whole TURN is done, so
    it's stamped only at turn-end by handle_terminal. This handler is a no-op
    guard that just confirms the reply tool fired.
    """
    if payload.get("tool_name", "") not in DISCORD_REPLY_TOOLS:
        return 0
    return 0


def handle_terminal(payload: dict) -> int:
    """Stop: emit the final-state emoji.

    Priority (most specific first):
      ❌ errored   — a tool errored this turn and no reply was sent
      ✅ replied   — a Discord reply was sent
      (suppress)   — only a content react was sent (the react IS the response)
      🖥️ terminal  — Discord-origin turn but no reply / react sent
    """
    transcript = payload.get("transcript_path", "")
    origins = _turn_origins(payload)
    if not origins:
        return 0

    had_error, _perm_denied = turn_had_errors(transcript)
    replied = assistant_called_discord_reply(transcript)
    reacted = assistant_called_discord_react(transcript)

    if had_error and not replied:
        for chat_id, msg_id in origins:
            _do_react(chat_id, msg_id, EMOJI["errored"], "errored")
        return 0
    if replied:
        for chat_id, msg_id in origins:
            _do_react(chat_id, msg_id, EMOJI["replied"], "replied")
        return 0
    if reacted:
        # A content react IS the response — suppress 🖥️, just clear transients.
        token = read_bot_token(detect_discord_state_dir())
        if token:
            for chat_id, msg_id in origins:
                for prev_mode in ALL_TRANSIENTS + ["notified"]:
                    discord_unreact(token, chat_id, msg_id, EMOJI[prev_mode])
        return 0

    # Silent turn — stamp 🖥️ (newest per channel).
    for chat_id, msg_id in _latest_per_channel(origins).items():
        _do_react(chat_id, msg_id, EMOJI["terminal"], "terminal")
    return 0


def handle_notified(payload: dict) -> int:
    """Notification: 🔔 on the latest Discord-origin message (additive)."""
    origins = _turn_origins(payload)
    if not origins:
        return 0
    chat_id, msg_id = origins[-1]  # document-order last == newest
    return _do_react(chat_id, msg_id, EMOJI["notified"], "notified")


def handle_interrupt_check(payload: dict) -> int:
    """Retired no-op. Kept registered so a stale ``--mode interrupt_check``
    call from old wiring dispatches cleanly instead of KeyError-ing."""
    return 0


HANDLERS = {
    "received":        handle_received,
    "working":         handle_working,
    "replied":         handle_replied,
    "terminal":        handle_terminal,
    "notified":        handle_notified,
    "interrupt_check": handle_interrupt_check,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=list(HANDLERS.keys()))
    args = parser.parse_args()

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        log(f"bad JSON input mode={args.mode}: {raw[:200]!r}")
        return 0

    try:
        return HANDLERS[args.mode](payload)
    except Exception:
        log(f"handler crash mode={args.mode}:\n{traceback.format_exc()}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
