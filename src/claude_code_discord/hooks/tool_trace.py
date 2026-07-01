"""PostToolUse hook: surface tool calls into the Discord narrate placeholder.

Reads the per-channel `tools` mode from the bot's tools.json sibling file
and, based on that mode, appends a one-line ticker entry (and optionally
a diff block or Bash stdout block) into the same per-turn placeholder
that the narrate hook owns. We share state file + helpers with narrate so
prose + tool traces live as one cohesive segment per reply boundary.

Modes:
  off      — no surfacing (default; this script exits 0)
  collapse — same as `diffs` while live (ticker + edit diffs), but the
             entire tool message gets deleted at Stop. Symmetric with
             narrate's 'collapse' mode — pair them when you want full
             visibility during the turn and a clean channel afterwards.
  ticker   — one-line `! ToolName(short args)` per tool call (orange on
             ```diff highlighter; `- ...` for errored calls renders red).
             Cross-platform color: works on Discord desktop AND mobile.
             Persists past Stop.
  diffs    — ticker + ```diff unified diff for Edit/Write/MultiEdit (kept)
  full     — diffs + plain ```fenced Bash stdout, secret-stripped (kept)

The hook input is the standard Claude Code PostToolUse payload:
  {
    "session_id": "...",
    "transcript_path": "/path/to/transcript.jsonl",
    "tool_name": "Bash",
    "tool_input": {...tool-specific...},
    "tool_response": {...tool-specific...}
  }
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
from typing import Any

# Reuse the core primitives so we share state, segment rotation, token
# resolution, and the same Discord HTTP helpers with the narrate hook.
from claude_code_discord._core import (
    DISCORD_LIMIT,
    _channel_tools_mode,
    _byte_offset_after_current_user_turn,
    _get_turn,
    _load_state,
    _save_state,
    _state_lock,
    _turn_key_for_message,
    _current_origin,
    detect_discord_state_dir,
    discord_edit_message,
    discord_send_message,
    log,
    read_bot_token,
    _last_user_entry,
)


# Pattern for "smells like a credential" — long base64/hex-ish runs.
# Used by full-mode Bash stdout stripping. Conservative on false positives
# is fine; the goal is "obvious tokens", not full-coverage DLP.
# Upper-bounded at 256 so a pathological line (e.g. 10KB of repeated chars in
# a diff) can't make the unbounded {32,} run scan degenerate — real tokens are
# well under 256 chars; anything longer just gets redacted in 256-char chunks.
_SECRET_RE = re.compile(r"[A-Za-z0-9_\-]{32,256}")


def _redact_secrets(text: str) -> str:
    """Replace anything looking like a credential with <REDACTED>."""
    return _SECRET_RE.sub("<REDACTED>", text)


def _channel_mode(state_dir: str, chat_id: str) -> str:
    """Resolve the channel's tools mode from tools.json. Delegates to the core
    resolver. Returns 'off' on any error (fail safe / silent)."""
    return _channel_tools_mode(state_dir, chat_id)


# Tools whose input contains a file path we'd format. Keys map to the
# input field that holds the path.
_PATH_TOOL_FIELDS = {
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}

# Tool input fields we'll show in the ticker arg digest. Falls back to
# json.dumps with truncation when none of these match.
_ARG_DIGEST_PREFERENCE = (
    "file_path", "notebook_path", "pattern", "command", "url",
    "symbols", "symbol", "ticker", "query",
)


# Whole-line ceiling for the tool-invocation header (`+ ● Tool(digest) tail`).
# 83 is about the most that fits before the header spills at a typical Discord
# zoom (tuned empirically). _ticker_line budgets the digest against THIS so the
# full line never exceeds it — the cap is on the rendered line, not the digest
# alone. Matched to TRACE_ROW_MAX=84 so headers reach the SAME right edge as
# diff rows (every row to a uniform width). The `●` marker is a WIDE glyph
# (~2 render cells) counted as 1 in len(), so a len()==84 header renders ~85
# cells. Cap at 83 → ~84 visual = flush with the diff edge. (Diff rows have no
# wide glyph, so they cap at the literal 84.)
HEADER_LINE_MAX = 83


def _arg_digest(tool_name: str, tool_input: dict, max_len: int = 55) -> str:
    """Short, one-line arg representation for the ticker.

    Picks the most "ID-shaped" field of the input, falls back to a json
    dump. Always single-line, always under max_len.

    max_len is normally supplied by _ticker_line (budgeted so the whole
    header line ≤ HEADER_LINE_MAX); the default here is just a fallback for
    any direct caller.
    """
    if not isinstance(tool_input, dict):
        return ""
    for key in _ARG_DIGEST_PREFERENCE:
        if key in tool_input:
            v = tool_input[key]
            if isinstance(v, str):
                s = v.strip().replace("\n", " ")
                if len(s) > max_len:
                    s = s[: max_len - 1] + "…"
                return s
    # Fallback: short JSON dump
    try:
        s = json.dumps(tool_input, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        s = str(tool_input)
    s = s.replace("\n", " ")
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s


TOOL_PREFIX = "🔧 ***Tool trace…***\n"
TOOL_PREFIX_FINAL = "🔧 **Tool trace**\n"


_TOOL_BLOCK_LEFT_PAD = " "  # 1 cell — match the single space after diff markers


def _tool_message_content(tool_buffer: str, prefix: str = TOOL_PREFIX,
                          panel: str = "") -> str:
    """Render the tool-trace message: prefix + ```diff``` fenced buffer.

    `panel` (agent-view footer, its own fenced block) is appended after
    the diff fence so it always sits at the visual bottom of the trace —
    edits don't reorder messages, so the footer rides every re-render
    and migrates automatically when the segment rotates.

    Lives as its OWN Discord message (not merged into the narrate
    placeholder) so the narration flow stays clean.

    Uses Discord's `diff` syntax highlighter, which works on BOTH
    desktop AND mobile (unlike `ansi` which only colors on desktop).
    The `+`-prefix renders green, `-` red, `!` orange, leading-space
    plain. Trade-off vs ANSI: only 3-4 colors available and the prefix
    glyph is visible, but it's universal.

    Padding is ONE cell, to match the single space inserted after diff
    markers (so colorized `+ x` lines and plain ` y` context lines align
    at the same visual column). NB: Discord's diff highlighter requires
    the colorizing char (+/-/!/@) at column 0 to colorize the line, so we
    never pad those — we only insert the single space AFTER the marker.
    Plain context / Bash-stdout lines (no colorizer) get the 1-cell pad.
    """
    if not tool_buffer:
        return prefix + ("\n" + panel if panel else "")
    # Colorizer chars (+/-/!/@) must stay at column 0 for Discord's diff
    # highlighter to fire. So we don't leading-pad those lines — but we
    # DO ensure there's a single space between the marker and the
    # content (so '+<p>' becomes '+ <p>'), which aligns colorized
    # content at the same visual column as the 2-space-padded plain
    # context lines below.
    padded_lines: list[str] = []
    for ln in tool_buffer.splitlines():
        if not ln:
            padded_lines.append(ln)
            continue
        first = ln[0]
        if first in "+-!@":
            # Ensure a space after the marker. If the next char is
            # already a space, leave it; otherwise insert one.
            if len(ln) > 1 and ln[1] != " ":
                ln = ln[0] + " " + ln[1:]
            padded_lines.append(ln)
        else:
            # Plain context line (typically Bash stdout already
            # space-prefixed). Apply the 2-cell pad.
            padded_lines.append(_TOOL_BLOCK_LEFT_PAD + ln)
    padded = "\n".join(padded_lines)
    out = prefix + "```diff\n" + padded + "\n```"
    if panel:
        out += "\n" + panel
    return out


def _failure_reason(tool_response: Any) -> tuple[str, str]:
    """Classify a failed tool call → (verb, reason) for the ticker tail.

    verb is FAILED (ran and errored) or BLOCKED (never ran — permission
    denied). reason is a short tail like 'exit 1' / 'timeout' / 'denied'.

    The PostToolUseFailure / errored tool_result content is usually a
    string ("Exit code 1\\n…", a <tool_use_error>…</tool_use_error>, etc.)
    but can be a dict with is_error/exit_code — handle both.
    """
    text = ""
    code = None
    if isinstance(tool_response, dict):
        code = tool_response.get("exit_code")
        c = tool_response.get("content") or tool_response.get("output") or tool_response.get("stdout") or ""
        text = c if isinstance(c, str) else json.dumps(c)
    elif isinstance(tool_response, str):
        text = tool_response

    low = text.lower()
    # Permission denial — the tool never ran.
    if "permission" in low and ("deny" in low or "denied" in low or "not allow" in low):
        return ("BLOCKED", "denied")
    if "requested permissions" in low or "user doesn't want" in low or "rejected" in low:
        return ("BLOCKED", "denied")
    # Timeout.
    if "timed out" in low or "timeout" in low:
        return ("FAILED", "timeout")
    # Exit code, e.g. "Exit code 1".
    m = re.search(r"exit code (\d+)", low)
    if m:
        return ("FAILED", f"exit {m.group(1)}")
    if isinstance(code, int) and code != 0:
        return ("FAILED", f"exit {code}")
    # Generic tool_use_error.
    if "<tool_use_error>" in low or "no such tool" in low:
        return ("FAILED", "error")
    return ("FAILED", "")


def _ticker_line(
    tool_name: str, tool_input: dict, errored: bool = False,
    reason: str = "", verb: str = "FAILED",
) -> str:
    """One-line tool-invocation header: `+ ● ToolName(short-args)`.

    Goes inside a ```diff``` fence. Discord's diff highlighter colors a
    whole line by its leading character. Empirically: `+` and `!` both
    render GREEN, `-` renders RED. Since `+`/`!` are indistinguishable, we
    use `+` for the header and rely on the `●` dot as the marker that says
    "this is a tool invocation, not a file-edit line." Edit lines stay bare
    `+`/`-` with NO dot, so:

      `+ ● Read(file)`   → green header, has the dot  (a tool ran)
      `+     new code`   → green edit line, no dot     (a line was added)
      `-     old code`   → red edit line, no dot       (a line was removed)

    Errored calls render red with a verb + reason tail:
      `- ● Bash(cmd) FAILED · exit 1`
      `- ● Edit(secrets.env) BLOCKED · denied`

    The ceiling is on the WHOLE rendered line, not just the digest: the
    digest budget is computed as HEADER_LINE_MAX − len(prefix) − len(name)
    − len("()") − len(tail), so a long tool name eats into the digest and
    the total line always lands ≤ HEADER_LINE_MAX (no spill). Capping the
    digest alone would let a header of `+ ● Bash(<full-width digest>)`
    render past the edge.
    """
    prefix = "- ● " if errored else "+ ● "
    short_name = tool_name
    if short_name.startswith("mcp__"):
        parts = short_name.split("__")
        if len(parts) >= 3:
            short_name = parts[-1]
    tail = ""
    if errored:
        tail = f" {verb}"
        if reason:
            tail += f" · {reason}"
    # Budget the digest so prefix+name+()+digest+tail ≤ HEADER_LINE_MAX.
    overhead = len(prefix) + len(short_name) + len("()") + len(tail)
    digest_budget = max(8, HEADER_LINE_MAX - overhead)  # floor so it's never absurd
    digest = _arg_digest(tool_name, tool_input, max_len=digest_budget)
    return f"{prefix}{short_name}({digest}){tail}"


def _diff_counts(before: str, after: str) -> tuple[int, int]:
    """Count added / removed lines between before and after. Used for the
    `⎿ [ +N,  -M ]` summary line above an edit's diff body."""
    added = removed = 0
    for ln in difflib.unified_diff(before.splitlines(), after.splitlines(), n=0, lineterm=""):
        if ln.startswith("+") and not ln.startswith("+++"):
            added += 1
        elif ln.startswith("-") and not ln.startswith("---"):
            removed += 1
    return added, removed


