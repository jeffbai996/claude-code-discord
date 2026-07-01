"""Surface the assistant's between-tool prose ("narration") to Discord.

This is NOT the LLM's extended-thinking trace — it's the plaintext
assistant text blocks that appear between tool calls in the session
transcript. You see them in the Claude Code terminal; the Discord
sender doesn't, which makes the bot look silent during long tasks. This
hook lifts that prose into a live Discord blockquote.

Invoked in three phases of the hook lifecycle:

  PostToolUse  (--mode watch)    — after each tool call, pick up any new
                                   assistant text blocks that appeared in
                                   the transcript since the last firing and
                                   post / edit a narrate placeholder.
  PreToolUse   (--mode prereply) — same logic as watch, but on the Discord
                                   reply tool's PreToolUse. The timing
                                   guarantees the placeholder lands BEFORE
                                   the reply message, so narration reads as
                                   "the bot was thinking THIS, then said
                                   THAT" instead of racing behind the reply.
  Stop         (--mode finalize) — at turn end, either delete the
                                   placeholder (collapse mode: the real reply
                                   already landed, narration just collapses
                                   away) or convert it to a persistent quoted
                                   header (always mode).

Per-channel mode lives in <discord_state_dir>/narrate.json:
  { "1234567890": "collapse" | "always" | "never" }
(Legacy "auto" is accepted as an alias for "collapse" and migrated on
read.) Default is "never" for every channel — opt-in.

Per-turn state + all the transcript/Discord primitives are shared with the
tool-trace hook via the _core module — so prose + tool traces live as one
cohesive per-turn placeholder set keyed by the inbound Discord message_id.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

# Shared primitives — do NOT re-implement. State dir/path, the reentrant
# file lock, load/save, blockquote rendering, segment sealing, token
# resolution, transcript origin parsing all come from the core module the
# tool-trace + turn-state hooks also build on.
from claude_code_discord._core import (
    DISCORD_LIMIT,
    NARRATE_PREFIX_AUTO,
    _blockquote,
    _byte_offset_after_current_user_turn,
    _discord_request,
    _extract_user_text,
    _get_turn,
    _last_user_entry,
    _load_last_origin,
    _load_state,
    _save_state,
    _seal_segment,
    _state_lock,
    _turn_key_for_message,
    detect_discord_state_dir,
    discord_edit_message,
    discord_send_message,
    log,
    parse_discord_origins,
    read_bot_token,
)

# Always-mode marker stays plain bold — it's a settled header, not a live
# status (which is why watch/prereply use the bold+italic NARRATE_PREFIX_AUTO
# from _core). The word transition ("Narrating…" → "Narration") is what tells
# a reader at a glance whether the block is live or done.
NARRATE_PREFIX_ALWAYS = "💬 **Narration**\n"


# ---------------------------------------------------------------------------
# Buffer splitting for the Discord char cap
# ---------------------------------------------------------------------------


def _fit_split(buf: str, prefix: str) -> tuple[str, str]:
    """Split buf into (head, tail) so prefix + _blockquote(head) fits the cap.

    The split lands on a natural boundary — paragraph, then line, then word —
    because the tail becomes its own blockquote segment and a mid-sentence cut
    reads as truncation (the exact bug this exists to fix). Rendering can
    LENGTHEN text (NBSP list indents, ZWSP fence splices), so the char budget
    is only a first guess; the loop re-verifies against the rendered form.
    Returns ("", buf) only if even a single word can't fit, which can't happen
    at Discord's 2000-char cap.
    """
    if len(prefix + _blockquote(buf)) <= DISCORD_LIMIT:
        return buf, ""
    budget = DISCORD_LIMIT - len(prefix) - 8
    head = buf[:budget]
    for sep in ("\n\n", "\n", " "):
        cut = head.rfind(sep)
        if cut > budget // 2:
            head = head[:cut]
            break
    while head and len(prefix + _blockquote(head)) > DISCORD_LIMIT:
        head = head[: int(len(head) * 0.9)].rstrip()
    return head, buf[len(head):].lstrip()


# ---------------------------------------------------------------------------
# Durable "last resolved origin" — survives a mid-task COMPACTION
# ---------------------------------------------------------------------------
#
# The normal origin lookup reads the transcript for the inbound <channel>
# tag, but a compaction summarizes that tag away, so post-compact the lookup
# finds nothing and narration silently dies for the rest of the task. _core
# owns the read side (_load_last_origin, recency-gated); we own the write
# side here so _resolve_turn_target can stamp every successful resolve.


def _last_origin_path() -> str:
    # Mirror _core's resolution so the read/write sides agree on one path.
    from claude_code_discord._core import STATE_DIR
    return os.path.join(STATE_DIR, "last_origin.json")


def _save_last_origin(chat_id: str, message_id: str, turn_ts: str) -> None:
    """Stamp the freshest successfully-resolved origin (best-effort)."""
    if not chat_id or not message_id:
        return
    from claude_code_discord._core import STATE_DIR
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        path = _last_origin_path()
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({
                "chat_id": chat_id,
                "message_id": message_id,
                "turn_ts": turn_ts,
                "saved_at": time.time(),
            }, f)
        os.replace(tmp, path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Per-channel narrate mode config
# ---------------------------------------------------------------------------


def _narrate_config_path() -> str:
    """<discord_state_dir>/narrate.json — sibling to the plugin's config."""
    return os.path.join(detect_discord_state_dir(), "narrate.json")


