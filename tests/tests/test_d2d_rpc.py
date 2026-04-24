# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for D2D (Device-to-Device) direct RPC invocation.

Tests that one device can call another device's RPC directly,
without an orchestrator in the loop.
"""

import asyncio
import json
import pytest


SETTLE_TIME = 0.2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_d2d_direct_rpc_sensor_reading(device_spawner, messaging_client):
    """Device A (robot) calls Device B (sensor) get_reading directly."""
    sensor, sensor_driver = await device_spawner.spawn_sensor(
        "itest-d2d-sensor", initial_temp=25.0, initial_humidity=55.0,
    )
    robot, robot_driver = await device_spawner.spawn_robot("itest-d2d-robot-caller")
    await asyncio.sleep(SETTLE_TIME)

    request = {
        "jsonrpc": "2.0",
        "id": "d2d-rpc-1",
        "method": "get_reading",
        "params": {"unit": "celsius"},
    }
    response = await messaging_client.request(
        "device-connect.default.itest-d2d-sensor.cmd",
        json.dumps(request).encode(),
        timeout=5.0,
    )
    data = json.loads(response)
    assert "result" in data, f"RPC failed: {data}"
    assert data["result"]["temperature"] == 25.0
    assert data["result"]["humidity"] == 55.0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_d2d_direct_rpc_robot_status(device_spawner, messaging_client):
    """Direct RPC to get robot status without orchestrator."""
    robot, robot_driver = await device_spawner.spawn_robot("itest-d2d-status-robot")
    await asyncio.sleep(SETTLE_TIME)

    request = {
        "jsonrpc": "2.0",
        "id": "d2d-rpc-2",
        "method": "get_status",
        "params": {},
    }
    response = await messaging_client.request(
        "device-connect.default.itest-d2d-status-robot.cmd",
        json.dumps(request).encode(),
        timeout=5.0,
    )
    data = json.loads(response)
    assert "result" in data
    assert data["result"]["busy"] is False


@pytest.mark.asyncio
@pytest.mark.integration
async def test_d2d_rpc_to_unknown_device(device_spawner, messaging_client):
    """RPC to a non-existent device should timeout."""
    request = {
        "jsonrpc": "2.0",
        "id": "d2d-rpc-missing",
        "method": "get_reading",
        "params": {},
    }
    with pytest.raises(Exception):
        await messaging_client.request(
            "device-connect.default.nonexistent-device.cmd",
            json.dumps(request).encode(),
            timeout=2.0,
        )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_d2d_rpc_chain(device_spawner, event_capture, messaging_client):
    """Chain: dispatch robot -> robot cleans -> emits cleaning_finished."""
    robot, robot_driver = await device_spawner.spawn_robot(
        "itest-d2d-chain-robot", clean_duration=0.3,
    )
    await asyncio.sleep(SETTLE_TIME)

    async with event_capture.subscribe("device-connect.*.itest-d2d-chain-robot.event.*") as events:
        request = {
            "jsonrpc": "2.0",
            "id": "d2d-chain-1",
            "method": "dispatch_robot",
            "params": {"zone_id": "zone-chain"},
        }
        response = await messaging_client.request(
            "device-connect.default.itest-d2d-chain-robot.cmd",
            json.dumps(request).encode(),
            timeout=5.0,
        )
        data = json.loads(response)
        assert data["result"]["status"] == "accepted"

        event = await events.wait_for("cleaning_finished", timeout=10)
        assert event.data["zone_id"] == "zone-chain"
