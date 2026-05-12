#!/usr/bin/env python3
"""device-a -- the device that goes offline in the peer-lifecycle demo.

Just heartbeats so the registry tracks it. When this process is killed,
the registry's offline_monitor will publish a `device-connect.<tenant>.device.offline`
event after the heartbeat TTL expires (default ~15s).
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from device_connect_edge import DeviceRuntime
from device_connect_edge.drivers import DeviceDriver, rpc
from device_connect_edge.types import DeviceIdentity

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  device-a  %(levelname)-7s  %(message)s")
log = logging.getLogger("device-a")


class DeviceADriver(DeviceDriver):
    device_type = "demo_device"

    @property
    def identity(self) -> DeviceIdentity:
        return DeviceIdentity(
            device_type="demo_device",
            description="Demo device A -- goes offline to trigger peer_lost",
        )

    @rpc()
    async def ping(self) -> dict:
        return {"ok": True}

    async def connect(self) -> None:
        log.info("connected")

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
        driver=DeviceADriver(),
        device_id="device-a",
        messaging_urls=urls,
        allow_insecure=allow_insecure,
    )

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, stop.set)

    log.info("=" * 56)
    log.info(" device-a   broker=%s", urls)
    log.info(" Kill me to trigger device-b's peer_lost handler.")
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