def channel_mode(chat_id: str) -> str:
    """Resolve the per-channel narrate mode. Default 'never' (opt-in).

    'auto' is the legacy alias for 'collapse' — accepted for narrate.json
    files that pre-date the rename. Any unrecognized value falls back to
    'never' so a typo can't accidentally spam a channel.
    """
    path = _narrate_config_path()
    if not os.path.exists(path):
        return "never"
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return "never"
    if not isinstance(cfg, dict):
        return "never"
    val = cfg.get(chat_id, "never")
    if val == "auto":
        val = "collapse"
    if val not in ("never", "collapse", "always"):
        log(f"invalid mode {val!r} for chat {chat_id} — defaulting to never")
        return "never"
    return val


# ---------------------------------------------------------------------------
# Discord HTTP — delete (send/edit live in _core)
# ---------------------------------------------------------------------------


def discord_delete_message(token: str, chat_id: str, msg_id: str) -> bool:
    """DELETE /channels/{id}/messages/{id}. Used by collapse-mode finalize."""
    url = f"https://discord.com/api/v10/channels/{chat_id}/messages/{msg_id}"
    return _discord_request("DELETE", url, token) is not None


# ---------------------------------------------------------------------------
# Transcript walkers — pick up new assistant text since last_seen
# ---------------------------------------------------------------------------

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean_narrate_text(text: str) -> str:
    """Strip HTML tags + trim whitespace. Per-text-block sanitation only.

    The model sometimes emits raw HTML in terminal-targeted prose (<br>,
    <span style=...>, etc.) for formatting affordances that render as literal
    tags on Discord. We strip those at the narrate boundary.

    Fence-neutralization and list-marker handling deliberately do NOT happen
    here — they live in _core._blockquote(), which runs on the full
    concatenated buffer. Doing them per-block caused two bugs: a ``` spanning
    two watcher fires got each half neutralized separately, then the
    concatenation re-formed a raw run (the "salad" leak); and a bullet-rewrite
    here collided with the blockquote's list-indent regex. Centralizing both
    in _blockquote fixes the collision and the leak.

    Returns an empty string if the text is empty after stripping — callers
    should skip empty results.
    """
    if not text:
        return ""
    cleaned = _HTML_TAG_RE.sub("", text)
    return cleaned.strip()


