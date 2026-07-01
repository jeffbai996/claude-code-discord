# claude-code-discord — product scope

## The one-liner
**Pipe your Claude Code session into Discord. Watch it work, see the diffs, get pinged on your phone when it needs you or finishes — in one command.**

## Who it's for
Any Claude Code user who runs long tasks and wants to step away from the terminal without going blind. Solo devs, people running agents on a remote box / VPS, anyone who's ever `tmux`'d into a session from their phone to check on a build. That's a *far* bigger audience than "self-host my multi-agent household," which is why this is the extraction worth making.

## What it does (the demo — this is what sells it)
The whole value prop is already proven in the ccdk demo, and it's genuinely good:
1. **Emoji turn-state** — one reaction on your message tracks the whole turn: 👀 got it → 🔧 working → ✅ done (or ❌ failed / ⏸ waiting on you).
2. **Live narration** — the prose Claude prints between tool calls, surfaced as a 🧠 blockquote as it thinks.
3. **Tool-trace with diffs** — every tool call rendered live: one-line ticker → full colorized `+/-` diffs → command output. The `tool_watcher.py` renderer (689 LOC, zero squad-coupling) is the crown jewel.
4. **Subagent panel** — when Claude fans out subagents, a live panel rides the bottom edited in place: spinner pulses, dots blink ○⇄◉ then fill solid as agents land, tokens + elapsed from real transcripts.
5. **Permission pings** — when Claude hits a permission prompt, you get pinged (read-only mirror; you still approve at the terminal — or see #new-ideas below).
6. **Command-back** — type `!ls` / `!git log` in the channel, it runs on the host, replies inline, zero token cost. Plus the `!` live-terminal-pane (one pinned message = a real scrollback).

## What to EXTRACT (keep)
| Component | LOC | Coupling | Notes |
|-----------|-----|----------|-------|
| `tool_watcher.py` | 689 | **0** | The diff/trace renderer. Ships nearly as-is. |
| `react_hook.py` | 1463 | 2 refs | Emoji turn-state. Trivial de-coupling. |
| `narrate.py` | 3042 | 1 ref | Narration. **Needs a diet** — it's the god-file; a lot of its bulk is squad-tuned collapse/settle logic. Target ~800-1000 LOC for the generic version. |
| `discord_passthrough.py` | 1071 | some | `!cmd` + `!` terminal pane. Keep, strip fleet bits. |
| `notify_hook.py` | 271 | low | Permission-prompt ping. Keep. |
| agent-view (`agent_view.py`) | — | some | The subagent panel. Keep — it's a headline demo feature. |

## What to CUT (the squad-specific plumbing nobody wants)
- The **memory/journal/files store** (`store.py`, `cli.py`, `client.py`, `files_store.py`) — crowded space, not the draw.
- The **Flask web UI** (`server.py`, templates/, static/) — huge surface, not needed for the core value.
- **Fleet management** (`bot_config.py`, `bot_admin.py`, `bots_doctor.py`, `capabilities.py`, `new_bot.py`, `agents.yaml`) — this is "run my 6-bot household," pure squad.
- **Personas, digest, inventory, merger, relay** — all squad-ops.
- The **slash-command bot** (`discord_handler.py`) — optional/later; the hooks alone deliver the value.

## The install flow (this is make-or-break)
The current ccdk install is a multi-step `settings.json` + `agents.yaml` + tailscale ordeal. For adoption this has to be **one command**:
```
npx claude-code-discord init
```
which: (1) prompts for a Discord bot token (or walks you through making one), (2) asks which channel, (3) writes the hooks into your `~/.claude/settings.json` (merging, not clobbering), (4) verifies with a test ping. Then you just use Claude Code normally and it shows up in Discord. `--off` / per-project toggles for when you don't want it.

Distribution options: npm (`npx`, matches Claude Code's ecosystem) or a `uv`/pipx Python tool. Given the hooks are Python, a `pipx install claude-code-discord` + a tiny `init` CLI is probably the honest fit, but an `npx` wrapper reads as more "Claude Code native." **Decision needed.**

---

## NEW IDEAS — what else to include (beyond what ccdk has today)

These are the things that would make it a *product* people star, not just an extraction:

### High-value, natural fits
1. **Two-way approve from Discord.** Today permission prompts are read-only (you still walk to the terminal). Let the user **tap ✅/❌ on the Discord ping to approve/deny the tool call** — the tap relays back to the session. This is the single biggest UX win; the ccdk tap-card relay already proves the mechanism exists. *This alone could be the headline feature.*
2. **Reply-to-steer.** Type a message in the channel mid-turn and it's injected as a steering message / interrupt into the session (like typing in the terminal). "actually use the other API" from your phone. Huge for long agent runs.
3. **@mention on done / on-blocked.** Configurable ping-your-phone triggers: when the turn finishes, when it's been waiting on a permission for >30s, when a long Bash command errors. The "walk away and get pulled back only when needed" loop.
4. **Session picker / multi-session.** If you run Claude Code in 3 repos, route each to its own channel (or thread). One bot, many sessions, clean separation.

### Medium-value / differentiators
5. **Cost + token meter.** A live per-turn / per-session token + $ counter in the status line (transcripts already carry the numbers — the subagent panel already reads them). Developers love a cost readout.
6. **Diff reactions → GitHub.** React to a tool-trace diff to, e.g., copy it, or open it as a gist/PR draft. Speculative, but "act on what you see" is sticky.
7. **Thread-per-task.** Long multi-subagent runs get their own Discord thread so the main channel stays clean; the subagent panel lives in the thread.
8. **Mobile-first formatting mode.** A denser render tuned for phone screens (the current one is tuned for the ccdk card width) — narrower diffs, collapsible trace.

### Lower priority / later
9. **Slack adapter.** The store layer was already transport-agnostic in ccdk; the same "surface a CC session into a chat app" could target Slack/Telegram. But nail Discord first.
10. **Voice mode.** ccdk has TTS/STT surfacing (gem-voice etc.) — reading the narration aloud, voice replies. Neat, but heavy; a v2 flourish.
11. **Web dashboard (opt-in).** A stripped `server.py` as an optional local dashboard for people who want a browser view too. Only if demand appears — it's the surface we're cutting for a reason.

### The sharpest MVP
If the goal is "ship something people actually adopt," the **minimum lovable product** is:
**tool-trace diffs + emoji turn-state + narration + @mention-on-done + tap-to-approve, installed in one command.** Ideas 1 and 3 (tap-approve, ping-on-done) are what turn it from "cool demo" into "I leave this on for every session." Everything else is roadmap.

### Tap-to-approve — CONFIRMED buildable, goes in MVP (verified 2026-07-01)
Native tap-to-approve (tap ✅ in Discord → Claude Code's permission dialog resolves `allow`) is architecturally supported, no hacks:
- A **`PreToolUse` hook** returns `hookSpecificOutput.permissionDecision: "allow" | "deny" | "ask"`. `allow` skips the prompt and runs the tool; `deny` blocks it.
- **Hooks block the session synchronously by default**, up to a **600s configurable `timeout`**. So the hook holds the turn, waits for the Discord tap to arrive (via a local file / socket / a small local HTTP endpoint the bot writes to), then returns the decision. The "freeze while waiting for remote approval" is the intended mechanism, not a side effect.
- **CRITICAL SECURITY DETAIL — timeout is fail-OPEN.** If the hook hits Claude Code's 600s timeout it's killed and the tool call **falls through to the normal permission flow (proceeds)**. So the poller MUST internally default to `deny` at some window well under 600s — otherwise a missed/ignored approval silently *runs* the tool. Fail-closed by our own logic, never rely on the CC timeout.
- Two builds: **(a) blocking PreToolUse hook** — works in normal interactive terminal sessions, the right fit for the target audience. **(b) `defer` + `claude -p --resume`** — cleaner (no held connection, no fail-open risk) but headless-only. Ship (a) for the MVP; (b) is a nice option for people running headless agents.
- Reference: `code.claude.com/docs/en/hooks.md` — "PreToolUse decision control," "Defer a tool call for later," timeout table.

So tap-to-approve is **MVP, not v1.1.** It's the headline feature and it's real. Tap-to-steer (inject a message) is a trivial bonus on the same relay.

---

## Open decisions for Jeff
1. **Runtime/distribution:** Python+pipx (honest, hooks are Python) vs npx wrapper (reads native)? 
2. **Name:** `claude-code-discord` is descriptive/SEO-friendly. Alternatives? (something punchier might market better, but descriptive wins on discoverability.)
3. **MVP scope:** the "sharpest MVP" above, or start even smaller (just tool-trace + emoji, ship, then iterate)?
4. **Tap-to-approve (idea #1):** in the MVP or v1.1? It's the best feature but also the most engineering (secure relay back into the session).
5. **License:** MIT (permissive, max adoption) presumably — same as ccdk.
