"""Local file-based approval rendezvous.

A tiny request/response channel between the in-session PreToolUse hook and the
out-of-session Discord bot. The hook writes a `.req` file describing a pending
tool call and blocks polling for the matching `.resp` file; the bot writes the
`.resp` when the owner taps ✅/❌.

The one invariant that matters: **fail closed.** `await_decision` returns "deny"
whenever a valid "allow" did not arrive in time — no response, a malformed
response, or a response that isn't exactly "allow". A missed or corrupt tap must
never run the tool.

No network here — the hook stays offline; only the bot side touches Discord.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from claude_code_discord import _core

# Poll cadence while waiting for a decision. Small enough to feel instant on a
# tap, large enough not to spin the CPU.
_POLL_INTERVAL_S = 0.05


def _rendezvous_dir() -> Path:
    """Resolve the rendezvous dir at CALL time (not import), so a changed
    CCD_STATE_DIR — e.g. per test, or a reconfigured install — is honored."""
    d = Path(_core._resolve_state_dir()) / "rendezvous"
    d.mkdir(parents=True, exist_ok=True)
    return d


def request_path(req_id: str) -> Path:
    return _rendezvous_dir() / f"{req_id}.req"


def response_path(req_id: str) -> Path:
    return _rendezvous_dir() / f"{req_id}.resp"


def create_request(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Record a pending approval and return its unique id."""
    req_id = uuid.uuid4().hex
    body = {
        "req_id": req_id,
        "tool_name": tool_name,
        # A short digest, not the full input — the request file is only a
        # human-facing summary for the card; the tool still runs from CC's copy.
        "tool_input": tool_input,
        "ts": time.time(),
    }
    request_path(req_id).write_text(json.dumps(body))
    return req_id


def read_request(req_id: str) -> dict[str, Any]:
    return json.loads(request_path(req_id).read_text())


def write_decision(req_id: str, decision: str) -> None:
    """The bot writes the owner's tap result. Only 'allow'/'deny' are meaningful;
    anything else will be treated as 'deny' by await_decision (fail closed)."""
    response_path(req_id).write_text(json.dumps({"decision": decision}))


def await_decision(req_id: str, deadline_s: float) -> str:
    """Block until a decision arrives or the deadline elapses. Returns 'allow'
    ONLY if a well-formed 'allow' response is present; returns 'deny' in every
    other case (timeout, malformed, missing, or an explicit deny)."""
    resp = response_path(req_id)
    deadline = time.monotonic() + deadline_s
    while True:
        if resp.exists():
            decision = _read_decision(resp)
            # Only an explicit, well-formed "allow" is allowed through.
            return "allow" if decision == "allow" else "deny"
        if time.monotonic() >= deadline:
            return "deny"  # no tap in time → fail closed
        time.sleep(_POLL_INTERVAL_S)


def _read_decision(resp: Path) -> str | None:
    """Parse a response file, returning the decision string or None if the file
    is unreadable/malformed. Never raises — a corrupt file must not crash the
    hook into an ambiguous state; the caller treats None as deny."""
    try:
        data = json.loads(resp.read_text())
        d = data.get("decision")
        return d if isinstance(d, str) else None
    except (OSError, ValueError):
        return None


def cleanup(req_id: str) -> None:
    """Remove both files for a settled request. Best-effort."""
    for p in (request_path(req_id), response_path(req_id)):
        try:
            p.unlink()
        except OSError:
            pass