def _new_text_blocks_since(
    transcript_path: str, last_byte_offset: int,
    *, skip_uuids: list[str] | None = None,
) -> tuple[list[str], list[str], int]:
    """Read the transcript from last_byte_offset for new assistant text.

    Returns (text_block_contents, assistant_message_uuids, new_byte_offset) in
    document order. Byte-offset tracking (not line index) works because the
    file is append-only JSONL, so offset comparison is trivially monotonic.

    `skip_uuids` are assistant-message uuids already incorporated — skipped
    even if in this read range, because the prereply scan-back can absorb
    blocks physically PAST `last_byte_offset` (eof-snapshot race), and without
    uuid dedup the next read would re-count them.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return [], [], last_byte_offset
    try:
        size = os.path.getsize(transcript_path)
    except OSError:
        return [], [], last_byte_offset
    if size <= last_byte_offset:
        return [], [], last_byte_offset

    skip_set = set(skip_uuids or [])
    new_texts: list[str] = []
    new_uuids: list[str] = []
    try:
        with open(transcript_path) as f:
            f.seek(last_byte_offset)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                uuid = obj.get("uuid")
                if isinstance(uuid, str) and uuid in skip_set:
                    continue
                msg = obj.get("message", {})
                content = msg.get("content", [])
                if not isinstance(content, list):
                    continue
                added_any = False
                for c in content:
                    if (isinstance(c, dict)
                            and c.get("type") == "text"
                            and isinstance(c.get("text"), str)):
                        cleaned = _clean_narrate_text(c["text"])
                        if cleaned:
                            new_texts.append(cleaned)
                            added_any = True
                if added_any and isinstance(uuid, str) and uuid:
                    new_uuids.append(uuid)
    except OSError as e:
        log(f"transcript read failed: {e}")
        return [], [], last_byte_offset

    return new_texts, new_uuids, size


def _scan_turn_text_blocks(
    transcript_path: str, turn_ts: str, seen_uuids: list[str] | None,
) -> tuple[list[str], list[str]]:
    """Robust fallback: scan ALL assistant text blocks for the current turn.

    Prereply safety net for the post-tool race: `_new_text_blocks_since` reads
    from a byte offset the post-tool watch tick already advanced past, so text
    emitted AFTER that advance but BEFORE the reply tool's prereply hook fires
    reads as []. This walks from the turn's user timestamp forward, collecting
    every assistant text block, deduped via each message `uuid` (seen_uuids in
    turn state). Returns (new_text_blocks, updated_seen_uuids). Bounded by turn
    length; re-reads from the turn boundary each prereply call.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return [], list(seen_uuids or [])
    seen = set(seen_uuids or [])
    new_texts: list[str] = []
    new_seen = list(seen_uuids or [])
    in_turn = False
    try:
        with open(transcript_path) as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                t = obj.get("type")
                if t == "user" and not in_turn:
                    ts = obj.get("timestamp") or obj.get("ts") or ""
                    if ts == turn_ts:
                        in_turn = True
                    continue
                if not in_turn:
                    continue
                if t != "assistant":
                    continue
                uuid = obj.get("uuid") or ""
                if uuid in seen:
                    continue
                msg = obj.get("message", {})
                content = msg.get("content", [])
                if not isinstance(content, list):
                    continue
                added = False
                for c in content:
                    if (isinstance(c, dict)
                            and c.get("type") == "text"
                            and isinstance(c.get("text"), str)):
                        cleaned = _clean_narrate_text(c["text"])
                        if cleaned:
                            new_texts.append(cleaned)
                            added = True
                if added and uuid:
                    seen.add(uuid)
                    new_seen.append(uuid)
    except OSError as e:
        log(f"turn scan failed: {e}")
    return new_texts, new_seen


# ---------------------------------------------------------------------------
# Reply-tracking helpers (segment rotation depends on mid-turn reply counting)
# ---------------------------------------------------------------------------

_DISCORD_REPLY_TOOL = "mcp__plugin_discord_discord__reply"


def count_discord_replies(transcript_path: str) -> int:
    """How many times has this turn's assistant called the Discord reply tool?

    Used to detect mid-turn replies and rotate the placeholder so subsequent
    narrate prose lands BELOW each reply instead of being edited invisibly
    into an earlier placeholder above it. "This turn" = every assistant entry
    after the most recent REAL user text entry (a tool_result-only entry
    doesn't bound a turn).
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return 0
    try:
        with open(transcript_path) as f:
            lines = f.readlines()
    except OSError:
        return 0
    count = 0
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = obj.get("type")
        content = obj.get("message", {}).get("content", [])
        if t == "user":
            # A real user text entry bounds the turn; a tool_result-only entry
            # is a tool reply, not a boundary.
            if isinstance(content, list):
                kinds = {c.get("type") for c in content if isinstance(c, dict)}
                if kinds and kinds.issubset({"tool_result"}):
                    continue
            break
        if t != "assistant" or not isinstance(content, list):
            continue
        for c in content:
            if (isinstance(c, dict) and c.get("type") == "tool_use"
                    and c.get("name", "") == _DISCORD_REPLY_TOOL):
                count += 1
    return count


def _last_reply_chat_id(transcript_path: str) -> str | None:
    """Return the chat_id of the most recent Discord reply tool_use.

    Used by watch mode to anchor narration to the channel the bot is actively
    conversing in — important when mid-turn interrupts from other channels
    cause `_last_user_entry` to point at a different chat than the bot is
    actually replying to. The reply tool's chat_id is the ground truth.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return None
    try:
        with open(transcript_path) as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "assistant":
            continue
        content = obj.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for c in content:
            if (isinstance(c, dict) and c.get("type") == "tool_use"
                    and c.get("name") == _DISCORD_REPLY_TOOL):
                cid = (c.get("input") or {}).get("chat_id")
                if isinstance(cid, str) and cid:
                    return cid
    return None


