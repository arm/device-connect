"""Test camera driver using device_connect_edge (device-connect-edge).

Validates the edge SDK package by importing from device_connect_edge.drivers.
"""

import asyncio
import time
import uuid
from typing import Optional

from device_connect_edge.drivers import DeviceDriver, rpc, emit
from device_connect_edge.types import DeviceIdentity, DeviceStatus


class TestCameraDriver(DeviceDriver):
    """Simulated camera for integration tests."""

    device_type = "test_camera"

    def __init__(self, failure_rate: float = 0.0, min_latency_ms: float = 10,
                 max_latency_ms: float = 50, location: str = "test-zone"):
        super().__init__()
        self._failure_rate = failure_rate
        self._min_delay = min_latency_ms / 1000
        self._max_delay = max_latency_ms / 1000
        self._location = location

    async def simulate_delay(self):
        import random
        await asyncio.sleep(random.uniform(self._min_delay, self._max_delay))

    def should_fail(self) -> bool:
        import random
        return random.random() < self._failure_rate

    @property
    def identity(self) -> DeviceIdentity:
        return DeviceIdentity(
            device_type="camera",
            manufacturer="TestCorp",
            model="TestCam-1000",
            firmware_version="1.0.0-test",
            arch="x86_64",
        )

    @property
    def status(self) -> DeviceStatus:
        return DeviceStatus(location=self._location)

    @rpc()
    async def capture_image(self, resolution: str = "1080p") -> dict:
        """Capture a simulated test image."""
        await self.simulate_delay()
        if self.should_fail():
            raise RuntimeError("Simulated capture failure")
        return {
            "image_b64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVQYV2NgYAAAAAMAAWgmWQ0AAAAASUVORK5CYII=",
            "resolution": resolution,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "device_id": getattr(self, "_device_id", "unknown"),
        }

    @emit()
    async def state_change_detected(self, zone_id: str, state_class: str, details: Optional[str] = None):
        """State change detected in camera view."""
        pass

    @emit()
    async def object_detected(self, label: str, confidence: float, bbox: Optional[list] = None):
        """Object detected in camera view."""
        pass

    async def trigger_event(self, event_name: str, payload: dict) -> None:
        """Programmatically trigger an event for testing."""
        payload.setdefault("event_id", uuid.uuid4().hex[:8])
        if "ts" not in payload and "timestamp" not in payload:
            payload["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await self._emit_event_internal(event_name, payload)

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass
