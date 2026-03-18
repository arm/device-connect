#!/usr/bin/env python3
"""Number Generator device simulator.

A simulated IoT device that periodically emits random numbers as events.
An AI agent connected via device-connect-agent-tools can discover this device,
subscribe to its events, and invoke its RPC functions.

Usage:
    # NATS (default) — dev mode (no auth)
    DEVICE_CONNECT_ALLOW_INSECURE=true python device_simulator.py

    # Zenoh — dev mode
    DEVICE_CONNECT_ALLOW_INSECURE=true ZENOH_CONNECT=tcp/localhost:7447 python device_simulator.py

    # NATS with credentials
    NATS_CREDENTIALS_FILE=~/.device-connect/credentials/rng-001.creds.json python device_simulator.py

    # Custom options
    python device_simulator.py --device-id rng-002 --interval 2.0 --min 0 --max 1000
"""

from __future__ import annotations

import asyncio
import argparse
import logging
import os
import random
import signal

from device_connect_sdk import DeviceRuntime
from device_connect_sdk.drivers import DeviceDriver, rpc, emit, periodic
from device_connect_sdk.types import DeviceIdentity, DeviceStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-28s  %(levelname)-7s  %(message)s",
)
log = logging.getLogger("number-generator")


class NumberGeneratorDriver(DeviceDriver):
    """Simulated number generator device."""

    device_type = "number_generator"

    def __init__(self, interval: float = 5.0, min_val: float = 0, max_val: float = 100):
        super().__init__()
        self._interval = interval
        self._min = min_val
        self._max = max_val
        self._total = 0

    @property
    def identity(self) -> DeviceIdentity:
        return DeviceIdentity(
            device_type="number_generator",
            manufacturer="Device Connect",
            model="RNG-500",
            firmware_version="1.0.0",
            description="Generates random numbers for testing AI agent pipelines",
        )

    @property
    def status(self) -> DeviceStatus:
        return DeviceStatus(location="simulator", availability="available")

    @emit()
    async def number_generated(self, value: float, label: str):
        """A new number was generated."""
        pass

    @rpc()
    async def generate(self, min: float = 0, max: float = 100) -> dict:
        """Generate a random number in [min, max]."""
        value = round(random.uniform(min, max), 2)
        self._total += 1
        log.info(">>> generate called — value=%.2f", value)
        await self.number_generated(value=value, label="on_demand")
        return {"value": value, "total_generated": self._total}

    @rpc()
    async def get_stats(self) -> dict:
        """Return generation statistics."""
        return {
            "total_generated": self._total,
            "range": [self._min, self._max],
            "interval_seconds": self._interval,
        }

    @periodic(interval=5.0)
    async def auto_generate(self):
        """Emit a random number every interval."""
        value = round(random.uniform(self._min, self._max), 2)
        self._total += 1
        log.info("emitting %.2f [periodic]", value)
        await self.number_generated(value=value, label="periodic")

    async def connect(self) -> None:
        log.info("Number generator driver connected")
        self.auto_generate.__func__._routine_interval = self._interval

    async def disconnect(self) -> None:
        log.info("Number generator driver disconnecting")


async def run(device_id: str, interval: float, min_val: float, max_val: float):
    allow_insecure = os.getenv("DEVICE_CONNECT_ALLOW_INSECURE", "").lower() in ("1", "true", "yes")

    # Auto-detect backend from env: ZENOH_CONNECT for Zenoh, NATS_URL for NATS
    zenoh_connect = os.getenv("ZENOH_CONNECT")
    if zenoh_connect:
        messaging_urls = [ep.strip() for ep in zenoh_connect.split(",")]
    else:
        messaging_urls = [os.getenv("NATS_URL", "nats://localhost:4222")]

    driver = NumberGeneratorDriver(interval=interval, min_val=min_val, max_val=max_val)

    device = DeviceRuntime(
        driver=driver,
        device_id=device_id,
        messaging_urls=messaging_urls,
        allow_insecure=allow_insecure,
    )

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    log.info("=" * 50)
    log.info("  Number Generator Simulator")
    log.info("  Device ID : %s", device_id)
    log.info("  Range     : [%.1f, %.1f]", min_val, max_val)
    log.info("  Interval  : %.1fs", interval)
    log.info("  Press Ctrl+C to stop")
    log.info("=" * 50)

    device_task = asyncio.create_task(device.run())
    await stop_event.wait()
    await device.stop()

    if not device_task.done():
        device_task.cancel()
        try:
            await device_task
        except asyncio.CancelledError:
            pass


def main():
    parser = argparse.ArgumentParser(description="Number Generator device simulator")
    parser.add_argument("--device-id", default="rng-001")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--min", type=float, default=0)
    parser.add_argument("--max", type=float, default=100)
    args = parser.parse_args()

    asyncio.run(run(device_id=args.device_id, interval=args.interval, min_val=args.min, max_val=args.max))


if __name__ == "__main__":
    main()
