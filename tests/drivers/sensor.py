"""Test sensor driver using device_connect_edge (device-connect-edge).

Validates the edge SDK package by importing from device_connect_edge.drivers.
"""

import asyncio
import time
import uuid
from typing import Optional

from device_connect_edge.drivers import DeviceDriver, rpc, emit
from device_connect_edge.types import DeviceIdentity, DeviceStatus


class TestSensorDriver(DeviceDriver):
    """Simulated temperature/humidity sensor for integration tests."""

    device_type = "test_sensor"

    def __init__(self, failure_rate: float = 0.0, min_latency_ms: float = 10,
                 max_latency_ms: float = 50, location: str = "test-room",
                 initial_temp: float = 22.0, initial_humidity: float = 45.0):
        super().__init__()
        self._failure_rate = failure_rate
        self._min_delay = min_latency_ms / 1000
        self._max_delay = max_latency_ms / 1000
        self._location = location
        self._current_temp = initial_temp
        self._current_humidity = initial_humidity

    async def simulate_delay(self):
        import random
        await asyncio.sleep(random.uniform(self._min_delay, self._max_delay))

    def should_fail(self) -> bool:
        import random
        return random.random() < self._failure_rate

    def set_values(self, temp: float, humidity: float) -> None:
        self._current_temp = temp
        self._current_humidity = humidity

    @property
    def identity(self) -> DeviceIdentity:
        return DeviceIdentity(
            device_type="temperature_sensor",
            manufacturer="TestCorp",
            model="TestSensor-1000",
            firmware_version="1.0.0-test",
            arch="x86_64",
            description="Test temperature/humidity sensor for integration tests.",
        )

    @property
    def status(self) -> DeviceStatus:
        return DeviceStatus(location=self._location, availability="available")

    @rpc()
    async def get_reading(self, unit: str = "celsius") -> dict:
        """Get current temperature and humidity reading."""
        await self.simulate_delay()
        if self.should_fail():
            raise RuntimeError("Simulated sensor read failure")
        temp = self._current_temp
        if unit == "fahrenheit":
            temp = (temp * 9 / 5) + 32
        elif unit == "kelvin":
            temp = temp + 273.15
        return {
            "temperature": round(temp, 2),
            "humidity": round(self._current_humidity, 2),
            "unit": unit,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "device_id": getattr(self, "_device_id", "unknown"),
        }

    @rpc()
    async def set_threshold(self, temperature: float, humidity: Optional[float] = None) -> dict:
        """Set alert thresholds."""
        await self.simulate_delay()
        return {"status": "success", "temperature_threshold": temperature}

    @rpc()
    async def set_location(self, location: str) -> dict:
        """Update the sensor's location."""
        await self.simulate_delay()
        old = self._location
        self._location = location
        return {"status": "success", "old_location": old, "location": location}

    @emit()
    async def reading(self, temperature: float, humidity: float, unit: str = "celsius"):
        """Periodic sensor reading."""
        pass

    @emit()
    async def threshold_exceeded(self, temperature: float, humidity: float, exceeded: str):
        """Threshold exceeded alert."""
        pass

    async def trigger_event(self, event_name: str, payload: dict) -> None:
        payload.setdefault("event_id", uuid.uuid4().hex[:8])
        if "ts" not in payload and "timestamp" not in payload:
            payload["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        await self._emit_event_internal(event_name, payload)

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass
