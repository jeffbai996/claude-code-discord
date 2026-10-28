# claude-code-discord

Four Claude Code lifecycle hooks that mirror a live terminal session into a Discord channel: status reactions on the triggering message, streamed between-tool narration, colorized unified diffs and command output, and fail-closed permission prompts you can answer from a phone.

The source is private; this repository holds the README.

## What a turn looks like in Discord

When a Claude Code session takes a message from Discord through the Discord channel plugin, the hooks attach to that message and surface the turn as it runs:

**1. One reaction tracks the turn (`turn-state`).** On `UserPromptSubmit`, `PreToolUse` and `Stop`, the hook removes the previous transient emoji (`0.3 s` spacing between REST calls to avoid Discord 429 rate limits) and applies the current state:

```text
👀 received  ->  🤔 Bash / 🔧 Edit,Write / 🌐 WebSearch / 🤖 Agent  ->  ✅ replied (or 🖥️ terminal / ❌ errored)
```

Read-only tools (`Read`, `Glob`, `Grep`) leave the current reaction in place so a file lookup mid-edit does not flicker the status back to thinking.

**2. Intermediate prose streams into a blockquote (`narrate`).** Assistant commentary between tool calls is read from the session JSONL transcript and edited in place as a `>>>` blockquote:

> 💬 ***Narrating…***
> \> Looking at `src/config.py`. The request timeout is hardcoded at 30 seconds; I'll move it to an environment variable and run the test suite.

Nested triple-backtick fences inside the narration are spliced with zero-width spaces so code blocks cannot break out of Discord's `>>>` quote container. When a narration buffer crosses Discord's 2,000-character message limit, or a Discord reply lands mid-turn, the hook seals the current message and opens a fresh one below it so commentary stays in chronological order.

**3. Tool calls and file edits render as live diffs (`tool-trace`).** On `PostToolUse` and `PostToolUseFailure`, the hook edits a single ````diff` message in place:

```diff
+ ● Read(src/config.py)
   ⎿ [42 lines]
+ ● Edit(src/config.py)
   ⎿ [+2, -1]
- 14 DEBUG = True
+ 14 DEBUG = False
+ 15 TIMEOUT = int(os.environ.get("TIMEOUT", 30))
+ ● Bash(pytest -q)
  4 passed in 0.18s
- ● Bash(./scripts/deploy.sh) FAILED · exit 1
```

Discord's `ansi` code blocks only render color on desktop and show raw escape sequences on mobile. `tool-trace` uses ````diff` instead, which colors column-0 `+` green and `-` red on both desktop and mobile. Diff rows are line-numbered with a right-justified gutter and capped at 84 columns (`1,800` characters or 30 lines per edit, dropping whole trailing lines with `... (N more lines)` rather than wrapping mid-line). In `full` mode, up to 20 lines of `Bash` stdout are appended with 32–256 character token strings redacted to `<REDACTED>`.

**4. Mutating tools wait on a fail-closed permission card (`approve`).** Read-only tools (`Read`, `Grep`, `Glob`, `NotebookRead`, `WebFetch`, `WebSearch`, `TodoWrite`, `TodoRead`) are auto-approved immediately. Any other tool writes a request file to `<state_dir>/rendezvous/<req_id>.req`, posts a card to the channel with `✅` and `❌` reactions, and blocks `PreToolUse` while polling every 50 ms for `<req_id>.resp`:

> 🔐 **Approve `Bash`?**
> `rm -rf ./build`
> ✅ allow   ❌ deny

## Fail-closed approval and the reaction flash

Two design constraints shape the `approve` hook:

- **Claude Code's native hook timeout is fail-open.** A `PreToolUse` hook can block for up to 600 seconds by default, but if the hook process times out or exits non-zero, Claude Code falls through to its normal permission flow and can execute the tool. `claude-code-discord-approve` enforces its own internal deadline (`CCD_APPROVE_TIMEOUT`, default `120` seconds), catches all internal exceptions, and always exits `0` with `"permissionDecision": "deny"` when a tap times out, a response file is missing or corrupt, or posting the card fails.
- **Out-of-band file rendezvous avoids the Discord message flash.** Relaying a reaction tap back into a Claude Code session by posting and deleting a signed channel message causes a visible 100–300 ms message flash in Discord. Writing the tap decision to a local `<req_id>.resp` file that the blocked `PreToolUse` hook polls keeps the approval entirely off the message stream.

