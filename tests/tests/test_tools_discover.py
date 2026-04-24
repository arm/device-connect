# Copyright (c) 2024-2026, Arm Limited and Contributors. All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for device-connect-agent-tools discover_devices().

Tests that the agent SDK can discover devices registered via device_connect_edge.
"""

import asyncio
import pytest


SETTLE_TIME = 0.3


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_returns_list(device_spawner, messaging_url):
    """discover_devices() should return a list."""
    await device_spawner.spawn_camera("itest-tools-disc-cam")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect, discover_devices

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        result = await asyncio.to_thread(discover_devices)
        assert isinstance(result, list)
        assert len(result) >= 1
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_includes_capabilities(device_spawner, messaging_url):
    """Discovered devices should include capabilities (functions, events)."""
    await device_spawner.spawn_sensor("itest-tools-caps-sensor")
    await asyncio.sleep(SETTLE_TIME)

    from device_connect_agent_tools import connect, disconnect, discover_devices

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        devices = await asyncio.to_thread(discover_devices)
        sensor = next((d for d in devices if d["device_id"] == "itest-tools-caps-sensor"), None)
        assert sensor is not None, f"Sensor not found in {[d['device_id'] for d in devices]}"

        # discover_devices() flattens capabilities into top-level functions/events
        function_names = [f["name"] for f in sensor.get("functions", [])]
        assert "get_reading" in function_names, f"get_reading not in {function_names}"
    finally:
        await asyncio.to_thread(disconnect)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_discover_refresh(device_spawner, messaging_url):
    """discover_devices(refresh=True) should pick up newly registered devices."""
    from device_connect_agent_tools import connect, disconnect, discover_devices

    await asyncio.to_thread(connect, nats_url=messaging_url)
    try:
        # First call — may or may not include our device
        await asyncio.to_thread(discover_devices)

        # Now spawn a new device
        await device_spawner.spawn_camera("itest-tools-refresh-cam")
        await asyncio.sleep(SETTLE_TIME)

        # Refresh should find it
        devices = await asyncio.to_thread(discover_devices, refresh=True)
        device_ids = [d["device_id"] for d in devices]
        assert "itest-tools-refresh-cam" in device_ids
    finally:
        await asyncio.to_thread(disconnect)
