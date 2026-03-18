"""Test robot driver using device_connect_sdk (device-connect-sdk).

Validates the edge SDK package by importing from device_connect_sdk.drivers.
"""

import asyncio
import time
from typing import Optional

from device_connect_sdk.drivers import DeviceDriver, rpc, emit
from device_connect_sdk.types import DeviceIdentity, DeviceStatus


class TestRobotDriver(DeviceDriver):
    """Simulated cleaning robot for integration tests."""

    device_type = "test_robot"

    def __init__(self, clean_duration: float = 0.5, failure_rate: float = 0.0,
                 min_latency_ms: float = 10, max_latency_ms: float = 50,
                 location: str = "test-zone"):
        super().__init__()
        self._failure_rate = failure_rate
        self._min_delay = min_latency_ms / 1000
        self._max_delay = max_latency_ms / 1000
        self._clean_duration = clean_duration
        self._location = location
        self._busy = False
        self._current_zone: Optional[str] = None
        self._cleaning_task: Optional[asyncio.Task] = None
        self._should_fail_flag = False

    async def simulate_delay(self):
        import random
        await asyncio.sleep(random.uniform(self._min_delay, self._max_delay))

    def should_fail(self) -> bool:
        import random
        return self._should_fail_flag or random.random() < self._failure_rate

    def set_should_fail(self, val: bool) -> None:
        self._should_fail_flag = val

    @property
    def identity(self) -> DeviceIdentity:
        return DeviceIdentity(
            device_type="cleaner_robot",
            manufacturer="TestCorp",
            model="TestClean-2000",
            firmware_version="1.0.0-test",
            arch="x86_64",
        )

    @property
    def status(self) -> DeviceStatus:
        return DeviceStatus(location=self._location)

    @rpc()
    async def dispatch_robot(self, zone_id: str) -> dict:
        """Dispatch the robot to clean a zone."""
        await self.simulate_delay()
        if self._should_fail_flag:
            raise RuntimeError("Robot dispatch failed (simulated failure injection)")
        if self._busy:
            return {"status": "rejected", "reason": "robot is busy", "current_zone": self._current_zone}
        self._busy = True
        self._current_zone = zone_id
        self._cleaning_task = asyncio.create_task(self._do_cleaning(zone_id))
        return {"status": "accepted", "zone_id": zone_id, "estimated_duration": self._clean_duration}

    @rpc()
    async def get_status(self) -> dict:
        """Get current robot status."""
        await self.simulate_delay()
        return {"busy": self._busy, "current_zone": self._current_zone}

    @emit("cleaning_finished")
    async def emit_cleaning_finished(self, zone_id: str, elapsed: float):
        pass

    @emit("cleaning_failed")
    async def emit_cleaning_failed(self, zone_id: str, error: str):
        pass

    async def _do_cleaning(self, zone_id: str) -> None:
        try:
            await asyncio.sleep(self._clean_duration)
            if self.should_fail():
                await self.emit_event("cleaning_failed", {
                    "zone_id": zone_id, "error": "simulated failure",
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
            else:
                await self.emit_event("cleaning_finished", {
                    "zone_id": zone_id, "elapsed": self._clean_duration,
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
        finally:
            self._busy = False
            self._current_zone = None

    async def trigger_event(self, event_name: str, payload: dict) -> None:
        if "ts" not in payload and "timestamp" not in payload:
            payload["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await self.emit_event(event_name, payload)

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        if self._cleaning_task and not self._cleaning_task.done():
            self._cleaning_task.cancel()
            try:
                await self._cleaning_task
            except asyncio.CancelledError:
                pass