The card renderer and reaction state machine (`render_card`, `decision_for_emoji`, `handle_reaction`, `scan_pending`) live in `src/claude_code_discord/bot.py`; a standalone Gateway process to watch reactions and write `<req_id>.resp` is not wired into the CLI yet, so without an external watcher mutating tool calls fail closed after the timeout.

## Hook wiring

`claude-code-discord init` merges four hook scripts into `~/.claude/settings.json` (preserving any existing hooks and unrelated keys):

| Hook executable | Claude Code event | Command wired in `settings.json` | Role |
|---|---|---|---|
| `claude-code-discord-turn-state` | `UserPromptSubmit`, `PreToolUse`, `Stop` | `--mode received`, `--mode working`, `--mode terminal` | Single-emoji turn lifecycle reaction on the inbound Discord message |
| `claude-code-discord-narrate` | `UserPromptSubmit`, `Stop` | `--mode watch`, `--mode finalize` | Streams between-tool assistant prose into an in-place `💬` blockquote |
| `claude-code-discord-tool-trace` | `PostToolUse`, `PostToolUseFailure` | default, `--failure` | Formats tool calls, line-numbered unified diffs, `Bash` stdout and failure reasons |
| `claude-code-discord-approve` | `PreToolUse` | default | Auto-allows read-only tools; blocks mutating tools on a fail-closed local rendezvous |

## Setup

```bash
claude-code-discord init      # prompts for the bot token and channel, merges the hooks, sends a test ping
claude-code-discord status    # shows the wired hooks and whether the token is set
claude-code-discord off       # removes the hooks and keeps the token
```

`init` also takes `--token`, `--channel` and `--no-ping` for non-interactive setup.

## Configuration

All output is opt-in per channel. Channel configuration files live in the Discord plugin state directory (`$DISCORD_STATE_DIR`, else `$CLAUDE_CONFIG_DIR/channels/discord`, else `~/.claude/channels/discord`) and are keyed by Discord channel ID:

- **`narrate.json`** — `{"<channel_id>": "collapse"}`:
  - `never` (default): narration hook is a no-op.
  - `collapse`: streams live `💬 ***Narrating…***` blockquotes during the turn and deletes them at `Stop` once the final reply lands.
  - `always`: keeps the narration in the channel and settles the header to `💬 **Narration**` at `Stop`.
- **`tools.json`** — `{"<channel_id>": "diffs"}`:
  - `off` (default): tool-trace hook is a no-op.
  - `ticker`: one line per tool call (`+ ● Edit(src/config.py)` or `- ● Bash(...) FAILED · exit 1`).
  - `diffs`: ticker plus line-numbered unified diffs for `Edit`, `Write` and `MultiEdit`, and line counts for `Read`.
  - `full`: `diffs` plus up to 20 lines of secret-redacted `Bash` stdout per command.
  - `collapse`: shows `diffs` while the turn is running and deletes the trace message at `Stop`.

Runtime state (`state.json`, `last_origin.json`, `narrate.log` and `rendezvous/`) lives in `$CCD_STATE_DIR` (default `~/.claude/claude-code-discord`).

| Environment variable | Default | Purpose |
|---|---|---|
| `CCD_APPROVE_TIMEOUT` | `120` | Seconds `approve` polls for a tap before returning `"deny"` |
| `CCD_STATE_DIR` | `~/.claude/claude-code-discord` | Runtime state, origin cache and `rendezvous/` directory |
| `CCD_SETTINGS_PATH` | `~/.claude/settings.json` | Target settings file for `init`, `status` and `off` |
| `CCD_LOG` | `$CCD_STATE_DIR/narrate.log` | Hook diagnostic log path |
| `DISCORD_STATE_DIR` | `~/.claude/channels/discord` | Directory containing `.env`, `narrate.json` and `tools.json` |

---

Source code is proprietary.
