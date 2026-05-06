# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Bounded NDJSON event-stream reader for `dc-portalctl devices stream`.

Race three signals: server payload, wall-clock duration, event count. Whichever
hits first wins; emit a `_meta` trailer locally if the server doesn't (older
servers / SSE format).
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import IO


async def stream_ndjson(
    session,
    url: str,
    headers: dict,
    *,
    duration: float | None,
    count: int | None,
    follow: bool,
    out: IO[str] | None = None,
) -> tuple[int, int, str]:
    """Read NDJSON from the URL line-by-line, enforcing duration/count locally.

    The server also enforces these caps; the client enforces them too so a
    misbehaving or proxy-buffered server can't hang the agent.

    Returns (exit_code, events_received, closed_by).
    """
    if out is None:
        out = sys.stdout
    started = time.monotonic()
    events = 0
    closed_by = "server"
    saw_meta = False

    async with session.get(url, headers=headers) as resp:
        if resp.status >= 400:
            text = await resp.text()
            sys.stderr.write(f"stream failed: HTTP {resp.status}: {text}\n")
            return 1, 0, "error"

        async for raw in resp.content:
            if not raw:
                continue
            line = raw.decode(errors="replace").strip()
            if not line:
                continue

            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # Pass through anything we can't parse so the user can see it
                out.write(line + "\n")
                out.flush()
                continue

            if isinstance(obj, dict) and "_meta" in obj:
                saw_meta = True
                meta = obj["_meta"]
                closed_by = str(meta.get("closed_by") or closed_by)
                events = int(meta.get("events_received") or events)
                out.write(json.dumps(obj) + "\n")
                out.flush()
                break

            events += 1
            out.write(json.dumps(obj) + "\n")
            out.flush()

            elapsed = time.monotonic() - started
            if duration is not None and elapsed >= duration and not follow:
                closed_by = "duration"
                break
            if count is not None and events >= count and not follow:
                closed_by = "count"
                break

    elapsed = time.monotonic() - started
    if not saw_meta:
        out.write(json.dumps({"_meta": {
            "closed_by": closed_by,
            "events_received": events,
            "elapsed_s": round(elapsed, 3),
        }}) + "\n")
        out.flush()

    if events == 0 and closed_by == "duration":
        return 2, events, closed_by  # documented "no events" exit code
    return 0, events, closed_by


def race_with_timer(coro, duration: float | None):
    """Race a coroutine against an asyncio sleep — used as belt-and-braces fallback.

    Currently unused (the line-loop polls the wall clock itself), but kept here
    if a future blocking variant is needed.
    """
    if duration is None:
        return coro
    return asyncio.wait_for(coro, timeout=duration + 5.0)
