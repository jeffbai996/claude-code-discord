# Tap-to-approve — design

Native "approve a Claude Code tool call from Discord" is architecturally
supported. This documents the mechanism and the one non-obvious safety rule.

## Mechanism (verified against `code.claude.com/docs/en/hooks.md`)

A **`PreToolUse` hook** returns a decision:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow" | "deny" | "ask",
    "permissionDecisionReason": "approved via Discord tap"
  }
}
```

- `allow` — skips the permission prompt, tool runs.
- `deny` — blocks the tool, reason shown to Claude.
- `ask` — falls back to the normal terminal confirmation.

**Hooks block the session synchronously by default**, up to a per-hook
`timeout` (default 600s for `command`/`http`/`mcp_tool` hooks, configurable).
So the flow is:

1. `PreToolUse` fires. The hook posts a Discord card: `🔐 Bash(rm -rf ./build)` with ✅/❌.
2. The hook **blocks**, polling a local rendezvous (file / unix socket / a tiny
   local HTTP endpoint the bot writes to) for the tap result.
3. The user taps. The Discord bot writes `allow`/`deny` to the rendezvous.
4. The hook reads it, returns the matching `permissionDecision`, unblocks the turn.

The "session freezes while waiting for you" is the intended behavior, not a bug.

## The one critical safety rule: FAIL CLOSED

Claude Code's hook `timeout` is **fail-OPEN**: if the hook process is killed at
timeout, the tool call **falls through to the normal permission flow and can
proceed**. So we must NOT rely on the CC timeout to deny.

**The poller defaults to `deny` at its own window, well under the CC timeout.**
E.g. poll for 120s then return `deny` — never let the 600s CC timeout be the
thing that decides, because that decision is "run the tool." A tap that never
comes must resolve to *blocked*, not *executed*.

## Two build targets

- **(a) Blocking `PreToolUse` hook** — works in normal interactive terminal
  sessions. This is the MVP target (the audience runs Claude Code in a terminal).
- **(b) `defer` + `claude -p --resume`** — the purpose-built async-permission
  primitive, but scoped to headless (`claude -p`) sessions. Cleaner (no held
  connection, no fail-open risk). Offer for the headless-agent crowd; not the
  default MVP path.

## Open implementation questions

- Rendezvous transport: file-poll (simplest, portable) vs unix socket (lower
  latency) vs local HTTP (cleanest if the bot is a long-running server). Start
  with file-poll for the MVP.
- How the hook and the Discord bot share the tool-call id → tap mapping.
- Per-tool policy: auto-allow read-only tools, only prompt for writes/Bash?
  (A allowlist so you're not tapping ✅ on every `Read`.)