def _read_line_count(tool_response: dict) -> int | None:
    """Best-effort line count for a Read tool result. The PostToolUse hook's
    tool_response shape varies; try the common fields, fall back to None
    (caller then shows no summary rather than a wrong number)."""
    if not isinstance(tool_response, dict):
        return None
    # Common shapes: {"file": {"content": "..."}} or {"content": "..."} or
    # a raw string under "output"/"stdout".
    content = None
    f = tool_response.get("file")
    if isinstance(f, dict):
        content = f.get("content")
    if content is None:
        content = tool_response.get("content") or tool_response.get("output") or tool_response.get("stdout")
    if isinstance(content, str) and content:
        return len(content.splitlines())
    return None


def _summary_line(text: str) -> str:
    """A plain (grey) summary line under a tool header.

    Returns the bare `⎿ {text}` with NO leading space — the single
    `_TOOL_BLOCK_LEFT_PAD` cell that _tool_message_content prepends to every
    non-colorizer line supplies the one space that keeps `⎿` off column 0.
    Carrying its own leading space here would double-pad it (own space + the
    context pad), over-indenting `⎿` by an extra cell.

    Renders as (after the 1-cell pad):
      ` ⎿ [34 lines]`     (Read)
      ` ⎿ [+4, -0]`       (Edit/Write/MultiEdit)
    """
    return f"⎿ {text}"


