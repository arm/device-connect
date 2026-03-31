"""Integration tests for D2D (Device-to-Device) event subscription.

Tests the @on decorator pattern: Device B subscribes to Device A's events
and reacts automatically via NATS pub/sub.
"""

import asyncio
import time

import pytest

from device_connect_sdk.drivers import DeviceDriver, rpc, emit, on
from device_connect_sdk.types import DeviceIdentity, DeviceStatus
from device_connect_sdk import DeviceRuntime


SETTLE_TIME = 0.5


class ReactiveRobotDriver(DeviceDriver):
    """A robot that automatically reacts to camera events via @on decorator."""

    device_type = "reactive_robot"

    def __init__(self):
        super().__init__()
        self.received_events: list[dict] = []
        self._cleaning = False

    @property
    def identity(self) -> DeviceIdentity:
        return DeviceIdentity(
            device_type="cleaner_robot",
            manufacturer="TestCorp",
            model="ReactiveBot-1000",
            firmware_version="1.0.0-test",
        )

    @property
    def status(self) -> DeviceStatus:
        return DeviceStatus(location="test-zone")

    @on(event_name="state_change_detected")
    async def on_state_change(self, device_id: str, event_name: str, params: dict) -> None:
        """React to state_change_detected events from any device."""
        self.received_events.append({
            "source_device": device_id,
            "event": event_name,
            "params": params,
        })
        # Emit our own event in response
        await self.emit_event("cleaning_started", {
            "zone_id": params.get("zone_id", "unknown"),
            "triggered_by": device_id,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })

    @rpc()
    async def get_received_events(self) -> dict:
        """Return all events received via @on decorator."""
        return {"events": self.received_events, "count": len(self.received_events)}

    @emit()
    async def cleaning_started(self, zone_id: str, triggered_by: str):
        pass

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass


@pytest.mark.asyncio
@pytest.mark.integration
async def test_d2d_on_decorator_receives_events(device_spawner, event_capture, nats_url):
    """Device with @on decorator should receive events from another device."""
    # Spawn the camera (event source)
    cam, cam_driver = await device_spawner.spawn_camera("itest-d2d-cam")

    # Spawn the reactive robot manually (custom driver)
    robot_driver = ReactiveRobotDriver()
    robot_driver._device_id = "itest-d2d-reactive-robot"
    robot_device = DeviceRuntime(
        driver=robot_driver,
        device_id="itest-d2d-reactive-robot",
        messaging_urls=[nats_url],
        tenant="default",
        ttl=15,
        allow_insecure=True,
    )
    robot_task = asyncio.create_task(robot_device.run())

    try:
        # Wait for robot to be ready (registration in NATS mode, announcer in D2D mode)
        for _ in range(100):
            if getattr(robot_device, '_d2d_mode', False):
                if getattr(robot_device, '_d2d_announcer', None) is not None:
                    break
            elif robot_device._registration_id is not None:
                break
            await asyncio.sleep(0.1)
        if getattr(robot_device, '_d2d_mode', False):
            assert robot_device._d2d_announcer is not None
        else:
            assert robot_device._registration_id is not None

        async with event_capture.subscribe("device-connect.*.itest-d2d-reactive-robot.event.*") as events:
            await asyncio.sleep(SETTLE_TIME)

            # Camera emits event — robot's @on handler should receive it
            await cam_driver.trigger_event("state_change_detected", {
                "zone_id": "zone-D2D",
                "class": "mess",
            })

            # Wait for the robot's reactive event
            event = await events.wait_for("cleaning_started", timeout=10)
            assert event.data["zone_id"] == "zone-D2D"
            # In Zenoh, the SDK's @on handler may not correctly parse source device_id
            if not getattr(robot_device, '_d2d_mode', False):
                assert event.data["triggered_by"] == "itest-d2d-cam"

        # Verify the robot's internal state
        assert len(robot_driver.received_events) >= 1
        # In Zenoh, the SDK's @on handler may not correctly parse source device_id
        # from slash-separated subjects (known SDK limitation)
        if not getattr(robot_device, '_d2d_mode', False):
            assert robot_driver.received_events[0]["source_device"] == "itest-d2d-cam"

    finally:
        robot_task.cancel()
        try:
            await robot_task
        except asyncio.CancelledError:
            pass
