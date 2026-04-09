#!/usr/bin/env python3
"""Trigger script for WebRTC streaming between devices.

Connects as a temporary device, sends one RPC, then exits.

Usage:
    export NATS_URL=nats://fabric.deviceconnect.dev:4222
    export NATS_CREDENTIALS_FILE=../beta/credentials/beta-device-003.creds.json

    # Tell a display to start watching a camera:
    python trigger_stream.py watch display-001 camera-001

    # Tell a display to stop watching a camera:
    python trigger_stream.py stop display-001 camera-001

    # List active streams on a device (camera or display):
    python trigger_stream.py list camera-001
"""

import asyncio
import logging
import os
import sys

from device_connect_edge import DeviceRuntime
from device_connect_edge.drivers import DeviceDriver
from device_connect_edge.types import DeviceIdentity, DeviceStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-24s  %(levelname)-7s  %(message)s",
)
logger = logging.getLogger("webrtc.trigger")


class TriggerDriver(DeviceDriver):
    """Minimal throwaway device used to send a single RPC."""

    device_type = "trigger"

    @property
    def identity(self) -> DeviceIdentity:
        return DeviceIdentity(
            device_type="trigger",
            manufacturer="Artly",
            model="Trigger-CLI",
            firmware_version="1.0.0",
            description="CLI trigger for WebRTC stream control",
        )

    @property
    def status(self) -> DeviceStatus:
        return DeviceStatus(location="cli", availability="available")

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass


async def run_trigger(command: str, args: list[str]):
    """Start a trigger device, send the command, then exit."""
    allow_insecure = os.getenv(
        "DEVICE_CONNECT_ALLOW_INSECURE", ""
    ).lower() in ("1", "true", "yes")

    driver = TriggerDriver()
    device = DeviceRuntime(
        driver=driver,
        device_id="trigger-stream-001",
        allow_insecure=allow_insecure,
    )

    async def send_and_stop():
        logger.info("Waiting for device discovery...")
        await asyncio.sleep(3.0)

        try:
            if command == "watch":
                if len(args) < 2:
                    logger.error("Usage: watch <display_id> <camera_id>")
                    await device.stop()
                    return
                display_id, camera_id = args[0], args[1]
                logger.info("Telling %s to watch %s ...", display_id, camera_id)
                result = await driver.invoke_remote(
                    display_id, "watch_camera",
                    timeout=20.0,
                    camera_device_id=camera_id,
                )
                inner = result.get("result", result)
                logger.info("Result: %s", inner)

            elif command == "stop":
                if len(args) < 2:
                    logger.error("Usage: stop <display_id> <camera_id>")
                    await device.stop()
                    return
                display_id, camera_id = args[0], args[1]
                logger.info("Telling %s to stop watching %s ...", display_id, camera_id)
                result = await driver.invoke_remote(
                    display_id, "stop_watching",
                    timeout=10.0,
                    camera_device_id=camera_id,
                )
                inner = result.get("result", result)
                logger.info("Result: %s", inner)

            elif command == "list":
                if len(args) < 1:
                    logger.error("Usage: list <device_id>")
                    await device.stop()
                    return
                target_id = args[0]
                logger.info("Listing streams on %s ...", target_id)
                result = await driver.invoke_remote(
                    target_id, "list_streams", timeout=10.0,
                )
                inner = result.get("result", result)
                streams = inner.get("streams", [])
                if streams:
                    for s in streams:
                        logger.info("  %s", s)
                else:
                    logger.info("  (no active streams)")

            else:
                logger.error("Unknown command: %s", command)
                logger.info("Commands: watch, stop, list")

        except Exception as exc:
            logger.error("Trigger failed: %s", exc)

        await asyncio.sleep(1.0)
        await device.stop()

    asyncio.ensure_future(send_and_stop())

    try:
        await device.run()
    except asyncio.CancelledError:
        pass


def main():
    if len(sys.argv) < 2:
        print("Usage:", file=sys.stderr)
        print(f"  {sys.argv[0]} watch <display_id> <camera_id>", file=sys.stderr)
        print(f"  {sys.argv[0]} stop  <display_id> <camera_id>", file=sys.stderr)
        print(f"  {sys.argv[0]} list  <device_id>", file=sys.stderr)
        print(file=sys.stderr)
        print("Examples:", file=sys.stderr)
        print(f"  {sys.argv[0]} watch display-001 camera-001", file=sys.stderr)
        print(f"  {sys.argv[0]} stop  display-001 camera-001", file=sys.stderr)
        print(f"  {sys.argv[0]} list  camera-001", file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]
    args = sys.argv[2:]

    try:
        asyncio.run(run_trigger(command, args))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