def _is_textish_path(path: str) -> bool:
    """Heuristic: is this a text file we should diff?"""
    if not path:
        return False
    lower = path.lower()
    textish_exts = (
        ".py", ".ts", ".tsx", ".js", ".jsx", ".md", ".txt", ".json",
        ".yaml", ".yml", ".toml", ".sh", ".html", ".css", ".sql",
        ".rs", ".go", ".java", ".c", ".cpp", ".h", ".hpp", ".rb",
        ".env", ".cfg", ".ini", ".conf", ".dockerfile",
    )
    if lower.endswith(textish_exts):
        return True
    # Extensionless files in well-known config dirs — treat as text
    if "/" in path and "." not in os.path.basename(path):
        return True
    return False


# Pathological single-line cap for diff bodies. Normal source lines (even
# long ones) are left whole — but a minified-JSON / one-line file is a single
# ~2500-char line that wraps into an unreadable wall in Discord. Truncate ONLY
# lines past this with a trailing …, so real code keeps its info while a
# mega-line gets tamed. 300 is well above any human-written code line.
MEGA_LINE_MAX = 300
# Target max RENDERED ROW width for any trace line: every row should reach the
# same right edge — 84 cells. Earlier static content caps were wrong because
# they bounded CONTENT, so a row's true width drifted with the line-number
# gutter (1-digit vs 3-digit nums pushed the right edge in/out). Cap the whole
# ROW instead: content budget = TRACE_ROW_MAX − gutter, computed per diff block
# from its actual number width, so every line lands at the same edge regardless
# of gutter. A diff row is f"{marker} {num} {content}" = marker(1) + sp(1) +
# num(width) + sp(1) + content = (3 + width) + content, so the per-line content
# budget is TRACE_ROW_MAX − 3 − width.
TRACE_ROW_MAX = 84

