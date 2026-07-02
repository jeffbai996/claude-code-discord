# Plan for B — the live 🤔 think-indicator (deferred feature)

**Status:** deferred from the MVP. The MVP ships feature A (the 🧠 narration
blockquote). This is the plan to greenlight B separately.

## What B is
While Claude is in extended thinking (before it emits any visible output), post a
live "🤔 Thinking…" indicator in Discord that *animates* — a pulsing glyph + dots
+ an elapsed timer — and then **settles** when the turn produces output: it either
collapses into a one-line summary ("🤔 thought for 12s") or is cleared. It's the
"something is happening, it hasn't hung" signal for the dead air before the first
token.

## Why it's not a straight port
The origin implementation is **~1325 LOC across 22 functions**, and the large
majority of that is solving a problem the single-user tool does not have:

- **`run_think_updater` (659 LOC)** is a *detached child process* re-exec'd per
  turn. It heartbeats a state file so other processes know the indicator is alive,
  and self-terminates on a bunch of signals (turn moved on, output arrived, Stop
  fired, hard timeout).
- **The multi-bot machinery (cut-worthy):** `_other_live_think_indicator`,
  `_claim_think_swap`/`_release_think_claim` (cross-process claim on a channel so
  two bots don't both post "Thinking…"), the orphan-reaping
  (`_reap_orphaned_think_deletes`, `_safety_net_settle_think`), the
  holder-staleness heartbeat logic. All of this exists because **N squad bots share
  one channel** and a crashed bot's orphaned indicator once black-holed a channel
  for 12 hours. A single-user tool has one session, one channel — none of this
  applies.
- **Squad settings coupling:** `extended_thinking_settings` reads per-bot
  `settings.json` to detect xhigh/thinking mode. The standalone tool reads its own
  config or the transcript.

Strip the multi-bot survival machinery and the actual *feature* is small — maybe
**150–250 LOC**. The 659-LOC updater is mostly defensive coordination, not the
animation.

## Proposed single-user build (the lean rebuild, not a port)

Rather than port-and-diet the 659-LOC updater, **rebuild the feature clean** on top
of `_core`, reusing the same primitives the other hooks use:

1. **Trigger.** A `PreToolUse`/`UserPromptSubmit`-adjacent hook (or the existing
   narrate `watch` entry) detects "extended thinking is active and no output yet."
   Simplest signal: the transcript shows a thinking block open with no assistant
   text/tool-use after it.
2. **The animator.** One short-lived background process (spawned by the hook, like
   the origin) that, every ~2s: edits a single Discord message via
   `_core.discord_edit_message` cycling the glyph (`_think_glyph`), dots
   (`_think_dots`), and an elapsed counter (`_fmt_think_duration`). These 4 tiny
   helpers (`_think_glyph`/`_think_dots`/`_think_phrase`/`_fmt_think_duration`,
   ~33 LOC total) port verbatim — they're pure formatters, zero coupling.
3. **Settle.** When the turn produces its first output (the narrate `watch` hook
   already detects new text/tool blocks — reuse that signal), the animator stops
   and either edits the message to a one-line "🤔 thought for Ns" summary
   (`_think_summary`, `_fmt_think_duration`) or deletes it. Single owner, single
   channel → no claim/orphan logic needed.
4. **Safety timeout.** A hard cap (e.g. 10 min) so a crashed session can't leave a
   spinning indicator. One `time`-based check in the animator loop — replaces the
   entire squad orphan-reaping subsystem.

**Reused verbatim (pure formatters, ~33 LOC):** `_think_glyph`, `_think_dots`,
`_think_phrase`, `_fmt_think_duration`, `_think_summary`.
**Rebuilt clean (~120–180 LOC):** the trigger detection, the animator loop, the
settle-on-first-output.
**Dropped entirely:** `run_think_updater`'s coordination bulk, all `_claim/_release`,
`_other_live_think_indicator`, orphan-reaping, `_safety_net_settle_think`,
`handle_think_prereply`'s squad anchor logic.

**Estimate:** ~200 LOC + tests. A focused build, not a slog — most of the origin's
size is multi-bot defense that evaporates for single-user.

## Open questions for the greenlight
1. **Is the trigger reliable in a plain terminal session?** Need to confirm the
   transcript exposes "thinking started, no output yet" cleanly for interactive
   (non-`--channels`) sessions. Worth a quick spike before committing.
2. **Spawn model.** The origin re-exec's a detached Python child. Cleaner options
   for a standalone tool: a thread, or a tiny asyncio task if the bot side is a
   long-running server. Decide with the tap-to-approve rendezvous (same "background
   worker that edits Discord" shape).
3. **Config:** always-on when thinking, or gated by the same per-channel
   narrate mode (auto/always/never) as feature A? (Origin made it ALWAYS show, even
   in `never`-narrate channels — a deliberate choice worth revisiting.)
4. **MVP+1 or later?** It's a genuinely nice "it's alive" signal but pure polish.
   Slots naturally *after* tap-to-approve (which shares the background-worker infra).

## Recommendation
Build B **after** tap-to-approve (Step 5), because both need the same
"background process that edits Discord + a local rendezvous/timeout" scaffold —
build that scaffold once for tap-to-approve, then B is a ~200-LOC addition reusing
it. Greenlight when the MVP (A + turn-state + trace + tap-approve) is shipping.
