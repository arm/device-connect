#!/usr/bin/env python3
# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""String Generator device simulator — uses Device Connect SDK constructs.

A simulated IoT device that periodically emits random string fragments
as events over NATS. An AI agent (Strands, LangChain, etc.) connected
via device-connect-agent-tools can discover this device, subscribe to its events,
and invoke its RPC functions.

Usage:
    # Start infra
    cd /path/to/Fab/core && docker compose up nats-jwt etcd device-registry-service -d

    # Run with explicit credentials
    python device_simulator.py --creds ~/.device-connect/credentials/devctl.creds.json

    # Or set via env var
    NATS_CREDENTIALS_FILE=~/.device-connect/credentials/devctl.creds.json \
      python device_simulator.py

    # Credentials auto-discovery checks (in order):
    #   1. NATS_CREDENTIALS_FILE env var
    #   2. ~/.device-connect/credentials/{device_id}.creds.json
    #   3. ~/.device-connect/credentials/{device_id}.creds

Environment Variables:
    NATS_URL: NATS server URL (default: nats://localhost:4222)
    NATS_CREDENTIALS_FILE: Path to .creds.json or .creds file
    TENANT: Tenant namespace (default: default)
    DEVICE_CONNECT_ALLOW_INSECURE: Allow insecure connections (default: false)
"""

from __future__ import annotations

import asyncio
import argparse
import logging
import os
import random
import signal
from pathlib import Path
from typing import Optional

from device_connect_edge import DeviceRuntime
from device_connect_edge.drivers import DeviceDriver, rpc, emit, periodic
from device_connect_edge.types import DeviceIdentity, DeviceStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-28s  %(levelname)-7s  %(message)s",
)
log = logging.getLogger("device-simulator")

# ── Word pools by mood ────────────────────────────────────────────────

_WORD_POOLS = {
    "mysterious": [
        "shadow", "whisper", "echoing", "void", "obsidian",
        "phantom", "twilight", "enigma", "labyrinth", "mirage",
    ],
    "euphoric": [
        "radiant", "soaring", "brilliant", "crimson", "cascade",
        "blazing", "drifting", "infinite", "aurora", "zenith",
    ],
    "melancholic": [
        "fading", "hollow", "sinking", "ashen", "forgotten",
        "withered", "silent", "crumbling", "remnant", "solitude",
    ],
    "chaotic": [
        "shattering", "fractal", "colliding", "spiral", "thunder",
        "erupting", "tangled", "volatile", "distorted", "surge",
    ],
}

_MOODS = list(_WORD_POOLS.keys())


class StringGeneratorDriver(DeviceDriver):
    """Simulated string generator device.

    Periodically emits random word fragments as ``words_generated`` events.
    Exposes ``get_status`` and ``generate_now`` RPCs so an agent can query
    or trigger generation on demand.
    """

    device_type = "string_generator"

    def __init__(self, interval: float = 4.0):
        super().__init__()
        self._interval = interval
        self._total_generated = 0

    # ── Identity & Status ─────────────────────────────────────────────

    @property
    def identity(self) -> DeviceIdentity:
        return DeviceIdentity(
            device_type="string_generator",
            manufacturer="Device Connect",
            model="Wordsmith-1000",
            firmware_version="1.0.0",
            arch="x86_64",
            description="Generates random word fragments for testing AI agent pipelines",
        )

    @property
    def status(self) -> DeviceStatus:
        return DeviceStatus(
            location="simulator",
            availability="available",
        )

    # ── Events ────────────────────────────────────────────────────────

    @emit()
    async def words_generated(self, fragment: str, word_count: int, mood: str):
        """A new word fragment was generated.

        Args:
            fragment: Space-separated random words.
            word_count: Number of words in the fragment.
            mood: The mood/theme of the words.
        """
        pass

    # ── RPCs ──────────────────────────────────────────────────────────

    @rpc()
    async def get_status(self) -> dict:
        """Get current status of the string generator.

        Returns:
            Dictionary with status, mode, total_generated, and interval.
        """
        log.info(">>> get_status called by agent")
        return {
            "status": "online",
            "mode": "random",
            "total_generated": self._total_generated,
            "interval_seconds": self._interval,
        }

    @rpc()
    async def generate_now(self, mood: str = "mysterious") -> dict:
        """Generate a word fragment on demand.

        Args:
            mood: Word mood/theme — one of mysterious, euphoric,
                  melancholic, chaotic. Defaults to mysterious.

        Returns:
            Dictionary with the generated fragment, word_count, and mood.
        """
        mood = mood if mood in _WORD_POOLS else random.choice(_MOODS)
        word_count = random.randint(3, 6)
        words = [random.choice(_WORD_POOLS[mood]) for _ in range(word_count)]
        fragment = " ".join(words)

        log.info(">>> generate_now called by agent — mood=%s, result='%s'", mood, fragment)
        self._total_generated += 1

        await self.words_generated(fragment=fragment, word_count=word_count, mood=mood)
        return {"fragment": fragment, "word_count": word_count, "mood": mood}

    # ── Periodic generation loop ──────────────────────────────────────

    @periodic(interval=4.0)
    async def generation_loop(self) -> None:
        """Emit a random word fragment every interval."""
        mood = random.choice(_MOODS)
        word_count = random.randint(3, 6)
        words = [random.choice(_WORD_POOLS[mood]) for _ in range(word_count)]
        fragment = " ".join(words)

        log.info("emitting '%s' [%s]", fragment, mood)
        self._total_generated += 1

        await self.words_generated(fragment=fragment, word_count=word_count, mood=mood)

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def connect(self) -> None:
        log.info("String generator driver connected")
        # Override the periodic interval from __init__
        self.generation_loop.__func__._routine_interval = self._interval

    async def disconnect(self) -> None:
        log.info("String generator driver disconnecting")


def _find_credentials_file(device_id: str) -> Optional[str]:
    """Auto-discover NATS credentials from well-known paths.

    Search order:
        1. NATS_CREDENTIALS_FILE env var (explicit override)
        2. ~/.device-connect/credentials/{device_id}.creds.json
        3. ~/.device-connect/credentials/{device_id}.creds
    """
    env_creds = os.getenv("NATS_CREDENTIALS_FILE")
    if env_creds and os.path.exists(env_creds):
        return env_creds

    creds_dir = Path.home() / ".device-connect" / "credentials"
    for suffix in (".creds.json", ".creds"):
        path = creds_dir / f"{device_id}{suffix}"
        if path.exists():
            return str(path)
    return None


async def run(device_id: str, interval: float, creds_file: Optional[str] = None):
    """Start the device simulator."""
    nats_url = os.getenv("NATS_URL", "nats://localhost:4222")
    tenant = os.getenv("TENANT", "default")


    driver = StringGeneratorDriver(interval=interval)

    device_kwargs = {
        "driver": driver,
        "device_id": device_id,
        "tenant": tenant,
    }

    # Configure messaging: JWT credentials or insecure
    if not creds_file:
        creds_file = _find_credentials_file(device_id)
    if creds_file:
        log.info("Using credentials: %s", creds_file)
        device_kwargs["nats_credentials_file"] = creds_file
        # Credential files embed Docker-internal NATS URLs (nats-jwt:4222).
        # Override with the externally-accessible URL so it works from the host.
        device_kwargs["messaging_urls"] = [nats_url]
        # Skip device_id-vs-JWT validation (simulator uses arbitrary IDs)
        device_kwargs["allow_insecure"] = True
    else:
        device_kwargs["messaging_urls"] = [nats_url]
        device_kwargs["allow_insecure"] = True

    device = DeviceRuntime(**device_kwargs)

    # Graceful shutdown on Ctrl+C
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler():
        log.info("Shutting down ...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    log.info("")
    log.info("=" * 60)
    log.info("  String Generator Simulator")
    log.info("  Device ID : %s", device_id)
    log.info("  Interval  : %.1fs", interval)
    log.info("  Press Ctrl+C to stop")
    log.info("=" * 60)
    log.info("")

    # Run device in background, wait for stop signal
    device_task = asyncio.create_task(device.run())

    await stop_event.wait()
    await device.stop()

    # Cancel device task if still running
    if not device_task.done():
        device_task.cancel()
        try:
            await device_task
        except asyncio.CancelledError:
            pass


def main():
    parser = argparse.ArgumentParser(
        description="String Generator device simulator (Device Connect SDK)",
    )
    parser.add_argument(
        "--device-id",
        default="sim-wordsmith-001",
        help="Device ID to register as (default: sim-wordsmith-001)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=4.0,
        help="Seconds between word generation events (default: 4)",
    )
    parser.add_argument(
        "--creds",
        default=None,
        help="Path to .creds.json or .creds file (default: auto-discover)",
    )
    args = parser.parse_args()

    asyncio.run(run(device_id=args.device_id, interval=args.interval, creds_file=args.creds))


if __name__ == "__main__":
    main()