# Character budget for the whole diff BODY (the joined lines, before the header
# + ⎿ summary + ```diff fence are added). Discord's hard message cap is 2000;
# a single tool block must fit in one message, because the rotation logic only
# splits BETWEEN blocks — an oversized single block can't be split and the
# chunker shatters it mid-line. The 30-line cap alone doesn't bound bytes:
# ~25 near-full-width lines already blow past 2000. So we also cap the body by
# chars. 1800 leaves ~200 for the header (~30) + ⎿ summary (~15) + ```diff fence
# (~12) + per-line pad (~1/line) — a comfortable margin under 2000. Whole lines
# are DROPPED with a "(N more lines)" marker — never wrapped, never mid-line
# `…` — preserving the "long one-line" rule. This is the anti-shatter floor; it
# can't go much higher without risking the 2000 cap.
DIFF_BODY_CHAR_BUDGET = 1800


def _cap_mega_line(ln: str) -> str:
    """Truncate a single diff line only if it's pathologically long (>MEGA_LINE_MAX)."""
    if len(ln) > MEGA_LINE_MAX:
        return ln[:MEGA_LINE_MAX - 1] + "…"
    return ln


def _diff_block(
    before: str, after: str, max_lines: int = 30,
) -> str | None:
    """Compact unified diff capped to max_lines AND a character budget.

    Diff-body lines are kept as ONE long line each — no hard-wrapping (that
    butchered content). They spill/wrap natively in Discord. The ONE
    exception: a pathologically long single line (>MEGA_LINE_MAX, e.g.
    minified JSON) is truncated with a …, because otherwise one line wraps
    into an unreadable wall.

    Two caps, whichever bites first, and BOTH only ever drop whole trailing
    lines (never wrap):
      • line COUNT  — max_lines
      • CHAR budget — DIFF_BODY_CHAR_BUDGET, so the assembled block fits in one
        Discord message and can't shatter.
    A single trailing `... (N more lines)` marker reports the total dropped.

    Returns None if nothing changed (caller skips the diff block).
    """
    if before == after:
        return None
    diff_lines = list(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        n=1, lineterm="",
    ))
    # Line-numbered rows: <marker><1 cell><lineno><space><content>, e.g.
    # "+ 42 foo". Marker stays at col 0 so Discord's ```diff colorizer fires;
    # the @@ header is dropped but its counters seed the line numbers
    # (new-side for +/context, old for −). Two-pass so line numbers
    # RIGHT-justify to the widest one → content column stays aligned across
    # single/double-digit lines (a ragged gutter misaligns). Content truncated
    # to a per-row budget so a long line + wide number can't wrap.
    _hunk_re = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    old_ln = new_ln = 0
    rows: list[tuple[str, int | None, str]] = []
    for ln in diff_lines:
        if ln.startswith("---") or ln.startswith("+++"):
            continue
        m = _hunk_re.match(ln)
        if m:
            old_ln, new_ln = int(m.group(1)), int(m.group(2))
            continue
        if ln.startswith("+"):
            rows.append(("+", new_ln, ln[1:])); new_ln += 1
        elif ln.startswith("-"):
            rows.append(("-", old_ln, ln[1:])); old_ln += 1
        else:
            rows.append((" ", new_ln, ln[1:] if ln.startswith(" ") else ln))
            old_ln += 1; new_ln += 1
    if not rows:
        return None
    width = max((len(str(n)) for _, n, _ in rows if n is not None), default=1)
    # Per-row content budget so every rendered row hits the same right edge
    # (TRACE_ROW_MAX): row = (3 + width) + content, so content ≤ ROW_MAX-3-width.
    content_max = max(8, TRACE_ROW_MAX - 3 - width)
    body: list[str] = []
    for marker, n, content in rows:
        c = content if len(content) <= content_max else content[:content_max - 1] + "…"
        num = str(n).rjust(width)
        if marker == " ":
            # CONTEXT line: _tool_message_content prepends a 1-cell pad to every
            # non-colorizer line (lines not starting with +/-/!/@), but NOT to
            # the +/- lines. So a context line emitted with the same 2-cell
            # "<sp><sp>num" lead as a "+ num" line ends up 1 cell deeper after
            # padding → the numbers misalign. Emit context with ONE leading
            # space so the pad brings it back to the same column as the marker
            # lines.
            body.append(f" {num} {c}")
        else:
            body.append(f"{marker} {num} {c}")
    return _cap_body(body, max_lines)


