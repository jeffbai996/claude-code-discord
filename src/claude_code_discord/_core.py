"""Core primitives for the Claude Code → Discord observability layer.

These are the shared, dependency-free building blocks that the hooks
(narrate / tool-watcher / react) are built on:

  * Discord REST helpers (send / edit a message via stdlib urllib).
  * The `>>>`-blockquote renderer used for narration prose.
  * Per-turn state persistence with a reentrant file lock.
  * Transcript parsers that resolve the inbound Discord message a turn is
    answering, and the byte offset just past it.

Narration is the plaintext assistant text that appears between tool calls
in the Claude Code session transcript. The person watching in Discord
doesn't see the terminal, so surfacing that prose is what stops the bot
from looking silent during a long task.

Per-turn state is keyed by the Discord ``message_id`` of the inbound
message a turn is responding to — NOT the user-entry timestamp.
``message_id`` is the stable, globally-unique turn identity. Timestamp
keying broke when the bot was hit in two channels at once: both turns
share one session/transcript, so "latest user message" flip-flopped
between the two channels mid-flight. That caused (1) narration landing in
the wrong channel and (2) placeholders that never collapsed. Keying by
message_id (paired with its chat_id from the SAME <channel> tag) makes
each inbound message its own turn.

State lives under a single directory resolved once at import:
  CCD_STATE_DIR env override → ~/.claude/claude-code-discord/
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from typing import Any, Iterator


# ---------------------------------------------------------------------------
# State-dir / path resolution
# ---------------------------------------------------------------------------
#
# A single directory holds all runtime state (per-turn placeholders, the
# durable last-origin stamp, the log). Resolution is a single-user default
# with one env override — no per-bot partitioning.


def _resolve_state_dir() -> str:
    """Resolve the state directory.

    Priority:
      1. CCD_STATE_DIR env override.
      2. ~/.claude/claude-code-discord/  (default).
    """
    override = os.environ.get("CCD_STATE_DIR", "")
    if override:
        return os.path.expanduser(override)
    return os.path.expanduser("~/.claude/claude-code-discord")


STATE_DIR = _resolve_state_dir()
STATE_PATH = os.path.join(STATE_DIR, "state.json")
LOG_PATH = os.environ.get("CCD_LOG", os.path.join(STATE_DIR, "narrate.log"))

# Discord 2000-char per-message hard limit; leave headroom for our prefix.
DISCORD_LIMIT = 2000
# Bold+italic for the live "narrating…" marker so it's visually distinct
# from the body even at a glance.
NARRATE_PREFIX_AUTO = "💬 ***Narrating…***\n"


# ---------------------------------------------------------------------------
# Discord state dir (bot token) resolution
# ---------------------------------------------------------------------------


def detect_discord_state_dir() -> str:
    """Find the Discord plugin's state dir.

    Priority order (matches how the plugin itself resolves):
      1. DISCORD_STATE_DIR env.
      2. CLAUDE_CONFIG_DIR/channels/discord.
      3. ~/.claude/channels/discord  (default — resolves per machine via $HOME).
    """
    explicit = os.environ.get("DISCORD_STATE_DIR", "")
    if explicit:
        return explicit
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if cfg:
        return os.path.join(cfg, "channels", "discord")
    return os.path.expanduser("~/.claude/channels/discord")


def read_bot_token(state_dir: str) -> str | None:
    """Read DISCORD_BOT_TOKEN from the plugin's state dir .env."""
    env_path = os.path.join(state_dir, ".env")
    if not os.path.exists(env_path):
        log(f"no token file at {env_path}")
        return None
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("DISCORD_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError as e:
        log(f"failed to read {env_path}: {e}")
    return None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Blockquote renderer
# ---------------------------------------------------------------------------

_NBSP = " "  # non-breaking space — Discord preserves these at line start
_LIST_RE = re.compile(r"^(\s*)([\-*+]|\d+\.)\s")
_FENCE_SAFE = "`​`​`"  # ``` with zero-width spaces spliced between


def _blockquote(text: str) -> str:
    """Wrap text in Discord's `>>>` blockquote: neutralize fences, indent lists.

    SINGLE rendering boundary for narration — runs on the full concatenated
    buffer (not per-segment), so it sees fences and lists exactly as Discord
    will. Doing fence-neutralization on the whole buffer (rather than per text
    block) is what fixes the "salad" leak: a ``` spanning two watcher fires
    used to slip through because each half was neutralized separately but the
    concatenation re-formed a raw run. On the whole buffer there are no
    half-fences.

    Transforms, in order:

    1. Neutralize ``` runs → `​`​` (zero-width spaces between backticks). A
       real ``` inside `>>>` always wins and opens a fenced code block,
       turning narration into a salad of quoted + unquoted fragments. The
       ZWSP splice keeps the visual ``` at normal zoom but stops Discord's
       fence regex from firing. Since all fences die here, there's no
       in-code-fence state to track — everything downstream is prose.

    2. Indent list lines. List rendering inside `>>>` is flat — "1. foo" /
       "2. bar" sit flush with the quote bar as a wall of text. Discord
       collapses leading ASCII spaces but preserves U+00A0, so prefix every
       list line (-/*/+ bullets and N. numbers) with two NBSPs base indent
       plus one NBSP per original leading-whitespace char (nesting depth).

    3. Strip trailing whitespace so the block doesn't end on padded blanks.
    """
    text = text.replace("```", _FENCE_SAFE)
    out_lines: list[str] = []
    for raw in text.rstrip().splitlines():
        m = _LIST_RE.match(raw)
        if m:
            leading_ws = m.group(1)
            rest = raw[len(leading_ws):]
            out_lines.append(_NBSP * 2 + (_NBSP * len(leading_ws)) + rest)
            continue
        out_lines.append(raw)
    return ">>> " + "\n".join(out_lines)


# ---------------------------------------------------------------------------
# Per-channel tools mode
# ---------------------------------------------------------------------------


def _channel_tools_mode(state_dir: str, chat_id: str) -> str:
    """Resolve the channel's `tools` mode from sibling tools.json.

    Used at narrate finalize time to decide whether tool messages
    should be deleted (collapse) or kept (ticker/diffs/full). Returns
    'off' on any missing-file or parse error.

    Note: thread→parent inheritance (present in the full tool) is intentionally
    omitted here — this core reads only the direct chat_id entry.
    """
    path = os.path.join(state_dir, "tools.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return "off"
    if not isinstance(data, dict):
        return "off"
    if chat_id in data:
        val = data[chat_id]
        return val if isinstance(val, str) else "off"
    return "off"


# ---------------------------------------------------------------------------
# Per-turn state + reentrant file lock
# ---------------------------------------------------------------------------

_LOCK_PATH = STATE_PATH + ".lock"

_lock_depth = 0  # per-process reentrancy counter (each hook run is one process)


@contextmanager
def _state_lock() -> Iterator[None]:
    """Exclusive, REENTRANT file lock around read-modify-write of state.json.

    PostToolUse fires after every tool call, so multi-tool turns
    (Bash + Read + Edit clusters) trigger many concurrent invocations
    of this script. Without locking, two concurrent watchers can both
    read state showing 'no placeholder_msg_id', both decide to create
    one, and we end up with duplicate placeholders for the same turn.

    fcntl.flock() blocks the SECOND PROCESS until the first commits, and
    auto-releases on fd close. But flock is per-open-file-description: a
    SAME-process nested acquire opens a new fd whose LOCK_EX blocks on the
    fd already held by this very process — a self-deadlock (e.g. a nested
    helper that re-enters the lock while the outer one is still held would
    wedge itself while holding the lock and starve every other process on
    this shared lock file). Reentrancy via a per-process depth counter fixes
    the whole class: we flock only on the outermost acquire and unlock only
    on the outermost release; nested acquires just pass through.
    """
    global _lock_depth
    if _lock_depth > 0:
        _lock_depth += 1
        try:
            yield
        finally:
            _lock_depth -= 1
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    f = open(_LOCK_PATH, "a")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        _lock_depth = 1
        yield
    finally:
        _lock_depth = 0
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        f.close()


def _load_state() -> dict[str, Any]:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.rename(tmp, STATE_PATH)
    except OSError as e:
        log(f"state save failed: {e}")


def _turn_key_for_message(message_id: str) -> str:
    """Build the state key for an inbound Discord message_id.

    message_id is the stable, globally-unique turn identity. Pairing the
    key to the inbound message (rather than a timestamp that the
    transcript's "latest user entry" lookup resolves) is what keeps two
    interleaved channels from colliding on one key.
    """
    return message_id


# ---------------------------------------------------------------------------
# Discord channel-tag parsing + user-entry text extraction
# ---------------------------------------------------------------------------

# Discord channel tag patterns. The plugin emits:
#   <channel source="discord" chat_id="..." message_id="..." user="..." ts="...">
# Both quoted forms (single + double) seen in the wild.
#
# BOTH ids are matched as numeric snowflakes (17-20 digits) on purpose.
# Documentation and persona text carry EXAMPLE tags — placeholder ids like
# chat_id="X", or a REAL chat_id paired with a fake message_id="1". A
# permissive message_id capture treated those doc tags as real inbound
# origins, so an indicator that posts to the tag's real chat_id could leak
# into channels the user never messaged. A real Discord message_id is ALWAYS
# a snowflake, so requiring digits on both ids rejects every doc/example tag
# at the source — only genuine inbound tags parse as origins.
CHANNEL_TAG_RE = re.compile(
    r'<channel\s+source=["\'](?:plugin:discord:discord|discord)["\']'
    r'[^>]*?chat_id=["\'](\d{17,20})["\']'
    r'[^>]*?message_id=["\'](\d{17,20})["\']',
    re.IGNORECASE,
)


def parse_discord_origins(user_text: str) -> list[tuple[str, str]]:
    """Return ALL Discord origins in the user text, in document order.

    A single user_text can contain multiple <channel> tags when the plugin
    batches several Discord messages between assistant turns (e.g. two
    people speaking before the bot responds). Each tag is one inbound
    message that may need its own reaction.
    """
    if not user_text:
        return []
    return [(m.group(1), m.group(2)) for m in CHANNEL_TAG_RE.finditer(user_text)]


def _extract_user_text(user_entry: dict | None) -> str:
    if not user_entry:
        return ""
    msg = user_entry.get("message", {})
    content = msg.get("content", [])
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                txt = c.get("text", "")
                if isinstance(txt, str):
                    parts.append(txt)
        return "\n".join(parts)
    return ""


# ---------------------------------------------------------------------------
# Synthetic-hook-feedback detection + last-user-entry resolution
# ---------------------------------------------------------------------------

_SYNTHETIC_USER_PREFIXES = (
    "Stop hook feedback:",
    "This session is being continued from a previous conversation",
)


def _is_synthetic_hook_feedback(content: Any) -> bool:
    """Detect harness-synthesized user entries that don't bound a real turn.

    These look like user messages (type='user') but they are synthesized by
    the harness, not sent by a real user, and they don't carry the inbound
    <channel> tag:

    - Stop-hook BLOCKED feedback injected by an echo-guard. Skipping these
      lets the retry-after-block path see the ORIGINAL Discord-origin user
      message as the latest real prompt, so reactions resolve correctly
      instead of seeing 'no origin'.
    - Compaction-continuation markers ("This session is being continued...").
      When compaction fires more than once in a session, these can appear
      back-to-back, and a nearer one can sit closer to 'now' than the real
      Discord-origin tag that's still deeper in the transcript. Treating them
      as turn boundaries stops the backward scan too early, losing the real
      origin.
    """
    if isinstance(content, str):
        stripped = content.lstrip()
        return stripped.startswith(_SYNTHETIC_USER_PREFIXES)
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                t = c.get("text", "")
                if isinstance(t, str) and t.lstrip().startswith(_SYNTHETIC_USER_PREFIXES):
                    return True
    return False


def _last_user_entry(transcript_path: str) -> dict | None:
    """Return the most recent REAL user entry (text content, not a tool_result).

    Also skips synthesized Stop-hook feedback messages (echo-guard BLOCK
    retries) — those look like user messages but were injected by the
    harness and don't carry the inbound <channel> tag.
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
        if obj.get("type") != "user":
            continue
        msg = obj.get("message", {})
        content = msg.get("content", [])
        # Skip tool_result-only entries — those are tool replies, not real prompts.
        if isinstance(content, list):
            kinds = {c.get("type") for c in content if isinstance(c, dict)}
            if kinds and kinds.issubset({"tool_result"}):
                continue
        if _is_synthetic_hook_feedback(content):
            continue
        return obj
    return None


# ---------------------------------------------------------------------------
# Durable "last resolved origin" — survives a mid-task COMPACTION
# ---------------------------------------------------------------------------
#
# The normal origin lookup reads the transcript for the inbound <channel>
# tag, but a compaction summarizes that tag away, so post-compact the lookup
# finds nothing and narration/trace/thinking silently dies for the rest of
# the task. We stamp the origin here on every successful resolve and fall
# back to it when the transcript yields none, so the turn carries on where it
# left off. Recency-gated so a stale origin from a finished turn can't
# resurrect and leak narration into a dead channel.
_LAST_ORIGIN_TTL_SEC = 1800  # 30 min — long enough to bridge a compact + the
                             # rest of a long task, short enough that an idle
                             # gap doesn't reattach to an old conversation.


def _last_origin_path() -> str:
    return os.path.join(STATE_DIR, "last_origin.json")


def _load_last_origin(reply_chat_id: str | None = None) -> tuple[str, str, str] | None:
    """Freshest durable origin, if recent. When reply_chat_id is known, only
    return a durable origin for THAT channel (so a post-compact turn replying
    in X can't be anchored to a stale origin from Y). Returns
    (chat_id, message_id, turn_ts) or None."""
    try:
        with open(_last_origin_path()) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    if (time.time() - float(d.get("saved_at", 0) or 0)) > _LAST_ORIGIN_TTL_SEC:
        return None
    chat_id = d.get("chat_id") or ""
    message_id = d.get("message_id") or ""
    if not chat_id or not message_id:
        return None
    if reply_chat_id and chat_id != reply_chat_id:
        return None
    return chat_id, message_id, d.get("turn_ts") or ""


def _current_origin(transcript_path: str) -> tuple[str, str] | None:
    """Resolve (chat_id, message_id) of the inbound message this turn answers.

    Reads the most recent real user entry and returns the LAST <channel>
    tag in it (document-order last == chronologically newest). chat_id and
    message_id come from the SAME tag, so routing and turn-keying can never
    disagree.

    Falls back to the durable last-resolved origin when the latest user entry
    carries no tag — e.g. it's an ESC-interrupt marker
    ("[Request interrupted by user]") or a compaction-continuation remnant.
    Without this, every tool call for the rest of the turn silently stops
    producing a trace the moment either of those entries becomes the newest
    one. Returns None only when there's genuinely no resolvable origin (not a
    Discord-origin turn at all, or the durable origin has expired).
    """
    user = _last_user_entry(transcript_path)
    user_text = _extract_user_text(user)
    origins = parse_discord_origins(user_text)
    if origins:
        return origins[-1]
    revived = _load_last_origin()
    if revived is not None:
        chat_id, message_id, _turn_ts = revived
        return chat_id, message_id
    return None


def _get_turn(state: dict[str, Any], key: str) -> dict[str, Any] | None:
    val = state.get(key)
    return val if isinstance(val, dict) else None


# ---------------------------------------------------------------------------
# Discord REST helpers (stdlib urllib)
# ---------------------------------------------------------------------------


def _discord_request(
    method: str, url: str, token: str, body: dict | None = None,
    _retries_left: int = 3,
) -> dict | None:
    data = None
    headers = {
        "Authorization": f"Bot {token}",
        "User-Agent": "claude-code-discord/0.1",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif method in ("POST", "PATCH"):
        # POST/PATCH with empty body still needs the length header
        data = b""
        headers["Content-Length"] = "0"

    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if 200 <= resp.status < 300:
                raw = resp.read()
                return json.loads(raw) if raw else {}
            log(f"{method} {url} → HTTP {resp.status}")
            return None
    except urllib.error.HTTPError as e:
        # Ride out a 429 BURST, not just a single one. A busy multi-channel bot
        # fires a wave of edits/deletes at finalize that saturates a channel's
        # rate-limit bucket; a one-shot retry meant the indicator's own
        # animate/settle edit could lose BOTH attempts and drop the frame —
        # leaving the spinner looking frozen mid-tier. Up to 3 backoff retries
        # honoring Retry-After lets the edit land once the burst drains, at the
        # cost of only sleeping when we're genuinely throttled.
        if e.code == 429 and _retries_left > 0:
            retry_after = e.headers.get("Retry-After", "1")
            try:
                wait = max(0.1, min(3.0, float(retry_after)))
            except ValueError:
                wait = 1.0
            log(f"{method} 429 backoff {wait}s ({_retries_left} left)")
            time.sleep(wait)
            return _discord_request(method, url, token, body,
                                    _retries_left=_retries_left - 1)
        log(f"{method} {url} HTTP {e.code} body={e.read()[:200]!r}")
        return None
    except Exception as e:
        log(f"{method} {url} failed: {e}")
        return None


def _fence_safe_truncate(content: str, limit: int = DISCORD_LIMIT) -> str:
    """Cap content to Discord's char limit WITHOUT orphaning a ``` fence.

    A naive content[:limit] can slice through (or drop) a block's closing
    ```; Discord then renders the rest of the channel as one runaway code
    block — the "unclosed code block" bug. When the cut leaves an odd number
    of fences, trim back past any partial backticks and append a closing
    fence so the block always closes within the limit."""
    if len(content) <= limit:
        return content
    cut = content[:limit]
    if cut.count("```") % 2 == 0:
        return cut
    # Odd fence count -> a closing ``` was lost. Reserve room (newline +
    # 3 backticks) and re-close, stripping any partial backticks at the cut.
    trimmed = content[:limit - 4].rstrip("`").rstrip()
    if trimmed.count("```") % 2 == 1:
        trimmed += "\n```"
    return trimmed[:limit]


def discord_send_message(token: str, chat_id: str, content: str) -> str | None:
    """POST /channels/{id}/messages. Returns the new message_id or None."""
    url = f"https://discord.com/api/v10/channels/{chat_id}/messages"
    body = {"content": _fence_safe_truncate(content),
            "allowed_mentions": {"parse": []}}
    resp = _discord_request("POST", url, token, body)
    return resp.get("id") if resp else None


def discord_edit_message(token: str, chat_id: str, msg_id: str, content: str) -> bool:
    url = f"https://discord.com/api/v10/channels/{chat_id}/messages/{msg_id}"
    body = {"content": _fence_safe_truncate(content)}
    return _discord_request("PATCH", url, token, body) is not None


# ---------------------------------------------------------------------------
# Transcript walker — pick up new assistant text since last_seen
# ---------------------------------------------------------------------------


def _byte_offset_after_current_user_turn(
    transcript_path: str, turn_ts: str,
) -> int:
    """Return the byte position just after the user-entry line matching turn_ts.

    Critical for first-run turn init: we only want to narrate text blocks
    that come AFTER the inbound message we're responding to. Initializing
    last_byte_offset at 0 would replay the entire session's history.

    Falls back to end-of-file (not 0) if the matching user entry isn't
    found — safer to narrate nothing than to flood with history.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return 0
    try:
        size = os.path.getsize(transcript_path)
    except OSError:
        return 0
    try:
        offset = 0
        with open(transcript_path) as f:
            while True:
                line = f.readline()
                if not line:
                    break
                # The newline that .readline() included is also the file
                # separator we care about — offset = end-of-this-line
                next_offset = offset + len(line.encode("utf-8"))
                stripped = line.strip()
                if stripped:
                    try:
                        obj = json.loads(stripped)
                    except json.JSONDecodeError:
                        obj = {}
                    if (obj.get("type") == "user"
                            and (obj.get("timestamp") or obj.get("ts")) == turn_ts):
                        return next_offset
                offset = next_offset
    except OSError as e:
        log(f"user-turn search failed: {e}")
        return size
    # Not found — fall back to end-of-file so we narrate nothing
    # rather than flooding history.
    return size


# ---------------------------------------------------------------------------
# Segment sealing
# ---------------------------------------------------------------------------


def _seal_segment(turn: dict) -> None:
    """Seal the current segment's placeholder and reset for a new one.

    Called when a mid-turn reply lands. The current placeholder stays
    where it is in the channel (above its triggering reply) and is
    appended to sealed_placeholders for Stop-time finalization. Buffer
    and live id reset so the next narrate text creates a fresh
    placeholder below the reply.
    """
    msg_id = turn.get("placeholder_msg_id")
    if msg_id:
        turn.setdefault("sealed_placeholders", []).append({
            "msg_id": msg_id,
            "buffer": turn.get("buffer", ""),
        })
    turn["placeholder_msg_id"] = None
    turn["buffer"] = ""
