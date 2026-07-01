"""Unit tests for claude_code_discord.hooks.tool_trace.

No network calls — discord_send_message / discord_edit_message are NEVER
exercised here. Every test drives a PURE formatter (_diff_block, _diff_counts,
_ticker_line, _redact_secrets, _tool_message_content) or main()'s stdin/JSON
contract, which short-circuits before any Discord I/O on empty/malformed input.
"""

from __future__ import annotations

import io
import json

import pytest

from claude_code_discord.hooks import tool_trace


# ---------------------------------------------------------------------------
# _diff_block — the headline renderer
# ---------------------------------------------------------------------------


def test_diff_block_has_plus_and_minus_lines():
    """A one-line change renders both a removed (-) and added (+) row."""
    before = "alpha\nbravo\ncharlie\n"
    after = "alpha\nBRAVO\ncharlie\n"
    out = tool_trace._diff_block(before, after)
    assert out is not None
    lines = out.splitlines()
    # The changed line shows up as a - (old) and a + (new). Marker is at col 0.
    assert any(ln.startswith("-") and "bravo" in ln for ln in lines)
    assert any(ln.startswith("+") and "BRAVO" in ln for ln in lines)


def test_diff_block_no_change_returns_none():
    """Identical before/after → nothing to render."""
    assert tool_trace._diff_block("same\ntext", "same\ntext") is None


def test_diff_block_respects_max_lines_cap():
    """A big change past max_lines drops whole trailing lines with a marker,
    and never emits more body rows than the cap (+ the marker line)."""
    before = ""
    after = "\n".join(f"line {i}" for i in range(100))
    out = tool_trace._diff_block(before, after, max_lines=5)
    assert out is not None
    lines = out.splitlines()
    # 5 kept rows + 1 "(N more lines)" marker at most.
    assert len(lines) <= 6
    assert any("more lines" in ln for ln in lines)


def test_diff_block_lines_are_numbered():
    """Rows carry a right-justified line number in the gutter (`+ 2 ...`)."""
    before = "one\ntwo\nthree"
    after = "one\nTWO\nthree"
    out = tool_trace._diff_block(before, after)
    assert out is not None
    # The added line for "TWO" should carry its new-side line number (2).
    plus_rows = [ln for ln in out.splitlines() if ln.startswith("+")]
    assert plus_rows
    assert any(" 2 " in ln for ln in plus_rows)


# ---------------------------------------------------------------------------
# _diff_counts
# ---------------------------------------------------------------------------


def test_diff_counts_pure_addition():
    """Adding lines to an empty file counts N additions, 0 deletions."""
    added, removed = tool_trace._diff_counts("", "a\nb\nc")
    assert (added, removed) == (3, 0)


def test_diff_counts_replacement():
    """Replacing one line counts one addition and one deletion."""
    added, removed = tool_trace._diff_counts("keep\nold\nkeep", "keep\nnew\nkeep")
    assert (added, removed) == (1, 1)


def test_diff_counts_no_change():
    assert tool_trace._diff_counts("x\ny", "x\ny") == (0, 0)


# ---------------------------------------------------------------------------
# _ticker_line / _tool_message_content — the one-liner
# ---------------------------------------------------------------------------


def test_ticker_line_edit_shows_path():
    """An Edit ticker one-liner carries the tool name, the file path digest,
    and the tool-invocation dot marker; it renders green (leading +)."""
    line = tool_trace._ticker_line("Edit", {"file_path": "src/app.py"})
    assert line.startswith("+ ● ")
    assert "Edit(" in line
    assert "src/app.py" in line


def test_ticker_line_errored_is_red_with_reason():
    """An errored call renders red (leading -) with a FAILED verb + reason."""
    line = tool_trace._ticker_line(
        "Bash", {"command": "false"}, errored=True, reason="exit 1", verb="FAILED"
    )
    assert line.startswith("- ● ")
    assert "FAILED" in line
    assert "exit 1" in line


def test_ticker_line_never_exceeds_header_cap():
    """A very long digest is budgeted so the whole rendered line stays capped."""
    line = tool_trace._ticker_line("Edit", {"file_path": "x/" + "y" * 300})
    assert len(line) <= tool_trace.HEADER_LINE_MAX


def test_tool_message_content_edit_payload_end_to_end():
    """A full Edit block (ticker + summary + numbered diff) renders a sensible
    trace: the file path, a +N/-N summary, and the ```diff fence."""
    block = tool_trace._format_tool_block(
        "diffs", "Edit",
        {"file_path": "mod.py", "old_string": "x = 1", "new_string": "x = 2"},
        {},
    )
    assert block is not None
    content = tool_trace._tool_message_content(block)
    assert "```diff" in content
    assert "mod.py" in content
    # +1, -1 summary line for the single-line replacement.
    assert "[+1, -1]" in content


# ---------------------------------------------------------------------------
# _redact_secrets
# ---------------------------------------------------------------------------


def test_redact_secrets_masks_long_token():
    """A long credential-shaped run is replaced with <REDACTED>."""
    fake_token = "sk_live_" + "A1b2C3d4" * 6  # 56 chars, > the 32-char floor
    out = tool_trace._redact_secrets(f"export TOKEN={fake_token}")
    assert fake_token not in out
    assert "<REDACTED>" in out


def test_redact_secrets_leaves_short_words_alone():
    """Ordinary short words aren't credential-shaped and stay intact."""
    text = "the quick brown fox jumps"
    assert tool_trace._redact_secrets(text) == text


# ---------------------------------------------------------------------------
# main() — stdin/JSON contract (no network on these paths)
# ---------------------------------------------------------------------------


def test_main_empty_stdin_returns_zero(monkeypatch):
    """Empty stdin → clean exit 0, no crash, no Discord call."""
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert tool_trace.main() == 0


def test_main_whitespace_only_stdin_returns_zero(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("   \n  "))
    assert tool_trace.main() == 0


def test_main_malformed_json_returns_zero(monkeypatch):
    """Malformed JSON is swallowed (logged) and returns 0, never raises."""
    monkeypatch.setattr("sys.stdin", io.StringIO("{not valid json"))
    assert tool_trace.main() == 0


def test_main_payload_without_transcript_returns_zero(monkeypatch):
    """A well-formed payload missing transcript_path/tool_name short-circuits
    in handle_tool BEFORE any origin lookup or Discord I/O."""
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"foo": "bar"})))
    assert tool_trace.main() == 0


def test_main_discord_tool_is_skipped(monkeypatch):
    """A Discord-side tool call is never surfaced (circular), returns 0 with
    no network — proves the mcp__plugin_discord_discord__ guard fires."""
    payload = {
        "transcript_path": "/nonexistent/transcript.jsonl",
        "tool_name": "mcp__plugin_discord_discord__reply",
        "tool_input": {},
        "tool_response": {},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert tool_trace.main() == 0


def test_failure_flag_renders_red_block(monkeypatch):
    """--failure mode short-circuits _format_tool_block to a single red line."""
    block = tool_trace._format_tool_block(
        "diffs", "Bash", {"command": "false"},
        "Exit code 1\nsomething broke", on_failure=True,
    )
    assert block is not None
    assert block.startswith("- ● ")
    assert "FAILED" in block
    assert "exit 1" in block