def _cap_body(body: list[str], max_lines: int = 30) -> str:
    """Cap a list of diff-body lines by line COUNT then CHAR budget, dropping
    whole trailing lines (never wrapping) and appending one
    `... (N more lines)` marker reporting the total dropped.

    Shared by every diff path (Edit via _diff_block, Write/MultiEdit inline)
    so no single tool block can exceed DIFF_BODY_CHAR_BUDGET and shatter."""
    total = len(body)
    kept = body[:max_lines]
    # Char-budget cap: add whole lines while they fit. +1 per join for newline.
    fitted: list[str] = []
    running = 0
    for ln in kept:
        cost = len(ln) + (1 if fitted else 0)
        if running + cost > DIFF_BODY_CHAR_BUDGET:
            break
        fitted.append(ln)
        running += cost
    dropped = total - len(fitted)
    if dropped > 0:
        fitted.append(f"... ({dropped} more lines)")
    return "\n".join(fitted)


def _read_file_safe(path: str) -> str:
    """Read a file, returning '' if missing/binary/too-big.

    Used to reconstruct the "before" side of Write/Edit operations
    when we only have the post-action transcript hook input.
    """
    if not path or not os.path.exists(path):
        return ""
    try:
        size = os.path.getsize(path)
        if size > 1_000_000:  # 1MB cap; bigger files don't get diffed
            return ""
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return ""


def _detect_error(tool_response: dict) -> bool:
    """Heuristic: did this tool call return an error?

    Common error shapes across hook tools:
      - dict with `is_error: True` or non-zero `exit_code`
      - dict whose `output` / `stdout` contains 'error' / 'failed' tokens
    Conservative — only flag confident error signals."""
    if not isinstance(tool_response, dict):
        return False
    if tool_response.get("is_error") is True:
        return True
    code = tool_response.get("exit_code")
    if isinstance(code, int) and code != 0:
        return True
    return False


