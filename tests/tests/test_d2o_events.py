# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for D2O event emission.

Tests that device_connect_edge drivers emit events that can be captured
by messaging subscribers (orchestrator perspective).
"""

import asyncio
import json
import pytest


SETTLE_TIME = 0.1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_camera_emits_event(device_spawner, event_capture):
    """Camera driver should emit events receivable by subscribers."""
    cam, cam_driver = await device_spawner.spawn_camera("itest-evt-cam")

    async with event_capture.subscribe("device-connect.*.itest-evt-cam.event.*") as events:
        await asyncio.sleep(SETTLE_TIME)

        await cam_driver.trigger_event("state_change_detected", {
            "zone_id": "zone-A",
            "class": "mess",
        })

        event = await events.wait_for("state_change_detected", timeout=5)
        assert event.device_id == "itest-evt-cam"
        assert event.data["zone_id"] == "zone-A"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_robot_emits_cleaning_finished(device_spawner, event_capture, messaging_client):
    """Robot should emit cleaning_finished after dispatch_robot completes."""
    robot, robot_driver = await device_spawner.spawn_robot(
        "itest-evt-robot", clean_duration=0.3,
    )

    async with event_capture.subscribe("device-connect.*.itest-evt-robot.event.*") as events:
        await asyncio.sleep(SETTLE_TIME)

        # Send RPC directly via messaging client
        request = {
            "jsonrpc": "2.0",
            "id": "test-rpc-1",
            "method": "dispatch_robot",
            "params": {"zone_id": "zone-B"},
        }
        response = await messaging_client.request(
            "device-connect.default.itest-evt-robot.cmd",
            json.dumps(request).encode(),
            timeout=5.0,
        )
        data = json.loads(response)
        assert data["result"]["status"] == "accepted"

        # Wait for completion event
        event = await events.wait_for("cleaning_finished", timeout=10)
        assert event.data["zone_id"] == "zone-B"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sensor_emits_reading(device_spawner, event_capture):
    """Sensor should emit reading events when triggered."""
    sensor, sensor_driver = await device_spawner.spawn_sensor("itest-evt-sensor")

    async with event_capture.subscribe("device-connect.*.itest-evt-sensor.event.*") as events:
        await asyncio.sleep(SETTLE_TIME)

        await sensor_driver.trigger_event("reading", {
            "temperature": 25.5,
            "humidity": 60.0,
            "unit": "celsius",
        })

        event = await events.wait_for("reading", timeout=5)
        assert event.data["temperature"] == 25.5


@pytest.mark.asyncio
@pytest.mark.integration
async def test_event_injector_simulates_device(event_injector, event_capture):
    """EventInjector should simulate device events receivable by subscribers."""
    async with event_capture.subscribe("device-connect.*.fake-device.event.*") as events:
        await asyncio.sleep(SETTLE_TIME)

        await event_injector.inject_event(
            device_id="fake-device",
            event_name="alert",
            payload={"level": "high", "message": "test alert"},
        )

        event = await events.wait_for("alert", timeout=5)
        assert event.data["level"] == "high"
        assert event.data["simulated"] is True
