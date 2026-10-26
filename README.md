# claude-code-discord

Mirror a Claude Code session into a Discord channel: live status reactions, narration, tool-call diffs, subagent telemetry, and tap-to-approve permission cards from your phone.

---

## Overview

When running a long task in Claude Code, `claude-code-discord` attaches to the session lifecycle and streams structured updates into a private Discord channel:

- **Turn-state reactions:** A single reaction tracks the turn lifecycle (`👀` received → `🔧` running tools → `✅` complete).
- **Live narration:** Streams the agent's intermediate reasoning and commentary into an in-place blockquote.
- **Tool traces & diffs:** Configurable per channel (`off`, `ticker`, `diffs`, `full`, or `collapse`), rendering colorized unified diffs for file edits and command statuses.
- **Subagent telemetry:** Live status card tracking active and completed subagents, model tiers, elapsed wall time, and token usage.
- **Tap-to-approve permissions (`PreToolUse`):** Holds dangerous tool calls on a fail-closed local boundary and posts an interactive permission card to Discord (`✅` allow / `❌` deny) with a bounded timeout.

## Architecture

| Hook / Component | Lifecycle Event | Responsibility |
| --- | --- | --- |
| `turn-state` | `UserPromptSubmit`, `PreToolUse`, `Stop` | Manages turn-level reaction state on the triggering Discord message |
| `narrate` | `UserPromptSubmit`, `Stop` | Streams and collapses intermediate agent narration |
| `tool-trace` | `PostToolUse`, `PostToolUseFailure` | Formats tool invocations, unified file diffs, and execution errors |
| `approve` | `PreToolUse` | Synchronous fail-closed permission gate backed by Discord reaction relay |

---

© 2026 Jeff Bai. All rights reserved.