def _format_tool_block(
    mode: str, tool_name: str, tool_input: dict, tool_response: Any,
    on_failure: bool = False,
) -> str | None:
    """Assemble the per-tool surface block based on mode.

    Returns None when nothing should be appended.

    on_failure=True (the PostToolUseFailure path) short-circuits to a single
    red `- ● Tool(args) FAILED · reason` line — no diff body, because a
    failed Edit/Write never applied, so there's nothing to diff. The reason
    (exit code / timeout / denied) is parsed from the error payload.
    """
    if mode == "off":
        return None

    if on_failure:
        verb, reason = _failure_reason(tool_response)
        # Renders red in every mode (ticker/diffs/collapse/full) — a failure
        # is always worth one line regardless of verbosity.
        return _ticker_line(tool_name, tool_input, errored=True,
                            reason=reason, verb=verb)

    errored = _detect_error(tool_response)
    ticker = _ticker_line(tool_name, tool_input, errored=errored)

    # `ticker` mode: just the one-line per-tool call.
    if mode == "ticker":
        return ticker

    # `collapse` and `diffs` both render ticker + diffs while live; the
    # only difference is that finalize deletes the whole tool message at Stop
    # for `collapse`, while `diffs` keeps it. `full` adds Bash stdout on top.
    diff_body: str | None = None
    summary: str | None = None  # the `  ⎿ [ ... ]` grey line under the header
    if mode in ("collapse", "diffs", "full") and tool_name in ("Edit", "Write", "MultiEdit"):
        path = tool_input.get("file_path") or ""
        if _is_textish_path(path):
            if tool_name == "Edit":
                before = tool_input.get("old_string", "")
                after = tool_input.get("new_string", "")
                diff_body = _diff_block(before, after)
                added, removed = _diff_counts(before, after)
                summary = _summary_line(f"[+{added}, -{removed}]")
            elif tool_name == "Write":
                # For Write we don't have a true "before" from the hook
                # input — the tool_response sometimes carries it, but
                # the safest reconstruction is just to show the new
                # content as +'d lines (no - side).
                #
                # One long line each, no wrap/truncate (matches _diff_block —
                # wrapping butchered content).
                after = tool_input.get("content", "")
                n_added = len(after.splitlines())
                # No per-line `…` truncation — diff lines stay full-length and
                # wrap natively in Discord. Only a pathologically long single
                # line (>MEGA_LINE_MAX) gets an ellipsis, matching the Edit path
                # (_diff_block → _cap_mega_line). _cap_body still bounds
                # line-count + char budget by dropping WHOLE trailing lines
                # (with a "(N more lines)" marker), so the block can't shatter —
                # but that's whole-line dropping, not the mid-line `…`.
                # New file = all-additions; number from 1 to match the
                # line-numbered Edit/MultiEdit format ("+ N content"), numbers
                # right-justified to the line count so content stays aligned.
                # Per-row content budget = TRACE_ROW_MAX − gutter (same as
                # _diff_block) so every row hits the same right edge.
                _src = after.splitlines()
                _w = len(str(len(_src))) if _src else 1
                _cmax = max(8, TRACE_ROW_MAX - 3 - _w)
                lines = [
                    f"+ {str(i).rjust(_w)} "
                    + (ln if len(ln) <= _cmax else ln[:_cmax - 1] + "…")
                    for i, ln in enumerate(_src, 1)
                ]
                diff_body = _cap_body(lines) if lines else None
                summary = _summary_line(f"[+{n_added}, -0]")
            elif tool_name == "MultiEdit":
                edits = tool_input.get("edits", []) or []
                chunks: list[str] = []
                tot_added = tot_removed = 0
                for ed in edits[:5]:  # cap to first 5 edits
                    before = ed.get("old_string", "")
                    after = ed.get("new_string", "")
                    a, r = _diff_counts(before, after)
                    tot_added += a
                    tot_removed += r
                    d = _diff_block(before, after, max_lines=15)
                    if d:
                        chunks.append(d)
                # Count the edits beyond the 5-chunk display cap too.
                for ed in edits[5:]:
                    a, r = _diff_counts(ed.get("old_string", ""), ed.get("new_string", ""))
                    tot_added += a
                    tot_removed += r
                if len(edits) > 5:
                    chunks.append(f"... ({len(edits) - 5} more edits)")
                # Char-budget the combined chunks so a multi-edit with several
                # large hunks can't overflow one Discord message either.
                joined = "\n".join(chunks)
                diff_body = _cap_body(joined.splitlines()) if joined else None
                summary = _summary_line(f"[+{tot_added}, -{tot_removed}]")

    # Read: no diff body, just a `  ⎿ [ N lines ]` summary so the user sees
    # how much was read. Read isn't in the edit branch above, so handle here.
    if mode in ("collapse", "diffs", "full") and tool_name == "Read":
        path = tool_input.get("file_path") or ""
        n = _read_line_count(tool_response)
        if n is not None:
            summary = _summary_line(f"[{n} lines]")

    bash_block: str | None = None
    if mode == "full" and tool_name == "Bash":
        # tool_response shape varies; try to grab stdout
        stdout = ""
        if isinstance(tool_response, dict):
            stdout = tool_response.get("stdout") or tool_response.get("output") or ""
            if not isinstance(stdout, str):
                stdout = str(stdout)
        if stdout.strip():
            lines = stdout.splitlines()
            if len(lines) > 20:
                lines = lines[:20] + [f"... ({len(stdout.splitlines()) - 20} more lines)"]
            # Truncate each line so Discord doesn't wrap them — wrapped lines
            # lose their leading space prefix and render as their own un-styled
            # chunk, breaking visual flow. 88 chars + the 1-space prefix below
            # ≈ 89 cells, right at the ~88-90 card width.
            lines = [(ln[:87] + "…") if len(ln) > 88 else ln for ln in lines]
            redacted = _redact_secrets("\n".join(lines))
            # Prefix every line with a single space so the diff highlighter
            # renders them plain (no green/red), but keeps them within the
            # outer ```diff fence. Triple-backticks inside the body would
            # close the outer fence and break the message — neutralize them
            # the same way the narrate blockquote does (zero-width space splice).
            safe = redacted.replace("```", "`​`​`")
            bash_block = "\n".join(" " + ln for ln in safe.splitlines())

    parts = [ticker]
    if summary is not None:
        parts.append(summary)
    if diff_body is not None:
        # diff_body is already +/- prefixed lines — embed directly
        # (no nested fence; the outer ```diff` fence is the only one)
        safe_diff = diff_body.replace("```", "`​`​`")
        parts.append(safe_diff)
    if bash_block is not None:
        parts.append(bash_block)
    return "\n".join(parts)