def _last_origin_for_channel(
    transcript_path: str, chat_id: str,
) -> tuple[str, str] | None:
    """Most recent inbound (chat_id, message_id) for a SPECIFIC channel.

    Scans the whole transcript newest→oldest for a user entry whose <channel>
    tag matches chat_id. Needed because under interleave the reply-target
    channel's inbound message may NOT be in the newest user entry — it can be
    several entries back. Returns None if none found.
    """
    if not chat_id or not transcript_path or not os.path.exists(transcript_path):
        return None
    try:
        with open(transcript_path) as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "user":
            continue
        txt = _extract_user_text(obj)
        if not txt:
            continue
        for cid, mid in reversed(parse_discord_origins(txt)):
            if cid == chat_id:
                return (cid, mid)
    return None


def _ts_for_message(transcript_path: str, message_id: str) -> str:
    """Timestamp of the user entry whose <channel> tag carries message_id.

    Used so the byte-offset walker bounds the turn at the CHOSEN origin's user
    message (which may be earlier than the newest entry under interleave), not
    at whatever _last_user_entry returned. Empty string if not found.
    """
    if not message_id or not transcript_path or not os.path.exists(transcript_path):
        return ""
    try:
        with open(transcript_path) as f:
            lines = f.readlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "user":
            continue
        txt = _extract_user_text(obj)
        if not txt:
            continue
        for _cid, mid in parse_discord_origins(txt):
            if mid == message_id:
                return obj.get("timestamp") or obj.get("ts") or ""
    return ""


# ---------------------------------------------------------------------------
# Turn-target resolution
# ---------------------------------------------------------------------------


def _resolve_turn_target(
    payload: dict, transcript_path: str, *, is_prereply: bool,
) -> tuple[str, str, str] | None:
    """Resolve (chat_id, message_id, turn_ts) for the turn this hook acts on.

    Both the target channel and the turn identity come from the SAME inbound
    <channel> tag so routing and keying never disagree. The multi-channel fix
    hinges on anchoring to "the conversation the bot is actually replying to,"
    NOT "the newest message in the transcript" — when two channels interleave,
    `_last_user_entry` flip-flops between them, so routing by it would send
    channel A's narration into channel B.

    The ground truth for "which conversation" is the REPLY TARGET:
      prereply → the about-to-fire reply tool's chat_id (from payload).
      watch    → the most recent reply tool's chat_id in the transcript.
    Fall back to the newest inbound origin, then to the durable last-resolved
    origin (post-compaction), only when no reply target is known.

    Returns None when this isn't a Discord-origin turn (nothing to anchor).
    """
    reply_chat_id: str | None = None
    if is_prereply:
        tool_input = payload.get("tool_input") or {}
        candidate = tool_input.get("chat_id")
        if isinstance(candidate, str) and candidate:
            reply_chat_id = candidate
    else:
        reply_chat_id = _last_reply_chat_id(transcript_path)

    user = _last_user_entry(transcript_path)
    user_text = _extract_user_text(user)
    origins = parse_discord_origins(user_text)
    if not origins:
        # No inbound <channel> tag in the latest user entry — usually a
        # COMPACTION summarized the original tag away mid-task. Fall back to
        # the durable last-resolved origin (recency- and channel-gated) so the
        # turn carries on where it left off instead of going silent.
        revived = _load_last_origin(reply_chat_id)
        if revived is not None:
            log(f"origin revived from durable file: chat={revived[0]} msg={revived[1]}")
            return revived
        return None

    chosen = None
    if reply_chat_id:
        # Prefer the inbound origin matching the reply target's channel, so the
        # turn key (message_id) belongs to the conversation we're in. Check the
        # latest user entry's origins first, then a whole-transcript scan —
        # under interleave the reply target's inbound message may be several
        # entries back, not in the newest one.
        for cid, mid in reversed(origins):
            if cid == reply_chat_id:
                chosen = (cid, mid)
                break
        if chosen is None:
            chosen = _last_origin_for_channel(transcript_path, reply_chat_id)
    if chosen is None:
        chosen = origins[-1]
    chat_id, message_id = chosen
    # The reply target's channel is authoritative for WHERE the placeholder
    # goes, even if no inbound tag matched it — this is what stops
    # cross-channel narration leaks.
    if reply_chat_id:
        chat_id = reply_chat_id

    # turn_ts must come from the user entry that actually carries the CHOSEN
    # message_id — under interleave the chosen origin can be an EARLIER entry
    # than the newest one, and the newest entry's ts would point the byte-offset
    # walker past the chosen turn's prose and narrate nothing.
    turn_ts = _ts_for_message(transcript_path, message_id)
    if not turn_ts:
        turn_ts = (user.get("timestamp") or user.get("ts") or "") if user else ""

    # Stamp this resolved origin so it survives a later compaction. Only the
    # transcript-resolved path saves — a revived origin already came from here.
    _save_last_origin(chat_id, message_id, turn_ts)
    return chat_id, message_id, turn_ts


