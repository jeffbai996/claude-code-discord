# claude-code-discord

Claude Code hooks that mirror a terminal session into a Discord channel. A reaction on your message tracks the turn, the commentary between tool calls appears in the channel, edits show up as diffs, and any tool that changes something waits for a tap on your phone.

The source is private. This repository holds only the README.

## A turn in Discord

When a session takes a message from Discord through Claude Code's Discord channel plugin, the hooks attach to that message and show the turn as it runs.

**Status.** One reaction on your message follows the turn. It starts as 👀 and changes with the kind of tool running. It ends on ✅ if a reply went out, 🖥️ if the turn finished without a reply in Discord, or ❌ if a tool failed and nothing was sent.

```text
👀 received  ->  🤔 Bash / 🔧 Edit,Write / 🌐 WebSearch / 🤖 Agent  ->  ✅ replied (or 🖥️ terminal / ❌ errored)
```

Read, Glob and Grep leave the reaction alone, so a quick lookup in the middle of an edit does not flip the status back to thinking.

**Narration.** The text Claude writes between tool calls is read from the session transcript and shown in a quoted block that is edited in place:

> 💬 ***Narrating…***
> \> Looking at `src/config.py`. The request timeout is hardcoded at 30 seconds; I'll move it to an environment variable and run the test suite.

Triple backticks inside the narration are broken up with zero-width spaces, so a code block cannot escape the quote. When the text reaches Discord's 2,000-character limit, or a reply lands mid-turn, the hook closes that message and starts a new one below it. That keeps the commentary in the order it happened.

**Tool calls and diffs.** Each tool call is a line in one message, edited as the turn goes on. Edits appear as diffs:

```diff
+ ● Read(src/config.py)
   ⎿ [42 lines]
+ ● Edit(src/config.py)
   ⎿ [+2, -1]
- 14 DEBUG = True
+ 14 DEBUG = False
+ 15 TIMEOUT = int(os.environ.get("TIMEOUT", 30))
- ● Bash(./scripts/deploy.sh) FAILED · exit 1
```

Discord colours `ansi` blocks only on desktop, so the hook uses `diff` blocks. They colour a leading `+` green and `-` red on desktop and mobile alike. A message has to fit in 2,000 characters and can only be split between blocks, so one edit is capped at 30 lines or 1,800 characters. Whole trailing lines are dropped with a `... (N more lines)` note, and lines are never wrapped.

**Approval.** Read-only tools (Read, Grep, Glob, NotebookRead, WebFetch, WebSearch, TodoWrite and TodoRead) go through at once. Every other tool, including ones the hook has never seen, writes a request file and posts a card with ✅ and ❌ reactions. The tool call stays blocked until someone taps:

> 🔐 **Approve `Bash`?**
> ```
> rm -rf ./build
> ```
> ✅ allow   ❌ deny

## Why approval fails closed

Claude Code's own hook timeout fails open. If a hook is killed at the timeout, the tool call falls through to the normal permission flow and can run. Leaving the decision to that timeout would turn a missed tap into an approval.

So the approve hook keeps its own deadline, 120 seconds by default and well under Claude Code's 600. No tap, a missing or corrupt answer, or any internal error ends in an explicit deny. The hook always prints a decision and exits 0. Exiting without one would hand the call back to the normal permission flow.

The answer comes back through a local file, not a Discord message. Carrying a tap back by posting and deleting a message makes it flash in the channel. Writing the decision to a file that the waiting hook polls keeps approval off the message stream.

The card builder and the reaction handler are written, but the bot process that watches Discord for taps and writes the answer file is not wired into the command-line tool yet. A card is also only posted for a turn that came from Discord. Until then, a tool call that needs approval waits out the deadline and is denied.

## Setup

```bash
claude-code-discord init      # asks for the bot token and channel, adds the hooks, sends a test message
claude-code-discord status    # lists the wired hooks and whether a token is set
claude-code-discord off       # removes the hooks and keeps the token
```

`init` adds its hooks to `~/.claude/settings.json` and leaves your other hooks and settings alone. It also takes `--token`, `--channel` and `--no-ping` for a non-interactive install.

## Configuration

Narration and tool output are off until you turn them on for a channel. The settings are two JSON files, keyed by Discord channel ID, in the Discord plugin's state directory. That is `$DISCORD_STATE_DIR`, else `$CLAUDE_CONFIG_DIR/channels/discord`, else `~/.claude/channels/discord`.

`narrate.json`, as in `{"<channel_id>": "collapse"}`:

- `never` (default): no narration.
- `collapse`: show the live block during the turn and delete it once the final reply lands.
- `always`: keep it, and change the header to `💬 **Narration**` at the end.

`tools.json`, as in `{"<channel_id>": "diffs"}`:

- `off` (default): no trace.
- `ticker`: one line per tool call.
- `diffs`: the ticker plus line-numbered diffs for `Edit`, `Write` and `MultiEdit`, and line counts for `Read`.
- `full`: `diffs` plus up to 20 lines of Bash output, with long token-like strings replaced by `<REDACTED>`.
- `collapse`: the same as `diffs` while the turn runs, then the message is deleted.

| Environment variable | Default | What it sets |
|---|---|---|
| `CCD_APPROVE_TIMEOUT` | `120` | Seconds the approve hook waits for a tap before denying |
| `CCD_STATE_DIR` | `~/.claude/claude-code-discord` | Runtime state: `state.json`, `last_origin.json`, `narrate.log` and `rendezvous/` |
| `CCD_SETTINGS_PATH` | `~/.claude/settings.json` | The settings file that `init`, `status` and `off` edit |
| `CCD_LOG` | `$CCD_STATE_DIR/narrate.log` | The hook log |
| `DISCORD_STATE_DIR` | `~/.claude/channels/discord` | The directory with `.env`, `narrate.json` and `tools.json` |

## License

Source code is proprietary.
