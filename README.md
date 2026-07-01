# claude-code-discord

**Pipe your Claude Code session into Discord — watch it work, see the diffs, and approve tools from your phone.**

Run a long task in Claude Code, step away from the terminal, and keep a live view in a Discord channel: its narration, its tool calls (with full diffs), its turn status — and get pinged when it needs you or finishes. Tap ✅ to approve a permission from your couch. Runs on your own box, over LAN or a private tunnel. Never the public internet.

> **Status: early alpha.** This extracts and sharpens the observability layer from an earlier multi-agent kit ([cc-discord-kit](https://github.com/jeffbai996/cc-discord-kit), now archived) into a small, single-purpose, one-command tool.

---

## What it looks like

Your agent is running a task in the terminal. Here's the same turn, in your Discord channel:

**It reacts to your message as it works** — one emoji tracks the whole turn:

> 👀 → 🔧 → ✅  *(got it → working → done)*

**It narrates** — the prose it'd normally only print to the terminal shows up live:

> 🧠 ***Narrating…***
> \> Looking at the config now. The timeout's hardcoded — I'll pull it into an env var and drop the debug flag while I'm here.

**It shows its tool calls** — from a one-line ticker up to full colorized diffs:

```diff
+ ● Edit(src/config.py)
  ⎿ [+3, -1]
- DEBUG = True
+ DEBUG = False
+ TIMEOUT = int(os.environ.get("TIMEOUT", 30))
+ ● Bash(npm test)
- ● Bash(npm run deploy) FAILED
```

**It shows its subagents** — when the agent fans work out, a live panel rides the bottom of the trace, edited in place every few seconds:

```
◓ agents · 2 running · 1 done · 16.5k tok

  ◉  research bear case   sonnet  1m12s  12.3k
  ◉  audit api handlers   sonnet  1m12s   3.9k
  ●  verify margin math   haiku     31s   4.2k
```

**And you can approve from your phone** — when Claude hits a permission prompt, you get pinged and can tap to decide:

> 🔐 **Permission needed** — `Bash(rm -rf ./build)`
> ✅ approve   ❌ deny

Tap ✅ and the tool runs; tap ❌ and it's blocked — without walking back to the terminal.

All of it is **opt-in** and **off by default**. Pick how much you want to see — silent, just status emoji, narration, or full diffs.

---

## Install

```bash
uvx claude-code-discord init
```

Walks you through a Discord bot token + channel, merges the hooks into your `~/.claude/settings.json` (it merges, never clobbers), and sends a test ping. Then just use Claude Code normally — it shows up in Discord.

Prefer pipx? `pipx install claude-code-discord && claude-code-discord init`.

---

## How it works

Three Claude Code hooks do the surfacing, opt-in per project:

| Hook | Claude Code event | What it surfaces |
| --- | --- | --- |
| **tool-trace** | `PostToolUse` | the actual tool calls — one-line ticker → full diffs → command output |
| **turn-state** | `UserPromptSubmit` / `Stop` | one emoji reaction tracking the turn: 👀 → 🔧 → ✅ |
| **narration** | (streamed) | the agent's between-tool prose, live as a 🧠 blockquote |
| **approve** | `PreToolUse` | pings you on a permission prompt; your tap resolves it `allow`/`deny` |

**Tap-to-approve** works because a `PreToolUse` hook can return `permissionDecision: allow` and blocks the session while it waits (up to a configurable timeout) for your Discord tap. See [docs/tap-to-approve.md](docs/tap-to-approve.md) for the design (and the fail-closed timeout handling that keeps a missed tap from silently running the tool).

---

## License

MIT — see [LICENSE](LICENSE).