# ---------------------------------------------------------------------------
# Watch phase — surface new assistant text blocks
# ---------------------------------------------------------------------------


def handle_watch(payload: dict, *, is_prereply: bool = False) -> int:
    """PostToolUse (or reply PreToolUse): surface new text blocks for this turn.

    is_prereply=True is set by the `--mode prereply` dispatch. In that mode we
    ALWAYS lock a placeholder above the upcoming reply, even when no new prose
    has accumulated yet — otherwise text emitted AFTER the reply tool fires
    creates a fresh placeholder that lands BELOW the reply. Empty placeholders
    get cleaned up at finalize.
    """
    transcript_path = payload.get("transcript_path") or ""
    if not transcript_path:
        return 0

    resolved = _resolve_turn_target(
        payload, transcript_path, is_prereply=is_prereply,
    )
    if resolved is None:
        return 0
    chat_id, message_id, turn_ts = resolved

    mode = channel_mode(chat_id)
    if mode == "never":
        return 0

    turn_key = _turn_key_for_message(message_id)

    # Serialize the entire read-modify-write of state.json so concurrent
    # PostToolUse hooks (one per tool call) can't both create a placeholder for
    # the same turn. flock blocks the second invocation until the first commits
    # its placeholder_msg_id back to state.
    with _state_lock():
        return _handle_watch_locked(
            payload, transcript_path, chat_id, mode, turn_key, turn_ts,
            is_prereply=is_prereply,
        )


def _suppress_post_reply_placeholder(
    mode: str, has_live_placeholder: bool,
    current_reply_count: int, replies_at_create: int,
) -> bool:
    """Whether to SKIP creating a narrate placeholder this tick.

    In `collapse` mode a placeholder created AFTER a reply has landed sits
    BELOW the output it narrates — and collapse deletes it at finalize anyway,
    so it's pure flicker. Skip it (the text isn't lost; collapse discards it
    regardless). `always` mode is NOT suppressed — it deliberately keeps the
    below-reply placeholder as a settled Narration header. A live placeholder
    is already correctly positioned above its reply, so never suppress that.
    """
    return (
        mode == "collapse"
        and not has_live_placeholder
        and current_reply_count > replies_at_create
    )


