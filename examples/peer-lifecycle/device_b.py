#!/usr/bin/env python3
"""device-b -- watches device-a, emits `black_out` when its peer goes offline.

Demonstrates the lifecycle subscription added in feat/peer-lifecycle-events:

    @on(device_id="device-a", event_name="peer_lost")

Subscribes to the registry's `device-connect.<tenant>.device.offline`
subject, filters by `device_id` post-hoc (since the subject is shared
per tenant), and reacts.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from device_connect_edge import DeviceRuntime
from device_connect_edge.drivers import DeviceDriver, emit
from device_connect_edge.drivers.base import on
from device_connect_edge.types import DeviceIdentity

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  device-b  %(levelname)-7s  %(message)s")
log = logging.getLogger("device-b")


class DeviceBDriver(DeviceDriver):
    device_type = "demo_device"

    @property
    def identity(self) -> DeviceIdentity:
        return DeviceIdentity(
            device_type="demo_device",
            description="Demo device B -- reacts to A's disappearance",
        )

    @on(device_id="device-a", event_name="peer_lost")
    async def on_peer_a_lost(self, device_id, event_name, payload):
        log.warning("peer LOST: %s   payload=%s", device_id, payload)
        await self.black_out(reason=f"peer {device_id} went offline")

    @emit()
    async def black_out(self, reason: str):
        """Emitted when the demo's peer dependency drops."""
        # Body is pre-emit hook -- the @emit decorator handles publication.
        log.info(">>> emitting black_out: %s", reason)

    async def connect(self) -> None:
        log.info("connected, waiting for device-a peer_lost")

    async def disconnect(self) -> None:
        log.info("disconnecting")


def _resolve_messaging_urls() -> list[str]:
    zenoh = os.environ.get("ZENOH_CONNECT")
    if zenoh:
        return [u.strip() for u in zenoh.split(",") if u.strip()]
    return [os.environ.get("NATS_URL", "nats://localhost:4222")]


async def main() -> None:
    urls = _resolve_messaging_urls()
    allow_insecure = os.environ.get("DEVICE_CONNECT_ALLOW_INSECURE", "true").lower() in (
        "1", "true", "yes",
    )
    device = DeviceRuntime(
        driver=DeviceBDriver(),
        device_id="device-b",
        messaging_urls=urls,
        allow_insecure=allow_insecure,
    )

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, stop.set)

    log.info("=" * 56)
    log.info(" device-b   broker=%s   watching=device-a", urls)
    log.info("=" * 56)

    task = asyncio.create_task(device.run())
    await stop.wait()
    await device.stop()
    if not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