def handle_tool(payload: dict, on_failure: bool = False) -> int:
    """PostToolUse(/Failure) entry point. Append the formatted tool block to
    the current narrate placeholder if the channel mode warrants it.

    on_failure=True is the PostToolUseFailure path — renders a red
    `- ● Tool(args) FAILED · reason` line instead of the normal trace.
    """
    transcript_path = payload.get("transcript_path") or ""
    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    # On failure the error text may arrive as tool_response OR tool_error.
    tool_response = (
        payload.get("tool_response")
        or payload.get("tool_error")
        or payload.get("error")
        or {}
    )

    if not transcript_path or not tool_name:
        return 0

    # Don't surface Discord-side tools — they ARE the reply mechanism,
    # so surfacing them would be circular noise.
    if tool_name.startswith("mcp__plugin_discord_discord__"):
        return 0

    # Resolve the Discord origin for this turn — chat_id + message_id from
    # the SAME <channel> tag, mirroring the narrate hook. Keying by message_id
    # (not the user-entry timestamp) is what keeps two interleaved channels
    # from sharing one key and clobbering each other's tool-trace message.
    origin = _current_origin(transcript_path)
    if not origin:
        return 0
    chat_id, message_id = origin

    state_dir = detect_discord_state_dir()
    mode = _channel_mode(state_dir, chat_id)
    if mode == "off":
        return 0

    block = _format_tool_block(mode, tool_name, tool_input, tool_response,
                               on_failure=on_failure)
    if not block:
        return 0

    turn_key = _turn_key_for_message(message_id)
    # turn_ts (for the transcript byte-offset walker) comes from the same
    # user entry the origin was parsed from, so it stays consistent.
    user = _last_user_entry(transcript_path)
    turn_ts = (user.get("timestamp") or user.get("ts") or "") if user else ""

    # Serialize state access — same flock infrastructure the narrate hook uses.
    # Without this, multiple concurrent PostToolUse fires (Bash + Edit
    # within one second) can both create a tool_msg_id and we end up
    # with duplicate tool-trace messages for the same turn.
    with _state_lock():
        return _handle_tool_locked(
            block, chat_id, state_dir, turn_key, turn_ts, transcript_path,
        )


def _handle_tool_locked(
    block: str, chat_id: str, state_dir: str, turn_key: str, turn_ts: str,
    transcript_path: str,
) -> int:
    """Body of handle_tool executed under _state_lock()."""
    state = _load_state()
    turn = _get_turn(state, turn_key)
    if turn is None:
        # No narrate turn yet — initialize the basic turn record so we
        # share keying with narrate. Tool state piggybacks on the same
        # turn entry but uses its OWN msg_id (tool_msg_id) so the
        # narrate placeholder stays pure prose.
        initial_offset = _byte_offset_after_current_user_turn(
            transcript_path, turn_ts
        )
        turn = {
            "chat_id": chat_id,
            "placeholder_msg_id": None,
            "last_byte_offset": initial_offset,
            "buffer": "",
            "mode": "always",
            "finalized": False,
            "replies_at_create": 0,
            "sealed_placeholders": [],
        }
    if turn.get("finalized"):
        return 0

    # Tool-specific state. Lives alongside narrate state on the same
    # turn entry but with its own message id + buffer so neither writer
    # clobbers the other's Discord message.
    turn.setdefault("tool_msg_id", None)
    turn.setdefault("tool_buffer", "")
    turn.setdefault("sealed_tool_messages", [])

    token = read_bot_token(state_dir)
    if not token:
        log(f"no token at {state_dir} — skipping tool surface")
        return 0

    # NO reply-rotation. We used to seal the tool message and start a FRESH one
    # whenever a reply landed mid-turn — but for any bot that does work AFTER
    # replying, that fresh trace posts BELOW the reply, so the bot's output
    # stopped being the last thing in the channel. Keeping the ONE tool message
    # means post-reply tools fold into the trace that's already ABOVE the first
    # reply, so the reply(ies) stay the last messages. The length-cap rotation
    # below still applies — that splits on Discord's 2000-char limit, not on
    # replies.

    # Append block to tool buffer; rotate on Discord cap. The live
    # message carries the agent-view footer (panel), so the length
    # check measures the composed content INCLUDING it — sealed
    # messages drop the footer (it migrates to the next segment).
    panel = turn.get("agent_panel", "")
    candidate_tools = (
        turn["tool_buffer"] + "\n" + block if turn["tool_buffer"] else block
    )
    candidate_content = _tool_message_content(candidate_tools, panel=panel)
    if (
        turn.get("tool_msg_id")
        and len(candidate_content) > DISCORD_LIMIT
    ):
        prev_id = turn["tool_msg_id"]
        prev_buf = turn["tool_buffer"]
        final_content = _tool_message_content(prev_buf, TOOL_PREFIX_FINAL)
        if discord_edit_message(token, chat_id, prev_id, final_content):
            log(f"sealed tool message {prev_id} for {turn_key} (length cap)")
        turn["sealed_tool_messages"].append({"msg_id": prev_id, "buffer": prev_buf})
        turn["tool_msg_id"] = None
        turn["tool_buffer"] = block
    else:
        turn["tool_buffer"] = candidate_tools

    content = _tool_message_content(turn["tool_buffer"], panel=panel)
    if panel and len(content) > DISCORD_LIMIT:
        # A large single block + the agent-view footer can overflow on a
        # FRESH segment, where the rotation check above (gated on an existing
        # tool_msg_id) never fires. Drop the footer rather than let the send
        # hard-slice orphan its closing fence — the agent_view updater re-adds
        # a budget-fitted panel on its next tick.
        log(f"dropped oversized panel footer for {turn_key} "
            f"(block+panel {len(content)}>{DISCORD_LIMIT})")
        content = _tool_message_content(turn["tool_buffer"])
    if turn.get("tool_msg_id"):
        if discord_edit_message(token, chat_id, turn["tool_msg_id"], content):
            log(f"edited tool message for {turn_key}")
    else:
        msg_id = discord_send_message(token, chat_id, content)
        if msg_id:
            turn["tool_msg_id"] = msg_id
            log(f"created tool message {msg_id} for {turn_key}")

    state[turn_key] = turn
    _save_state(state)
    return 0