def _handle_watch_locked(
    payload: dict, transcript_path: str, chat_id: str, mode: str,
    turn_key: str, turn_ts: str, *, is_prereply: bool = False,
) -> int:
    """Body of handle_watch executed under _state_lock()."""
    state = _load_state()
    turn = _get_turn(state, turn_key)
    if turn is None:
        # First firing for this turn — initialize the offset at the byte
        # position just after the inbound user message. NEVER 0 — that would
        # replay the entire session history.
        initial_offset = _byte_offset_after_current_user_turn(
            transcript_path, turn_ts
        )
        turn = {
            "chat_id": chat_id,
            # Current segment's live placeholder. Reset to None each time a
            # mid-turn reply lands (segment rotation).
            "placeholder_msg_id": None,
            "last_byte_offset": initial_offset,
            "buffer": "",
            "mode": mode,
            "finalized": False,
            # How many discord-reply tool calls had landed when the current
            # segment's placeholder was created. When the live count exceeds
            # this, a reply landed mid-segment and we seal + start a new one.
            "replies_at_create": 0,
            # Trail of sealed placeholders within this turn — collapse deletes
            # them all at Stop, always keeps them as Narration headers.
            "sealed_placeholders": [],
            # Assistant-message UUIDs whose text has already been incorporated
            # into the buffer. Used by prereply's scan-back to dedupe against
            # text the byte-offset path already captured.
            "seen_text_uuids": [],
        }
    if turn.get("finalized"):
        return 0

    # Pass seen_text_uuids into the reader so we skip already-incorporated
    # messages — handles the race where a prior prereply's scan-back captured a
    # block physically PAST `last_byte_offset`; without uuid dedup the next
    # watch would re-read the same block and double the buffer.
    new_texts, new_uuids, new_offset = _new_text_blocks_since(
        transcript_path, turn.get("last_byte_offset", 0),
        skip_uuids=turn.get("seen_text_uuids", []),
    )

    # In prereply mode ALSO scan back through the current turn and pick up
    # text the byte-offset path missed (emitted between a tool's PostToolUse
    # and the reply tool's PreToolUse — the offset advanced past the tool
    # record but the post-tool text may not have flushed yet). Deduped by uuid.
    if is_prereply:
        scan_texts, scan_seen = _scan_turn_text_blocks(
            transcript_path, turn_ts,
            list(turn.get("seen_text_uuids", [])) + new_uuids,
        )
        if scan_texts:
            new_texts = new_texts + scan_texts
            log(f"prereply scan-back recovered {len(scan_texts)} block(s) for {turn_key}")
        new_uuids = [u for u in scan_seen
                     if u not in turn.get("seen_text_uuids", [])]

    # Segment rotation: if a reply landed since the current segment's
    # placeholder was created, seal the current placeholder (so it stays
    # positioned above its triggering reply) and reset state for a new segment.
    current_reply_count = count_discord_replies(transcript_path)
    if (
        turn.get("placeholder_msg_id")
        and current_reply_count > turn.get("replies_at_create", 0)
    ):
        _seal_segment(turn)
        log(f"rotated narrate segment for {turn_key} (reply count {current_reply_count})")

    # Collapse-mode post-reply guard: once a reply has landed this turn with no
    # live placeholder, any new placeholder would post BELOW the reply and get
    # deleted at finalize anyway — pure flicker. Advance offset + seen-uuids so
    # we don't re-scan this text next tick, then exit. always-mode is NOT
    # suppressed — it keeps below-reply placeholders as Narration headers.
    if _suppress_post_reply_placeholder(
        mode, bool(turn.get("placeholder_msg_id")),
        current_reply_count, turn.get("replies_at_create", 0),
    ):
        turn["last_byte_offset"] = new_offset
        _merge_seen_uuids(turn, new_uuids)
        state[turn_key] = turn
        _save_state(state)
        return 0

    if not new_texts and turn.get("placeholder_msg_id"):
        # Nothing new to add — keep state coherent and exit.
        turn["last_byte_offset"] = new_offset
        _merge_seen_uuids(turn, new_uuids)
        state[turn_key] = turn
        _save_state(state)
        return 0
    if not new_texts and not (is_prereply and mode == "always"):
        # Nothing yet — don't create an empty placeholder.
        #
        # Exception: prereply in `always` mode posts the placeholder NOW (above
        # the upcoming reply) so any prose emitted AFTER the reply edits THIS
        # placeholder instead of creating a new one below it. In `collapse`
        # mode the placeholder gets deleted at finalize regardless of position,
        # so the position-locking would be pure visual noise — skip the stub.
        return 0

    state_dir = detect_discord_state_dir()
    token = read_bot_token(state_dir)
    if not token:
        log(f"no bot token at {state_dir} — skipping narrate")
        return 0

    # Append new text to buffer with paragraph breaks between blocks.
    incoming = "\n\n".join(new_texts) if new_texts else ""
    candidate_buffer = (turn["buffer"] + "\n\n" + incoming
                        if turn["buffer"] and incoming else (
                            turn["buffer"] or incoming
                        ))
    candidate_content = NARRATE_PREFIX_AUTO + _blockquote(candidate_buffer or "…")

    # Length guard: if appending this batch would push the placeholder over
    # Discord's 2000-char cap, the edit would silently truncate mid-blockquote
    # — raw text leaks past the quote bar. Auto-rotate: seal the current
    # segment (sized as it was before this batch), reset, start a fresh
    # placeholder with just the new text. Same mechanism as reply-triggered
    # rotation, driven by length instead of a reply landing.
    if (
        turn.get("placeholder_msg_id")
        and len(candidate_content) > DISCORD_LIMIT
    ):
        _seal_segment(turn)
        log(f"rotated narrate segment for {turn_key} (length cap)")
        turn["buffer"] = incoming
    else:
        turn["buffer"] = candidate_buffer
    turn["last_byte_offset"] = new_offset
    _merge_seen_uuids(turn, new_uuids)

    # A single batch can itself exceed the cap — one long uninterrupted block,
    # most commonly the FIRST batch of a turn, which skips the rotation guard
    # above (no placeholder exists yet) and would get hard-sliced mid-sentence
    # by the send-side cap. Carve the buffer into cap-sized segments at natural
    # boundaries: render each head into the current placeholder (creating one
    # if needed), seal it, carry the remainder forward as the live segment.
    while len(NARRATE_PREFIX_AUTO + _blockquote(turn["buffer"] or "…")) > DISCORD_LIMIT:
        head, tail = _fit_split(turn["buffer"], NARRATE_PREFIX_AUTO)
        if not head or not tail:
            break  # unsplittable — defensive, the send cap still applies
        head_content = NARRATE_PREFIX_AUTO + _blockquote(head)
        if turn.get("placeholder_msg_id"):
            discord_edit_message(token, chat_id, turn["placeholder_msg_id"], head_content)
        else:
            msg_id = discord_send_message(token, chat_id, head_content)
            if not msg_id:
                break
            turn["placeholder_msg_id"] = msg_id
            turn["replies_at_create"] = current_reply_count
        turn["buffer"] = head  # sealed entry snapshots this for finalize
        _seal_segment(turn)
        turn["buffer"] = tail
        log(f"split oversized narrate buffer for {turn_key} "
            f"({len(tail)} chars carried to next segment)")

    # Both modes show the live bold+italic "Narrating…" status marker while the
    # turn is mid-flight; finalize transitions it (collapse deletes it; always
    # swaps to the plain-bold "Narration" header). Prereply may post BEFORE any
    # prose accumulates — render a "…" stub so the blockquote isn't empty;
    # subsequent watch ticks replace it.
    content = NARRATE_PREFIX_AUTO + _blockquote(turn["buffer"] or "…")

    if turn.get("placeholder_msg_id"):
        if discord_edit_message(token, chat_id, turn["placeholder_msg_id"], content):
            log(f"edited narrate placeholder for {turn_key}")
    else:
        msg_id = discord_send_message(token, chat_id, content)
        if msg_id:
            turn["placeholder_msg_id"] = msg_id
            turn["replies_at_create"] = current_reply_count
            log(f"created narrate placeholder {msg_id} for {turn_key}")
        else:
            log(f"narrate placeholder send failed for {turn_key}")

    state[turn_key] = turn
    _save_state(state)
    return 0


