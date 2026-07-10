"""End-to-end smoke test against the mock model — no credentials needed.

Exercises the full chain: engine -> query loop -> tool orchestration ->
permission gate (auto-grant broker) -> bash tool -> transcript persistence.

    python scripts/smoke.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("COMPASS_MOCK_MODEL", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compass.core.query_engine import QueryEngine, Session  # noqa: E402
from compass.models import events  # noqa: E402
from compass.tools.base import PermissionBroker  # noqa: E402


async def main() -> int:
    engine = QueryEngine()
    session = Session(broker=PermissionBroker(policy="auto_grant"))
    seen: list[str] = []

    print(f"session: {session.id}\n")
    async for event in engine.ask(session, "Run a quick sanity check of the workspace."):
        seen.append(event.type)
        if isinstance(event, events.TextDelta):
            print(event.text, end="", flush=True)
        elif isinstance(event, events.ToolCallStarted):
            print(f"\n[tool_call] {event.tool_name} {event.arguments}")
        elif isinstance(event, events.ToolResult):
            print(f"[tool_result] error={event.is_error} {event.content[:120]}")
        elif isinstance(event, events.UsageReport):
            print(
                f"\n[usage] prompt={event.prompt_tokens} "
                f"completion={event.completion_tokens} cost=${event.cost_usd}"
            )
        elif isinstance(event, events.TurnComplete):
            print(f"[turn_complete] reason={event.reason} turns={event.turns}")

    await engine.store.flush()
    transcript = await engine.store.load(session.id)
    print(f"\ntranscript records: {len(transcript)}")

    required = {"stream_request_start", "text_delta", "assistant_message",
                "tool_call_started", "tool_result", "turn_complete"}
    missing = required - set(seen)
    tool_ok = any(
        m.role == "tool" and not m.is_error and "compass-smoke-ok" in (m.content or "")
        for m in transcript
    )
    if missing:
        print(f"FAIL: missing events {missing}")
        return 1
    if not tool_ok:
        print("FAIL: bash tool result not found in transcript")
        return 1
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