def handle_tool_prereply(payload: dict) -> int:
    """No-op: the eager tool-trace anchor is disabled.

    It used to pre-post an EMPTY "🔧 Tool trace…" message on every Discord
    reply's PreToolUse so post-reply tool calls would fold in ABOVE the reply.
    The cost: on a reply-only turn (the common case — most conversational
    replies use no tools) that empty anchor was VISIBLE for the whole turn and
    only reaped at Stop — i.e. a tool trace appearing with no tool call. We now
    create the trace LAZILY, on the first real tool's PostToolUse, so
    "no tool ⇒ no trace" holds. The only regression is cosmetic: an echo-first
    turn (reply, THEN tool work) shows its trace just below the reply instead
    of above — the pre-anchor behavior, and far less confusing than a phantom
    trace on every chat reply. _anchor_tool_message_locked + the empty-anchor
    reap in finalize are kept (harmless) so re-enabling is a one-line revert if
    the positioning ever matters more.
    """
    return 0


def _anchor_tool_message_locked(
    chat_id: str, state_dir: str, turn_key: str, turn_ts: str,
    transcript_path: str,
) -> int:
    """Create an empty tool-trace anchor for this turn if one doesn't exist yet.

    Runs under _state_lock(). Posted from the reply tool's PreToolUse, so it
    locks a position ABOVE the upcoming reply. If a tool already ran this turn,
    tool_msg_id is set and it's already above the first reply — leave it alone.
    """
    state = _load_state()
    turn = _get_turn(state, turn_key)
    if turn is None:
        initial_offset = _byte_offset_after_current_user_turn(
            transcript_path, turn_ts
        )
        turn = {
            "chat_id": chat_id,
            "placeholder_msg_id": None,
            "last_byte_offset": initial_offset,
            "buffer": "",
            "mode": "always",
            "finalized": False,
            "replies_at_create": 0,
            "sealed_placeholders": [],
        }
    if turn.get("finalized"):
        return 0
    turn.setdefault("tool_msg_id", None)
    turn.setdefault("tool_buffer", "")
    turn.setdefault("sealed_tool_messages", [])

    # Already have a tool message — it was created on an earlier tool call this
    # turn, which (having run before this reply) is already positioned above the
    # reply. Nothing to anchor.
    if turn.get("tool_msg_id"):
        return 0

    token = read_bot_token(state_dir)
    if not token:
        log(f"no token at {state_dir} — skipping tool anchor")
        return 0

    # Empty buffer → _tool_message_content returns just the live "Tool trace…"
    # prefix. The first post-reply PostToolUse edits this message in place
    # (folding the block in above the reply); finalize reaps it if it stays
    # empty (reply-only turn).
    content = _tool_message_content("")
    msg_id = discord_send_message(token, chat_id, content)
    if msg_id:
        turn["tool_msg_id"] = msg_id
        log(f"anchored empty tool message {msg_id} for {turn_key} (prereply)")
    state[turn_key] = turn
    _save_state(state)
    return 0


def main() -> int:
    # --failure (or --on-failure) flips us into PostToolUseFailure mode: the
    # hook for that event passes the flag so we render a red FAILED/BLOCKED
    # line. (PostToolUse fires only on success, so without a separate failure
    # hook a failed tool call was never surfaced at all.)
    on_failure = any(a in ("--failure", "--on-failure") for a in sys.argv[1:])
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log("bad JSON input")
        return 0
    return handle_tool(payload, on_failure=on_failure)


if __name__ == "__main__":
    sys.exit(main())