def _merge_seen_uuids(turn: dict, new_uuids: list[str]) -> None:
    """Append any not-yet-recorded assistant-message uuids to turn state."""
    if not new_uuids:
        return
    seen = turn.get("seen_text_uuids") or []
    for u in new_uuids:
        if u not in seen:
            seen.append(u)
    turn["seen_text_uuids"] = seen


# ---------------------------------------------------------------------------
# Finalize phase — seal / collapse the turn's placeholders at Stop
# ---------------------------------------------------------------------------


def handle_finalize(payload: dict) -> int:
    """Stop: clean up the narrate placeholders for this turn.

    A turn may contain multiple segment placeholders if a mid-turn reply
    triggered rotation. Collapse mode deletes them all (the placeholder is
    purely ephemeral). Always mode edits each to the settled Narration header
    so they read as section headers above their respective replies.
    """
    transcript_path = payload.get("transcript_path") or ""
    with _state_lock():
        return _handle_finalize_locked(payload, transcript_path)


def _finalize_one_turn(
    turn_key: str, turn: dict, token: str, state_dir: str,
    transcript_path: str = "",
) -> None:
    """Finalize a single turn's narrate placeholders in-place.

    Mutates `turn` (sets finalized=True). Network calls use the passed token so
    the caller resolves it once per Stop, not per turn.
    """
    chat_id = turn.get("chat_id")

    # Resolve mode at finalize time from the CURRENT channel config, not the
    # value snapshotted at turn-start. Otherwise a turn that began under
    # `always` (or before the channel was switched to `collapse`) finalizes
    # under the stale mode and the narration persists instead of collapsing —
    # the "collapse doesn't work / channel gets clogged on long turns" bug. The
    # live channel config is the source of truth.
    snapshot_mode = turn.get("mode", "never")
    current_mode = channel_mode(chat_id) if chat_id else snapshot_mode
    mode = current_mode
    if snapshot_mode != current_mode:
        log(f"mode changed mid-turn {snapshot_mode}->{current_mode} "
            f"({turn_key}); finalizing as {current_mode}")

    # Collect everything to finalize: the live segment + all sealed segments
    # from mid-turn rotations.
    segments: list[dict] = list(turn.get("sealed_placeholders", []))
    live_msg_id = turn.get("placeholder_msg_id")
    if live_msg_id:
        segments.append({
            "msg_id": live_msg_id,
            "buffer": turn.get("buffer", ""),
        })

    if segments and chat_id:
        for seg in segments:
            seg_msg_id = seg.get("msg_id")
            if not seg_msg_id:
                continue
            buf = (seg.get("buffer") or "").strip()
            if not buf:
                # Empty placeholder — almost always a prereply stub whose reply
                # tool ran with no prose around it. Nothing to settle into, just
                # remove (same in both collapse and always).
                if discord_delete_message(token, chat_id, seg_msg_id):
                    log(f"deleted empty narrate placeholder {seg_msg_id} ({turn_key})")
                continue
            if mode == "collapse":
                if discord_delete_message(token, chat_id, seg_msg_id):
                    log(f"deleted narrate placeholder {seg_msg_id} (collapse, {turn_key})")
            elif mode == "always":
                content = NARRATE_PREFIX_ALWAYS + _blockquote(buf)
                if discord_edit_message(token, chat_id, seg_msg_id, content):
                    log(f"finalized narrate placeholder {seg_msg_id} (always, {turn_key})")

    turn["finalized"] = True


