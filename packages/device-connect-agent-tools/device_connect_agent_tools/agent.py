# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""DeviceConnectAgent — high-level agent that connects, discovers, and reacts to events.

Usage::

    from device_connect_agent_tools import DeviceConnectAgent

    agent = DeviceConnectAgent(goal="monitor cameras", nats_url="nats://localhost:4222")
    await agent.prepare()
    await agent.run()       # blocks, listening for events
    await agent.stop()

Or as an async context manager::

    async with DeviceConnectAgent(goal="monitor cameras") as agent:
        print(agent.devices)
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue as _queue
from typing import Any, Callable, Dict, List, Optional

from device_connect_agent_tools.connection import connect, disconnect, get_connection

logger = logging.getLogger(__name__)


class DeviceConnectAgent:
    """Async agent that connects to Device Connect, discovers devices, and reacts to events."""

    def __init__(
        self,
        goal: str,
        on_event: Optional[Callable[[str, str, Dict[str, Any]], None]] = None,
        batch_window: float = 12.0,
        nats_url: Optional[str] = None,
        zone: str = "default",
        credentials: Optional[Dict[str, Any]] = None,
        tls_config: Optional[Dict[str, Any]] = None,
        request_timeout: float = 30.0,
    ):
        self.goal = goal
        self._on_event = on_event
        self._batch_window = batch_window
        self._connect_kwargs = {
            "nats_url": nats_url,
            "zone": zone,
            "credentials": credentials,
            "tls_config": tls_config,
            "request_timeout": request_timeout,
        }
        self._devices: List[Dict[str, Any]] = []
        self._stopped = False

    # ── Properties ──────────────────────────────────────────────────

    @property
    def zone(self) -> str:
        return self._connect_kwargs.get("zone", "default")

    @property
    def devices(self) -> List[Dict[str, Any]]:
        return self._devices

    # ── Lifecycle ───────────────────────────────────────────────────

    async def prepare(self) -> Dict[str, Any]:
        """Connect to NATS and discover devices.

        Returns:
            Dict with ``goal`` and ``devices``.
        """
        from device_connect_agent_tools.tools import discover_devices

        connect(**self._connect_kwargs)
        self._devices = discover_devices()
        return {"goal": self.goal, "devices": self._devices}

    async def run(self) -> None:
        """Listen for device events and dispatch them.

        Uses a subscribe → batch → prompt pattern.

        The NATS subscription runs on the connection's dedicated event
        loop thread; a thread-safe queue bridges events back to this
        coroutine.

        Blocks until :meth:`stop` is called or the task is cancelled.
        """
        conn = get_connection()
        event_q: _queue.Queue = _queue.Queue()

        async def _on_msg(data: bytes, subject: str, reply=None):
            try:
                parts = subject.split(".")
                dev_id = parts[2] if len(parts) >= 5 else "unknown"
                event_name = ".".join(parts[4:]) if len(parts) >= 5 else subject
                payload = json.loads(data.decode())
                params = payload.get("params", {})

                if self._on_event:
                    self._on_event(dev_id, event_name, params)

                event_q.put({
                    "device_id": dev_id,
                    "event_name": event_name,
                    "params": params,
                })
            except Exception as e:
                logger.error("Error parsing event: %s", e)

        # Subscribe on the connection's event loop (where NATS lives)
        subject = f"device-connect.{self.zone}.*.event.>"
        future = asyncio.run_coroutine_threadsafe(
            conn.async_subscribe_with_subject(subject, _on_msg), conn.loop,
        )
        self._sub = future.result(timeout=10.0)

        loop = asyncio.get_running_loop()
        try:
            while not self._stopped:
                # Poll thread-safe queue without blocking the event loop
                try:
                    first = await loop.run_in_executor(
                        None, lambda: event_q.get(timeout=1.0),
                    )
                except _queue.Empty:
                    continue

                batch = [first]
                await asyncio.sleep(self._batch_window)
                while not event_q.empty():
                    try:
                        batch.append(event_q.get_nowait())
                    except _queue.Empty:
                        break

                prompt = _build_prompt(self.goal, batch)
                self._run_agent_sync(prompt)
        except asyncio.CancelledError:
            pass
        finally:
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    self._sub.unsubscribe(), conn.loop,
                )
                fut.result(timeout=2.0)
            except Exception:
                logger.debug("cleanup error during unsubscribe", exc_info=True)

    def _run_agent_sync(self, prompt: str) -> str:
        """Invoke the LLM with the given prompt.

        Override in tests or subclasses. The default raises
        NotImplementedError so callers know they need to supply an LLM.
        """
        raise NotImplementedError(
            "Subclass must implement _run_agent_sync or override it"
        )

    async def stop(self) -> None:
        """Stop the event loop and close the connection."""
        self._stopped = True
        # Unsubscribe before closing (best-effort)
        sub = getattr(self, "_sub", None)
        if sub is not None:
            try:
                conn = get_connection()
                fut = asyncio.run_coroutine_threadsafe(
                    sub.unsubscribe(), conn.loop,
                )
                fut.result(timeout=2.0)
            except Exception:
                logger.debug("cleanup error during stop unsubscribe", exc_info=True)
            self._sub = None
        disconnect()

    # ── Context manager ─────────────────────────────────────────────

    async def __aenter__(self):
        await self.prepare()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()
        return False


def _build_prompt(goal: str, batch: list[dict]) -> str:
    """Build an LLM prompt from a batch of device events."""
    lines = []
    for evt in batch:
        display = {
            k: v for k, v in evt["params"].items()
            if not k.startswith("_") and k not in ("event_id", "ts", "traceparent")
        }
        lines.append(
            f"- {evt['device_id']}::{evt['event_name']}: "
            f"{json.dumps(display, default=str)}"
        )

    return (
        f"Goal: {goal}\n\n"
        f"{len(batch)} device event(s) received:\n"
        + "\n".join(lines)
        + "\n\nAnalyze these events and take any necessary actions."
    )
