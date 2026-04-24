# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for device lifecycle.

Tests that device_connect_edge-based drivers register with the real registry,
maintain heartbeats, and can be discovered via device-connect-agent-tools.
"""

import asyncio
import pytest


SETTLE_TIME = 0.3
DISCOVERY_TIMEOUT = 10
DISCOVERY_POLL = 0.3


@pytest.mark.asyncio
@pytest.mark.integration
async def test_camera_registers(device_spawner, messaging_backend):
    """A device_connect_edge camera should register and get a registration ID."""
    device, driver = await device_spawner.spawn_camera("itest-cam-lifecycle")
    if messaging_backend == "zenoh":
        assert device._d2d_announcer is not None
    else:
        assert device._registration_id is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_robot_registers(device_spawner, messaging_backend):
    """A device_connect_edge robot should register and get a registration ID."""
    device, driver = await device_spawner.spawn_robot("itest-robot-lifecycle")
    if messaging_backend == "zenoh":
        assert device._d2d_announcer is not None
    else:
        assert device._registration_id is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sensor_registers(device_spawner, messaging_backend):
    """A device_connect_edge sensor should register and get a registration ID."""
    device, driver = await device_spawner.spawn_sensor("itest-sensor-lifecycle")
    if messaging_backend == "zenoh":
        assert device._d2d_announcer is not None
    else:
        assert device._registration_id is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_device_discoverable_via_tools(device_spawner, messaging_url):
    """Devices registered via device_connect_edge should be discoverable via device-connect-agent-tools."""
    device, driver = await device_spawner.spawn_camera("itest-cam-discover")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect, discover_devices

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        deadline = asyncio.get_event_loop().time() + DISCOVERY_TIMEOUT
        device_ids: list[str] = []
        while asyncio.get_event_loop().time() < deadline:
            devices = await asyncio.to_thread(discover_devices)
            device_ids = [d["device_id"] for d in devices]
            if "itest-cam-discover" in device_ids:
                break
            await asyncio.sleep(DISCOVERY_POLL)
        assert "itest-cam-discover" in device_ids, f"Device not found in {device_ids}"
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_multiple_devices_discoverable(device_spawner, messaging_url):
    """Multiple device_connect_edge drivers should all appear in discovery."""
    await device_spawner.spawn_camera("itest-multi-cam")
    await device_spawner.spawn_robot("itest-multi-robot")
    await device_spawner.spawn_sensor("itest-multi-sensor")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect, discover_devices

    expected = {"itest-multi-cam", "itest-multi-robot", "itest-multi-sensor"}
    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        deadline = asyncio.get_event_loop().time() + DISCOVERY_TIMEOUT
        device_ids: list[str] = []
        while asyncio.get_event_loop().time() < deadline:
            devices = await asyncio.to_thread(discover_devices)
            device_ids = [d["device_id"] for d in devices]
            if expected.issubset(device_ids):
                break
            await asyncio.sleep(DISCOVERY_POLL)
        for name in expected:
            assert name in device_ids, f"{name} not found in {device_ids}"
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_device_type_filter(device_spawner, messaging_url):
    """discover_devices(device_type=...) should filter by type."""
    await device_spawner.spawn_camera("itest-filter-cam")
    await device_spawner.spawn_robot("itest-filter-robot")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect, discover_devices

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        deadline = asyncio.get_event_loop().time() + DISCOVERY_TIMEOUT
        camera_ids: list[str] = []
        while asyncio.get_event_loop().time() < deadline:
            cameras = await asyncio.to_thread(discover_devices, device_type="camera")
            camera_ids = [d["device_id"] for d in cameras]
            if "itest-filter-cam" in camera_ids:
                break
            await asyncio.sleep(DISCOVERY_POLL)
        assert "itest-filter-cam" in camera_ids
        # Robot should not appear in camera-filtered results
        assert "itest-filter-robot" not in camera_ids
    finally:
        await asyncio.to_thread(disconnect)
