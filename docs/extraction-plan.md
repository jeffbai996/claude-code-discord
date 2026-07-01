# Extraction plan — porting the observability layer from cc-discord-kit

The ccdk hooks are extractable but NOT independent files — `tool_watcher.py`
imports ~19 names from `narrate.py`, so there's a shared core tangled inside
narrate. The clean-room version separates that core out first.

## Coupling map (measured from ccdk `origin/main~1`)

`tool_watcher.py` (689 LOC) imports from `narrate.py`:
- **Discord I/O:** `discord_send_message` (~9 LOC), `discord_edit_message` (~6), `read_bot_token`
- **State:** `_load_state` (~8), `_save_state` (~11), `_state_lock`
- **Rendering:** `_blockquote` (~47), `DISCORD_LIMIT`, `NARRATE_PREFIX_AUTO`
- **Turn tracking:** `_get_turn`, `_turn_key`, `_byte_offset_after_current_user_turn`, `_seal_segment`, `_last_user_entry`, `_extract_user_text`, `count_discord_replies`
- **Discovery:** `detect_discord_state_dir`, `parse_discord_origins`

Note: `read_bot_token`, `detect_discord_state_dir`, `parse_discord_origins` are
NOT module-level in narrate — trace where narrate gets them (imported from a
sibling, or defined nested). Do this trace before porting.

## Target architecture

```
src/claude_code_discord/
  _core.py        # shared: Discord REST I/O, JSON state (load/save/lock),
                  #   turn tracking, blockquote/limit, state-dir discovery,
                  #   bot-token read. ~200 LOC. The de-tangled core.
  hooks/
    tool_trace.py # PostToolUse — the diff/trace renderer (was tool_watcher, 689 LOC)
    turn_state.py # UserPromptSubmit/Stop — emoji reactions (was react_hook)
    narrate.py    # narration — SLIMMED from 3042 LOC (drop squad-tuned
                  #   collapse/settle bulk; keep the core narration path)
    approve.py    # PreToolUse — tap-to-approve (NEW, see tap-to-approve.md)
  cli.py          # init/status/off (stub exists)
```

## Port order (each step independently testable)

1. **`_core.py` first.** Port the ~9 core primitives, rename `CCDK_*` env vars →
   `CCD_*`, strip the one log-path coupling. Unit-test the state round-trip and
   the blockquote renderer in isolation (no Discord needed).
2. **`tool_trace.py`** — port tool_watcher on top of `_core`. Its diff renderer
   (`_diff_block` / `_tool_message_content`) is the crown jewel and is already
   clean (the alignment logic verified 2026-07-01). Golden-test the diff output.
3. **`turn_state.py`** — port react_hook (only 2 squad refs to strip).
4. **`narrate.py`** — the diet. Port the core narration path; drop the
   think-collapse/settle machinery unless it proves needed. Biggest judgment call.
5. **`approve.py`** — build fresh per tap-to-approve.md. The headline feature.
6. **`cli.py init`** — the one-command setup that writes the hook block into
   `~/.claude/settings.json` (merge, never clobber).

## First slice to ship
`_core.py` + `tool_trace.py` + `cli.py init` = "see your tool calls + diffs in
Discord, one command." That alone is demoable. turn_state, narrate, approve
layer on after.