def _handle_finalize_locked(payload: dict, transcript_path: str) -> int:
    """Body of handle_finalize executed under _state_lock().

    Stop fires once at the END of the whole agent turn, with only
    transcript_path — no message_id to tell us which turn ended. Since the
    session is wrapping up, EVERY not-yet-finalized turn should collapse now.
    Sweeping all of them (rather than resolving a single key from "latest user
    message") is what stops the multi-channel orphan bug: when two channels
    interleaved, a single-key finalize cleaned only the newest turn and left
    the other channel's placeholder up forever.
    """
    state = _load_state()
    pending = [
        (k, v) for k, v in state.items()
        if isinstance(v, dict)
        and not k.startswith("_")  # skip reserved sub-tables (e.g. _react)
        and not v.get("finalized")
        and ("placeholder_msg_id" in v or "sealed_placeholders" in v)
    ]
    if pending:
        state_dir = detect_discord_state_dir()
        token = read_bot_token(state_dir)
        if token:
            for turn_key, turn in pending:
                _finalize_one_turn(turn_key, turn, token, state_dir,
                                   transcript_path)
                state[turn_key] = turn
        else:
            log(f"no bot token at {state_dir} — skipping finalize sweep")

    # Garbage-collect old finalized turns (>24h) so state stays bounded.
    now = time.time()
    cutoff = now - 86400
    pruned = {
        k: v for k, v in state.items()
        if not (isinstance(v, dict) and v.get("finalized")
                and isinstance(v.get("ts"), (int, float)) and v["ts"] < cutoff)
    }
    # Stamp finalize time on the turns we just settled so GC can age them out.
    for turn_key, turn in pending:
        if turn_key in pruned:
            pruned[turn_key] = {**turn, "ts": now}
    _save_state(pruned)
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--mode",
        choices=("watch", "prereply", "finalize"),
        required=True,
    )
    args = ap.parse_args()

    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log("bad JSON input, passing through")
        return 0

    if args.mode == "watch":
        return handle_watch(payload)
    if args.mode == "prereply":
        # Fires as PreToolUse on the Discord reply tool. Same logic as
        # handle_watch, but the timing guarantees the placeholder lands BEFORE
        # the reply message — narration reads as "the bot was thinking THIS,
        # then said THAT" instead of racing behind the reply (PostToolUse hooks
        # fire after the reply has shipped). is_prereply=True forces placeholder
        # creation even with no prose yet, so post-reply prose edits THIS
        # placeholder instead of creating a new one below the reply.
        #
        # Only the Discord reply tool's PreToolUse should anchor the prose
        # placeholder (it positions relative to the upcoming reply); other
        # tools' PreToolUse have nothing to anchor here.
        tool_name = payload.get("tool_name", "")
        if tool_name != _DISCORD_REPLY_TOOL:
            return 0
        return handle_watch(payload, is_prereply=True)
    return handle_finalize(payload)


if __name__ == "__main__":
    sys.exit(main())
