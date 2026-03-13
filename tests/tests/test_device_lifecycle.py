"""Integration tests for device lifecycle.

Tests that device_connect_sdk-based drivers register with the real registry,
maintain heartbeats, and can be discovered via device-connect-agent-tools.
"""

import asyncio
import pytest


SETTLE_TIME = 0.3


@pytest.mark.asyncio
@pytest.mark.integration
async def test_camera_registers(device_spawner, messaging_backend):
    """A device_connect_sdk camera should register and get a registration ID."""
    device, driver = await device_spawner.spawn_camera("itest-cam-lifecycle")
    if messaging_backend == "zenoh":
        assert device._d2d_announcer is not None
    else:
        assert device._registration_id is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_robot_registers(device_spawner, messaging_backend):
    """A device_connect_sdk robot should register and get a registration ID."""
    device, driver = await device_spawner.spawn_robot("itest-robot-lifecycle")
    if messaging_backend == "zenoh":
        assert device._d2d_announcer is not None
    else:
        assert device._registration_id is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sensor_registers(device_spawner, messaging_backend):
    """A device_connect_sdk sensor should register and get a registration ID."""
    device, driver = await device_spawner.spawn_sensor("itest-sensor-lifecycle")
    if messaging_backend == "zenoh":
        assert device._d2d_announcer is not None
    else:
        assert device._registration_id is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_device_discoverable_via_tools(device_spawner, messaging_url):
    """Devices registered via device_connect_sdk should be discoverable via device-connect-agent-tools."""
    device, driver = await device_spawner.spawn_camera("itest-cam-discover")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect, discover_devices

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        devices = await asyncio.to_thread(discover_devices)
        device_ids = [d["device_id"] for d in devices]
        assert "itest-cam-discover" in device_ids, f"Device not found in {device_ids}"
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_multiple_devices_discoverable(device_spawner, messaging_url):
    """Multiple device_connect_sdk drivers should all appear in discovery."""
    await device_spawner.spawn_camera("itest-multi-cam")
    await device_spawner.spawn_robot("itest-multi-robot")
    await device_spawner.spawn_sensor("itest-multi-sensor")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect, discover_devices

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        devices = await asyncio.to_thread(discover_devices)
        device_ids = [d["device_id"] for d in devices]
        assert "itest-multi-cam" in device_ids
        assert "itest-multi-robot" in device_ids
        assert "itest-multi-sensor" in device_ids
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
        cameras = await asyncio.to_thread(discover_devices, device_type="camera")
        camera_ids = [d["device_id"] for d in cameras]
        assert "itest-filter-cam" in camera_ids
        # Robot should not appear in camera-filtered results
        assert "itest-filter-robot" not in camera_ids
    finally:
        await asyncio.to_thread(disconnect)
